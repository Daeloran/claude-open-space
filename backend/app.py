"""L'Open Space : pilote des sessions Claude Code (Agent SDK) et diffuse des événements de jeu au front.

Chaque « employé » est une session ClaudeSDKClient persistante, recrutée à la demande sur un projet
(dossier) : son contexte grossit d'un ticket à l'autre, d'où la jauge de fatigue et les pauses café
(compaction). Chaque employé a sa propre file de tickets, traités l'un après l'autre.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from . import konsole
from .events import INTERN_NAMES, ask_questions, deliverable_for, summarize_tool
from .observer import Observer, TranscriptTail, chat_entries, chat_history
from .plan_usage import PlanUsage
from .projects import recent_projects

WORKDIR = os.environ.get("OPENSPACE_CWD")  # projet proposé en tête de liste
# Réserve de prénoms pour les recrutements (cyclique, suffixée une fois épuisée)
TEAM = [n.strip() for n in os.environ.get("OPENSPACE_TEAM", "Léa,Hugo,Inès").split(",") if n.strip()]
CONTEXT_WINDOW = int(os.environ.get("OPENSPACE_CONTEXT", "200000"))
# Non défini : le defaultMode des réglages Claude Code de l'utilisateur s'applique
PERMISSION_MODE = os.environ.get("OPENSPACE_PERMISSION_MODE") or None
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
# Outils sans risque : pas de passage par le bureau du manager
AUTO_TOOLS = ["Read", "Glob", "Grep", "TodoWrite", "WebSearch", "Agent"]
CLAUDE_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
# Hosts pour lesquels on accepte l'origine http://<Host>. Liste fermée contre le DNS rebinding
# (evil.com rebindé sur 127.0.0.1 enverrait Host = Origin = evil.com). « testserver » est le
# Host du TestClient Starlette : inoffensif, un navigateur ne l'enverrait qu'avec un DNS local.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
# Tickets terminés gardés pour le snapshot : au-delà, les plus anciens sont oubliés (mémoire bornée)
DONE_KEPT = 50
ANSWER_MAX = 4000  # caractères par réponse à une question


@dataclass
class Ticket:
    id: str
    title: str


class Hub:
    """Diffusion WebSocket, validations en attente et état courant (pour le snapshot)."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.pending: dict[str, asyncio.Future[tuple[bool, object]]] = {}  # -> (allow, answers bruts)
        self.ticket_seq = 0
        # État dérivé des événements émis, renvoyé à chaque (re)connexion. En mémoire seulement.
        self.board: dict[str, dict] = {}     # ticket_id -> {id, title, status, ...}, ordre de création
        self.agents: dict[str, dict] = {}    # agent_id -> {id, name, cwd, project}, ordre de recrutement
        self.totals = {"usd": 0.0, "tokens": 0}
        self.context: dict[str, float] = {}  # agent_id -> ratio
        self.requests: dict[str, dict] = {}  # request_id -> événement permission_request en attente
        # Session terminal -> ticket tapé dans son onglet : {"id", "busy", "sent"} (id None pendant l'envoi)
        self.terminal: dict[str, dict] = {}
        # agent_id -> clients dont le panneau de discussion est ouvert sur cet employé
        self.chats: dict[str, set[WebSocket]] = {}

    def track(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "ticket_created":
            self.board[ev["ticket"]["id"]] = {**ev["ticket"], "status": "queued"}
        elif kind == "ticket_assigned" and (t := self.board.get(ev["ticket_id"])):
            t.update(status="assigned", agent_id=ev.get("agent_id"))
        elif kind == "ticket_done" and (t := self.board.get(ev["ticket_id"])):
            t.update(status="done", ok=ev.get("ok", True), usd=ev.get("usd", 0.0))
            done = [k for k, v in self.board.items() if v["status"] == "done"]
            for k in done[:-DONE_KEPT]:
                del self.board[k]
        elif kind == "agent_hired":
            self.agents[ev["agent"]["id"]] = ev["agent"]
        elif kind == "observed_joined":  # session du terminal
            self.agents[ev["agent"]["id"]] = {**ev["agent"], "observed": True}
        elif kind == "observed_left":
            self.agents.pop(ev["agent_id"], None)
            self.context.pop(ev["agent_id"], None)
            self.chats.pop(ev["agent_id"], None)
        elif kind == "observed_status" and (a := self.agents.get(ev["agent_id"])):
            a["status"] = ev["status"]
            a.pop("waiting_for", None)
            if "waiting_for" in ev:
                a["waiting_for"] = ev["waiting_for"]
        elif kind == "cost":
            self.totals["usd"] += ev.get("usd") or 0.0
            self.totals["tokens"] += ev.get("tokens") or 0
        elif kind == "context":
            self.context[ev["agent_id"]] = ev["ratio"]
        elif kind == "permission_request":
            self.requests[ev["request_id"]] = ev
        elif kind == "permission_resolved":
            self.requests.pop(ev["request_id"], None)

    def snapshot(self) -> dict:
        return {"type": "snapshot", "agents": list(self.agents.values()),
                "tickets": list(self.board.values()), "totals": dict(self.totals),
                "context": dict(self.context), "pending_permissions": list(self.requests.values())}

    async def emit(self, event: dict) -> None:
        if event.get("type") == "chat_entry":  # contenu de conversation : panneaux abonnés seulement, hors snapshot
            for ws in list(self.chats.get(event.get("agent_id"), ())):
                with contextlib.suppress(Exception):
                    await ws.send_json(event)
            return
        self.track(event)
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                self.clients.discard(ws)
        await self.follow_terminal(event)

    async def follow_terminal(self, ev: dict) -> None:
        """Ticket d'une session terminal terminé au premier signal : fin de réponse dans le transcript
        (`observed_turn_end` postérieur à l'envoi), retour idle après busy (pas `waiting` : il attend ta réponse), ou départ de la session."""
        kind, aid = ev.get("type"), ev.get("agent_id")
        t = self.terminal.get(aid)
        if not t or not t["id"]:
            return
        if kind == "observed_status" and ev.get("status") == "busy":
            t["busy"] = True
            return
        # ponytail: une réponse en cours au moment de l'envoi (prompt mis en file) peut clore le ticket tôt
        if (kind == "observed_left" or (kind == "observed_status" and t["busy"] and ev.get("status") == "idle")
                or (kind == "observed_turn_end" and _after(ev.get("at"), t.get("sent")))):
            del self.terminal[aid]
            await self.emit({"type": "ticket_done", "ticket_id": t["id"], "agent_id": aid,
                             "usd": 0.0, "tokens": 0, "ok": kind != "observed_left"})


def _after(at, sent: datetime | None) -> bool:
    """Horodatage du transcript postérieur à l'envoi (illisible ou absent : ligne nouvelle, on la prend)."""
    try:
        return sent is None or datetime.fromisoformat(str(at)) >= sent
    except (TypeError, ValueError):
        return True


hub = Hub()
plan = PlanUsage(CLAUDE_CONFIG_DIR)
observer: Observer | None = None  # créé au démarrage ; connaît le pid de chaque session terminal


class Employee:
    def __init__(self, idx: int, name: str, cwd: str | None = None) -> None:
        self.id = f"e{idx}"
        self.name = name
        self.cwd = cwd
        self.tickets: asyncio.Queue[Ticket] = asyncio.Queue()
        self.subagents: dict[str, str] = {}   # tool_use_id du Task -> id du stagiaire
        self.tool_names: dict[str, str] = {}  # tool_use_id -> nom de l'outil
        self.intern_seq = 0
        self.session_id: str | None = None  # session SDK, connue au premier message : son transcript sert au panneau
        self.tail: TranscriptTail | None = None
        self.options = ClaudeAgentOptions(
            cwd=cwd,
            allowed_tools=AUTO_TOOLS,
            permission_mode=PERMISSION_MODE,
            can_use_tool=self.can_use_tool,
        )

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "cwd": self.cwd, "project": Path(self.cwd or ".").name}

    def actor(self, msg) -> str:
        parent = getattr(msg, "parent_tool_use_id", None)
        return self.subagents.get(parent, self.id) if parent else self.id

    async def can_use_tool(self, tool_name, input_data, context):
        rid = uuid.uuid4().hex[:8]
        fut: asyncio.Future[tuple[bool, object]] = asyncio.get_running_loop().create_future()
        hub.pending[rid] = fut
        ask = tool_name == "AskUserQuestion"
        questions = ask_questions(input_data) if ask else []
        await hub.emit({
            "type": "permission_request", "request_id": rid, "agent_id": self.id,
            "tool": tool_name, "summary": summarize_tool(tool_name, input_data),
            **({"questions": questions} if ask else {}),
        })
        try:
            allow, raw = await fut
        finally:
            # Décision ou annulation (session tombée) : la demande sort de l'état dans tous les cas
            hub.pending.pop(rid, None)
            hub.requests.pop(rid, None)
        if ask and allow:  # réponses du navigateur : questions connues, texte seulement, longueur bornée
            known = {q["question"] for q in questions}
            answers = {q: a[:ANSWER_MAX] for q, a in (raw.items() if isinstance(raw, dict) else [])
                       if q in known and isinstance(a, str)}
            return PermissionResultAllow(updated_input={**input_data, "answers": answers})
        if ask:
            return PermissionResultDeny(message="Le manager n'a pas répondu à ta question : décide toi-même "
                                                "ou demande autrement.")
        if allow:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="Refusé par le manager. Propose une autre approche.")

    async def run(self) -> None:
        while True:
            try:
                await self._work()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # session tombée : on prévient et on relance
                await hub.emit({"type": "message", "agent_id": self.id, "text": f"Session interrompue : {exc}"})
                await asyncio.sleep(5)

    async def _work(self) -> None:
        async with ClaudeSDKClient(options=self.options) as client:
            while True:
                ticket = await self.tickets.get()
                await hub.emit({"type": "ticket_assigned", "ticket_id": ticket.id, "agent_id": self.id})
                cost, tokens, ok = 0.0, 0, True
                try:
                    await client.query(ticket.title)
                    async for msg in client.receive_response():
                        c, t = await self.translate(msg)
                        cost, tokens = cost + c, tokens + t
                        if isinstance(msg, ResultMessage):
                            ok = not getattr(msg, "is_error", False)
                    # Mesure après la réponse (couvre aussi une compaction survenue pendant le ticket)
                    await self.refresh_context(client)
                except Exception as exc:
                    ok = False
                    await hub.emit({"type": "message", "agent_id": self.id, "text": f"Erreur : {exc}"})
                    raise
                finally:
                    await hub.emit({"type": "ticket_done", "ticket_id": ticket.id, "agent_id": self.id,
                                    "usd": cost, "tokens": tokens, "ok": ok})
                    self.tickets.task_done()

    async def translate(self, msg) -> tuple[float, int]:
        who = self.actor(msg)
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    await hub.emit({"type": "message", "agent_id": who, "text": block.text.strip()[:280]})
                elif isinstance(block, ToolUseBlock):
                    self.tool_names[block.id] = block.name
                    summary = summarize_tool(block.name, block.input)
                    await hub.emit({"type": "tool_use", "agent_id": who, "tool": block.name, "summary": summary})
                    if block.name in ("Task", "Agent"):
                        self.intern_seq += 1
                        sid = f"{self.id}-s{self.intern_seq}"
                        self.subagents[block.id] = sid
                        name = INTERN_NAMES[(self.intern_seq - 1) % len(INTERN_NAMES)]
                        await hub.emit({"type": "subagent_spawned", "parent_id": self.id,
                                        "agent": {"id": sid, "name": name}, "task": summary})
                    if d := deliverable_for(block.name, block.input):
                        await hub.emit({"type": "deliverable", "agent_id": who, **d})
        elif isinstance(msg, UserMessage):
            content = msg.content if isinstance(msg.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    tool = self.tool_names.pop(block.tool_use_id, "?")
                    await hub.emit({"type": "tool_result", "agent_id": who, "tool": tool,
                                    "ok": not bool(getattr(block, "is_error", False))})
                    if sid := self.subagents.pop(block.tool_use_id, None):
                        await hub.emit({"type": "subagent_done", "agent_id": sid})
        elif isinstance(msg, RateLimitEvent):
            if plan.apply_rate_limit(msg.rate_limit_info):
                await hub.emit(plan.event)
        elif isinstance(msg, SystemMessage):
            if getattr(msg, "subtype", "") == "init":
                self.set_session((getattr(msg, "data", None) or {}).get("session_id"))
            if getattr(msg, "subtype", "") == "compact_boundary":
                await hub.emit({"type": "compaction", "agent_id": self.id})
        elif isinstance(msg, ResultMessage):
            self.set_session(getattr(msg, "session_id", None))
            usage = msg.usage or {}
            get = lambda k: int(usage.get(k, 0) or 0)  # noqa: E731
            ctx_tokens = get("input_tokens") + get("cache_read_input_tokens") + get("cache_creation_input_tokens")
            tokens = ctx_tokens + get("output_tokens")
            cost = float(msg.total_cost_usd or 0)
            await hub.emit({"type": "cost", "agent_id": self.id, "usd": cost, "tokens": tokens})
            return cost, tokens
        return 0.0, 0

    def set_session(self, sid) -> None:
        if isinstance(sid, str) and re.fullmatch(r"[\w-]+", sid) and sid != self.session_id:  # sid sert dans un glob
            self.session_id, self.tail = sid, None

    def transcript(self) -> Path | None:
        return next((CLAUDE_CONFIG_DIR / "projects").glob(f"*/{self.session_id}.jsonl"), None) if self.session_id else None

    def follow(self) -> Path | None:
        """Transcript de la session, suivi depuis sa fin actuelle dès qu'il existe (offset fixé tout de suite)."""
        if self.tail is None and (path := self.transcript()):
            self.tail = TranscriptTail(path, from_end=True)
            self.tail.read_new()
        return self.tail.path if self.tail else None

    def chat_updates(self) -> list[dict]:
        """Nouvelles entrées du transcript pour les panneaux ouverts (le début est servi par `chat_history`)."""
        if not self.follow():
            return []
        return [{"type": "chat_entry", "agent_id": self.id, "entry": e} for e in chat_entries(self.tail.read_new())]

    async def refresh_context(self, client) -> None:
        """Fatigue = remplissage réel du contexte de la session (maxTokens : limite effective avant compaction)."""
        try:
            usage = await client.get_context_usage()
            ratio = usage["totalTokens"] / (usage.get("maxTokens") or CONTEXT_WINDOW)
        except Exception:
            return  # mesure impossible : on garde la dernière valeur
        await hub.emit({"type": "context", "agent_id": self.id, "ratio": min(1.0, max(0.0, ratio))})


employees: dict[str, Employee] = {}  # recrutés à la demande, jamais licenciés (hors scope #12)
workers: set[asyncio.Task] = set()   # tâches run() des employés, annulées à l'arrêt


def recruit_name(n: int) -> str:
    """n-ième prénom de la réserve, suffixé (« Léa 2 ») quand la réserve est épuisée."""
    name, lap = TEAM[n % len(TEAM)], n // len(TEAM)
    return f"{name} {lap + 1}" if lap else name


async def hire(cwd: str) -> Employee:
    e = Employee(len(employees), recruit_name(len(employees)), cwd)
    employees[e.id] = e
    task = asyncio.create_task(e.run(), name=f"employee-{e.id}")
    workers.add(task)
    task.add_done_callback(workers.discard)
    await hub.emit({"type": "agent_hired", "agent": e.info()})
    return e


async def route_ticket(data: dict) -> Employee | dict | str:
    """Destinataire d'un `new_ticket` : employé (recruté si besoin), session terminal (son agent), ou la raison du refus."""
    if data.get("agent_id"):
        if (agent := hub.agents.get(str(data["agent_id"]), {})).get("observed"):
            return agent
        return employees.get(str(data["agent_id"])) or "Employé inconnu."
    if data.get("cwd"):
        # abspath, pas resolve() : le chemin reste celui de la liste de projets (liens symboliques gardés)
        path = os.path.abspath(os.path.expanduser(str(data["cwd"])))
        return await hire(path) if os.path.isdir(path) else f"Dossier introuvable : {data['cwd']}"
    return "Choisis un employé ou un projet pour ce ticket."


async def type_in_terminal(agent: dict, title: str) -> str | None:
    """Tape le ticket dans l'onglet Konsole de la session terminal. None si envoyé, sinon la raison."""
    aid = agent["id"]
    pid = agent.get("pid") or (observer.watched.get(aid, {}).get("pid") if observer else None)
    if not pid:
        return "Onglet Konsole introuvable pour cette session terminal."
    if hub.agents.get(aid, {}).get("status") == "waiting":  # le texte tomberait dans la question / la permission
        return "La session attend ta réponse dans son terminal : rien n'est tapé."
    if aid in hub.terminal:
        return "Cette session terminal a déjà un ticket en cours."
    hub.terminal[aid] = {"id": None, "busy": False, "sent": datetime.now(timezone.utc)}  # réservé pendant l'envoi (deux onglets du jeu)
    if reason := await konsole.send_prompt(pid, title):
        del hub.terminal[aid]
    return reason


def chat_transcript(aid: str) -> Path | str:
    """Transcript de l'employé `aid` (piloté ou session terminal), ou la raison de l'échec."""
    if e := employees.get(aid):
        # Suivi fixé avant la lecture de l'historique : une ligne écrite entre les deux arrive en double, jamais perdue
        return e.follow() or "Pas encore de conversation : donne-lui un ticket."
    w = observer.watched.get(aid) if observer else None
    if not w:
        return "Employé inconnu (seules les sessions du terminal ont un historique)."
    return w["tail"].path if w["tail"] else "Pas encore de transcript pour cette session."


def read_history(path: Path) -> list[dict] | str:
    try:
        return chat_history(path)
    except OSError:
        return "Transcript illisible."


async def refresh_plan_usage() -> None:
    while True:
        await hub.emit(await plan.get())
        await asyncio.sleep(plan.ttl)


async def observe_terminal(observer: Observer, interval: float = 2.0) -> None:
    """Sessions Claude Code du terminal : une passe toutes les `interval` s, une erreur ne tue pas la boucle."""
    while True:
        try:
            await observer.poll()
            for e in list(employees.values()):  # panneaux des employés pilotés : même relève
                for ev in await asyncio.to_thread(e.chat_updates):
                    await hub.emit(ev)
        except Exception:
            logging.getLogger(__name__).exception("observation des sessions terminal")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    global observer
    plan_task = asyncio.create_task(refresh_plan_usage(), name="plan-usage")
    observer = Observer(CLAUDE_CONFIG_DIR, hub.emit, context_window=CONTEXT_WINDOW)
    observe_task = asyncio.create_task(observe_terminal(observer), name="observer")
    yield
    tasks = [plan_task, observe_task, *workers]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="L'Open Space", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND)


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """Anti Cross-Site WebSocket Hijacking : seule l'interface servie par l'Open Space peut se connecter."""
    if not origin:
        return False
    extra = {o.strip() for o in os.environ.get("OPENSPACE_ALLOWED_ORIGINS", "").split(",") if o.strip()}
    if origin in extra:
        return True
    return bool(host) and urlsplit(f"//{host}").hostname in LOOPBACK_HOSTS and origin == f"http://{host}"


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host")):
        await ws.close(code=1008)
        return
    await ws.accept()
    hub.clients.add(ws)
    projects = await asyncio.to_thread(recent_projects, CLAUDE_CONFIG_DIR, WORKDIR)
    await ws.send_json({"type": "hello", "team": list(hub.agents.values()), "projects": projects})
    await ws.send_json(hub.snapshot())
    await ws.send_json(plan.event)
    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            if kind == "new_ticket" and (title := str(data.get("title", "")).strip()):
                target = await route_ticket(data)
                if isinstance(target, dict):  # session terminal : le prompt est tapé dans son onglet
                    target = await type_in_terminal(target, title) or target
                if isinstance(target, str):  # refus : seul l'émetteur est prévenu
                    await ws.send_json({"type": "ticket_rejected", "title": title, "reason": target})
                    continue
                hub.ticket_seq += 1
                ticket = Ticket(f"t{hub.ticket_seq}", title)
                if isinstance(target, dict):
                    hub.terminal[target["id"]]["id"] = ticket.id
                await hub.emit({"type": "ticket_created", "ticket": {"id": ticket.id, "title": ticket.title}})
                if isinstance(target, dict):
                    await hub.emit({"type": "ticket_assigned", "ticket_id": ticket.id, "agent_id": target["id"]})
                else:
                    await target.tickets.put(ticket)
            elif kind == "open_chat":
                aid = str(data.get("agent_id"))
                res = chat_transcript(aid)
                if isinstance(res, Path):
                    res = await asyncio.to_thread(read_history, res)
                if isinstance(res, str):
                    await ws.send_json({"type": "chat_history", "agent_id": aid, "entries": [], "error": res})
                    continue
                # ponytail: une ligne lue par l'observateur pendant la lecture peut manquer ou arriver en double
                hub.chats.setdefault(aid, set()).add(ws)
                await ws.send_json({"type": "chat_history", "agent_id": aid, "entries": res})
            elif kind == "close_chat":
                hub.chats.get(str(data.get("agent_id")), set()).discard(ws)
            elif kind == "permission_decision":
                rid, allow = str(data.get("request_id")), bool(data.get("allow"))
                fut = hub.pending.get(rid)
                if fut and not fut.done():
                    fut.set_result((allow, data.get("answers")))
                    # Ferme la popup sur les autres onglets
                    await hub.emit({"type": "permission_resolved", "request_id": rid, "allow": allow})
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)
        for subs in hub.chats.values():
            subs.discard(ws)
