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
from .events import INTERN_NAMES, ask_questions, deliverable_for, is_pr_command, summarize_tool, todo_items
from .observer import Observer, TranscriptTail, chat_entries, chat_history, intern_updates, subagent_file
from .plan_usage import PlanUsage
from . import observer as observer_mod
from .projects import first_cwd, recent_projects, resumable_sessions

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
DELIV_KEPT = 20  # livrables gardés pour le snapshot (le panneau en montre 4)
MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "auto")  # Maj+Tab du terminal, par employé
ANSWER_MAX = 4000  # caractères par réponse à une question
LIMIT_MARGIN = 60  # secondes après la remise à zéro de la limite avant de reprendre
LIMIT_RETRY = 300  # sans heure de remise à zéro connue : nouvel essai toutes les 5 min


async def wait_until_reset(seconds: float) -> None:
    await asyncio.sleep(seconds)


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
        self.todos: dict[str, list[dict]] = {}  # agent_id -> dernière liste TodoWrite
        self.commands: dict[str, list[dict]] = {}  # agent_id -> commandes slash de sa session
        self.requests: dict[str, dict] = {}  # request_id -> événement permission_request en attente
        # Stats de l'en-tête et de la Direction, rendues au rechargement de la page
        self.plan_usage: dict | None = None
        self.deliverables: list[dict] = []  # les DELIV_KEPT derniers ; deliverable_count les compte tous
        self.deliverable_count = 0
        self.coffee = 0
        self.permissions = {"total": 0, "denied": 0}  # décisions rendues depuis le jeu
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
        elif kind == "paused" and (a := self.agents.get(ev["agent_id"])):
            a["paused_until"] = ev["until"]
        elif kind == "resumed" and (a := self.agents.get(ev["agent_id"])):
            a.pop("paused_until", None)
        elif kind == "mode_changed" and (a := self.agents.get(ev["agent_id"])):
            a["mode"] = ev["mode"]
        elif kind == "agent_hired":
            self.agents[ev["agent"]["id"]] = ev["agent"]
        elif kind == "observed_joined":  # session du terminal
            self.agents[ev["agent"]["id"]] = {**ev["agent"], "observed": True}
        elif kind in ("observed_left", "agent_left"):  # session terminal partie ou employé congédié
            self.agents.pop(ev["agent_id"], None)
            self.commands.pop(ev["agent_id"], None)
            self.context.pop(ev["agent_id"], None)
            self.todos.pop(ev["agent_id"], None)
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
        elif kind == "commands":
            self.commands[ev["agent_id"]] = ev["commands"]
        elif kind == "todos":
            self.todos[ev["agent_id"]] = ev["todos"]
        elif kind == "permission_request":
            self.requests[ev["request_id"]] = ev
        elif kind == "permission_resolved":
            self.requests.pop(ev["request_id"], None)
        elif kind == "plan_usage":
            self.plan_usage = ev
        elif kind == "deliverable":
            self.deliverable_count += 1
            self.deliverables = [*self.deliverables, {"path": ev.get("path"), "kind": ev.get("kind")}][-DELIV_KEPT:]
        elif kind == "compaction":
            self.coffee += 1

    def snapshot(self) -> dict:
        return {"type": "snapshot", "agents": list(self.agents.values()),
                "tickets": list(self.board.values()), "totals": dict(self.totals),
                "context": dict(self.context), "todos": dict(self.todos), "commands": dict(self.commands), "pending_permissions": list(self.requests.values()),
                "plan_usage": self.plan_usage, "deliverables": list(self.deliverables),
                "deliverable_count": self.deliverable_count, "coffee": self.coffee, "permissions": dict(self.permissions)}

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
                or (kind == "tool_result" and ev.get("pr") and ev.get("ok"))  # PR/MR créée
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
    def __init__(self, idx: int, name: str, cwd: str | None = None, resume: str | None = None) -> None:
        self.id = f"e{idx}"
        self.name = name
        self.cwd = cwd
        self.tickets: asyncio.Queue[Ticket] = asyncio.Queue()
        self.subagents: dict[str, str] = {}   # tool_use_id du Task -> id du stagiaire
        self.tool_names: dict[str, str] = {}  # tool_use_id -> nom de l'outil
        self.prs: set[str] = set()  # tool_use_id des commandes qui ouvrent une PR/MR
        self.delivered = False  # ticket en cours déjà livré (PR/MR créée) : pas de second ticket_done
        self.intern_seq = 0
        self.client: ClaudeSDKClient | None = None
        self.task: asyncio.Task | None = None  # run(), annulée au congé
        self.current: Ticket | None = None  # ticket en cours de réponse
        self.interrupted = False
        self.limited: tuple[int | None] | None = None  # (resets_at,) si la limite d'usage a rejeté la réponse
        self.pause: asyncio.Task | None = None  # attente de la remise à zéro de la limite
        self.commands: list[dict] | None = None  # commandes slash de la session, connues à la connexion
        self.session_id: str | None = None  # session SDK, connue au premier message : son transcript sert au panneau
        self.tail: TranscriptTail | None = None
        self.intern_tails: dict[str, TranscriptTail] = {}  # stagiaire -> transcript de son sous-agent
        self.options = ClaudeAgentOptions(
            cwd=cwd,
            allowed_tools=AUTO_TOOLS,
            permission_mode=PERMISSION_MODE,
            can_use_tool=self.can_use_tool,
            resume=resume,  # conversation existante reprise dans le jeu (#39)
        )
        self.set_session(resume)

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "cwd": self.cwd, "project": Path(self.cwd or ".").name,
                "mode": self.options.permission_mode,
                **({"resumed": self.options.resume} if self.options.resume else {})}

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
            **({"plan": str(input_data.get("plan") or "")} if tool_name == "ExitPlanMode" else {}),
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
            self.client = client
            self.commands, clear = None, False
            while not clear:
                ticket = await self.tickets.get()
                self.current, self.interrupted, self.delivered = ticket, False, False
                await hub.emit({"type": "ticket_assigned", "ticket_id": ticket.id, "agent_id": self.id})
                if self.commands is None:  # liste de la session, lue une fois connectée
                    await self.load_commands(client)
                cost, tokens, ok = 0.0, 0, True
                cmd, _, arg = ticket.title.partition(" ") if ticket.title.startswith("/") else ("", "", "")
                try:
                    if cmd == "/model":  # géré ici : changer de modèle sans prompt
                        self.options.model = arg.strip() or None
                        await client.set_model(self.options.model)
                        continue
                    if cmd == "/clear":  # nouvelle session après ce ticket (sortie du client, relancé par run)
                        clear = True
                        await self.forget_session()
                        continue
                    prompt = ticket.title
                    while True:
                        self.limited = None
                        await client.query(prompt)
                        async for msg in client.receive_response():
                            c, t = await self.translate(msg)
                            cost, tokens = cost + c, tokens + t
                            if isinstance(msg, ResultMessage):
                                ok = not getattr(msg, "is_error", False)
                        if ok or self.limited is None or self.interrupted or not await self.wait_limit():
                            break
                        prompt, ok = "continue", True  # même session : le travail coupé reprend avec son contexte
                    # Mesure après la réponse (couvre aussi une compaction survenue pendant le ticket)
                    await self.refresh_context(client)
                except Exception as exc:
                    ok = False
                    await hub.emit({"type": "message", "agent_id": self.id, "text": f"Erreur : {exc}"})
                    raise
                finally:
                    self.current = None
                    if not self.delivered:
                        await hub.emit({"type": "ticket_done", "ticket_id": ticket.id, "agent_id": self.id,
                                        "usd": cost, "tokens": tokens, "ok": ok and not self.interrupted,
                                        **({"reason": "interrompu"} if self.interrupted else {})})
                    self.tickets.task_done()

    async def wait_limit(self) -> bool:
        """Limite d'usage atteinte : pause jusqu'à sa remise à zéro. False si interrompu pendant la pause."""
        (resets_at,) = self.limited
        now = datetime.now(timezone.utc).timestamp()
        delay = max(resets_at - now, 0) + LIMIT_MARGIN if resets_at else LIMIT_RETRY
        until = datetime.fromtimestamp(now + delay, timezone.utc).isoformat()
        await hub.emit({"type": "paused", "agent_id": self.id, "until": until})
        self.pause = asyncio.ensure_future(wait_until_reset(delay))
        try:
            await asyncio.wait({self.pause})  # l'annulation de la pause (interrupt) ne remonte pas ici
        finally:
            self.pause.cancel()
            self.pause = None
            await hub.emit({"type": "resumed", "agent_id": self.id})
        return not self.interrupted

    async def forget_session(self) -> None:
        """/clear : la conversation repart de zéro (plus de reprise), le panneau et la fatigue aussi."""
        self.options.resume = None
        self.session_id, self.tail, self.intern_tails = None, None, {}
        await hub.emit({"type": "chat_cleared", "agent_id": self.id})
        await hub.emit({"type": "context", "agent_id": self.id, "ratio": 0.0})

    async def load_commands(self, client) -> None:
        """Commandes slash de la session (skills, commandes projet et intégrées), pour l'autocomplétion et le tri."""
        try:
            info = await client.get_server_info() or {}
        except Exception:  # ancienne CLI, client de test : pas de liste, les commandes passent telles quelles
            return
        self.commands = [{"name": str(c["name"]), "description": str(c.get("description") or ""),
                          "argumentHint": str(c.get("argumentHint") or "")}
                         for c in info.get("commands") or [] if isinstance(c, dict) and c.get("name")]
        await hub.emit({"type": "commands", "agent_id": self.id, "commands": self.commands})

    def rejects(self, title: str) -> str | None:
        """Raison du refus d'une commande slash absente de la session (liste connue seulement)."""
        name = title[1:].split(" ", 1)[0] if title.startswith("/") else None
        if name and self.commands is not None and name not in {"model", "clear"} | {c["name"] for c in self.commands}:
            return f"Commande /{name} inconnue ou indisponible hors du terminal."
        return None

    async def set_mode(self, mode: str) -> None:
        """Mode de permission de la session en cours, gardé dans les options pour une reconnexion."""
        if self.client:
            await self.client.set_permission_mode(mode)
        self.options.permission_mode = mode
        await hub.emit({"type": "mode_changed", "agent_id": self.id, "mode": mode})

    async def interrupt(self) -> None:
        """Échap du terminal : stoppe la réponse en cours ; la session est gardée pour le ticket suivant."""
        if not self.current or not self.client:
            return
        self.interrupted = True
        if self.pause:  # en pause sur la limite : pas de réponse en cours à stopper
            self.pause.cancel()
            return
        await self.deny_pending()
        await self.client.interrupt()

    async def deny_pending(self) -> None:
        for rid in [r for r, ev in hub.requests.items() if ev.get("agent_id") == self.id]:
            if (fut := hub.pending.get(rid)) and not fut.done():  # sa demande en attente tombe avec la réponse
                fut.set_result((False, None))
                await hub.emit({"type": "permission_resolved", "request_id": rid, "allow": False})

    async def dismiss(self) -> None:
        """Congé : demandes refusées, tickets en attente en échec, session fermée ; la conversation reste sur disque."""
        employees.pop(self.id, None)
        self.interrupted = True  # le ticket en cours se termine en échec (émis par _work à l'annulation)
        await self.deny_pending()
        if self.task:
            self.task.cancel()  # sortie du `async with ClaudeSDKClient` : processus fermé
            await asyncio.gather(self.task, return_exceptions=True)
        while not self.tickets.empty():  # après l'annulation : plus personne ne les prend
            t = self.tickets.get_nowait()
            self.tickets.task_done()
            await hub.emit({"type": "ticket_done", "ticket_id": t.id, "agent_id": self.id,
                            "usd": 0.0, "tokens": 0, "ok": False, "reason": "congédié"})
        for sid in self.subagents.values():
            await hub.emit({"type": "subagent_done", "agent_id": sid})
        await hub.emit({"type": "agent_left", "agent_id": self.id})

    async def translate(self, msg) -> tuple[float, int]:
        who = self.actor(msg)
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    await hub.emit({"type": "message", "agent_id": who, "text": block.text.strip()[:280]})
                elif isinstance(block, ToolUseBlock):
                    self.tool_names[block.id] = block.name
                    if who == self.id and is_pr_command(block.name, block.input):
                        self.prs.add(block.id)
                    summary = summarize_tool(block.name, block.input)
                    await hub.emit({"type": "tool_use", "agent_id": who, "tool": block.name, "summary": summary})
                    if block.name in ("Task", "Agent"):
                        self.intern_seq += 1
                        sid = f"{self.id}-s{self.intern_seq}"
                        self.subagents[block.id] = sid
                        name = INTERN_NAMES[(self.intern_seq - 1) % len(INTERN_NAMES)]
                        await hub.emit({"type": "subagent_spawned", "parent_id": self.id,
                                        "agent": {"id": sid, "name": name}, "task": summary})
                    if block.name == "TodoWrite":
                        await hub.emit({"type": "todos", "agent_id": who, "todos": todo_items(block.input)})
                    if d := deliverable_for(block.name, block.input):
                        await hub.emit({"type": "deliverable", "agent_id": who, **d})
        elif isinstance(msg, UserMessage):
            content = msg.content if isinstance(msg.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    tool = self.tool_names.pop(block.tool_use_id, "?")
                    ok = not bool(getattr(block, "is_error", False))
                    await hub.emit({"type": "tool_result", "agent_id": who, "tool": tool, "ok": ok})
                    if block.tool_use_id in self.prs:
                        self.prs.discard(block.tool_use_id)
                        # ponytail: coût du ticket inconnu avant la fin du tour → usd 0 (compté dans les totaux, pas dans le coût moyen)
                        if ok and self.current and not self.delivered:  # PR/MR créée : ticket livré sans attendre la fin du tour
                            self.delivered = True
                            await hub.emit({"type": "ticket_done", "ticket_id": self.current.id, "agent_id": self.id,
                                            "usd": 0.0, "tokens": 0, "ok": True})
                    if sid := self.subagents.pop(block.tool_use_id, None):
                        await hub.emit({"type": "subagent_done", "agent_id": sid})
        elif isinstance(msg, RateLimitEvent):
            if getattr(msg.rate_limit_info, "status", None) == "rejected":
                ts = getattr(msg.rate_limit_info, "resets_at", None)
                self.limited = (ts if isinstance(ts, (int, float)) else None,)
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
        # Outils des stagiaires : déjà reçus du SDK (parent_tool_use_id), seules leurs entrées de panneau manquent
        return ([{"type": "chat_entry", "agent_id": self.id, "entry": e} for e in chat_entries(self.tail.read_new())]
                + intern_updates(self.tail.path, self.subagents, self.intern_tails, tools=False))

    async def refresh_context(self, client) -> None:
        """Fatigue = remplissage réel du contexte de la session (maxTokens : limite effective avant compaction)."""
        try:
            usage = await client.get_context_usage()
            ratio = usage["totalTokens"] / (usage.get("maxTokens") or CONTEXT_WINDOW)
        except Exception:
            return  # mesure impossible : on garde la dernière valeur
        await hub.emit({"type": "context", "agent_id": self.id, "ratio": min(1.0, max(0.0, ratio))})


employees: dict[str, Employee] = {}  # recrutés à la demande, retirés au congé (#59)
workers: set[asyncio.Task] = set()   # tâches run() des employés, annulées à l'arrêt


def recruit_name(n: int) -> str:
    """n-ième prénom de la réserve, suffixé (« Léa 2 ») quand la réserve est épuisée."""
    name, lap = TEAM[n % len(TEAM)], n // len(TEAM)
    return f"{name} {lap + 1}" if lap else name


def live_session_ids() -> set[str]:
    """Sessions tenues par un processus Claude Code vivant (terminal) : jamais deux processus sur une session."""
    alive = observer.pid_alive if observer else observer_mod.pid_alive
    return {s["session_id"] for s in observer_mod.live_sessions(CLAUDE_CONFIG_DIR, pid_alive=alive)}


def resume_target(sid: str) -> tuple[str | None, str | None]:
    """(dossier de la session `sid` à reprendre dans le jeu, None) ou (None, raison du refus)."""
    if not re.fullmatch(r"[\w-]+", sid):  # sert dans un glob
        return None, "Identifiant de session invalide."
    if not (path := next((CLAUDE_CONFIG_DIR / "projects").glob(f"*/{sid}.jsonl"), None)):
        return None, "Session introuvable."
    if any(sid in (e.session_id, e.options.resume) for e in employees.values()):
        return None, "Cette session est déjà reprise dans le jeu."
    if sid in live_session_ids():
        return None, "Claude tourne encore sur cette session dans ton terminal : quitte-le d'abord."
    if not (cwd := first_cwd(path)):
        return None, "Dossier de la session inconnu."
    return cwd, None


async def hire(cwd: str, resume: str | None = None) -> Employee:
    e = Employee(len(employees), recruit_name(len(employees)), cwd, resume)
    employees[e.id] = e
    task = e.task = asyncio.create_task(e.run(), name=f"employee-{e.id}")
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
    # Stagiaire (en cours) d'un piloté ou d'une session terminal : transcript de son sous-agent
    owners = [(e.follow(), e.subagents) for e in employees.values()]
    owners += [(w["tail"] and w["tail"].path, w.get("interns", {})) for w in (observer.watched.values() if observer else [])]
    for transcript, interns in owners:
        if tuid := next((t for t, s in interns.items() if s == aid), None):
            f = transcript and subagent_file(transcript, tuid)
            return f if f and f.exists() else "Pas encore de journal pour ce stagiaire."
    w = observer.watched.get(aid) if observer else None
    if not w:
        return "Employé inconnu ou parti (seuls les employés et stagiaires présents ont un historique)."
    return w["tail"].path if w["tail"] else "Pas encore de transcript pour cette session."


def read_history(path: Path) -> list[dict] | str:
    try:
        return chat_history(path, sidechain="subagents" in path.parts)
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
                elif isinstance(target, Employee):
                    target = target.rejects(title) or target
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
            elif kind == "list_sessions":
                sessions = await asyncio.to_thread(resumable_sessions, CLAUDE_CONFIG_DIR, live_session_ids())
                await ws.send_json({"type": "sessions", "sessions": sessions})
            elif kind == "resume":
                sid = str(data.get("session_id") or "")
                cwd, reason = await asyncio.to_thread(resume_target, sid)
                if reason:
                    await ws.send_json({"type": "resume_rejected", "session_id": sid, "reason": reason})
                else:
                    await hire(cwd, resume=sid)
            elif kind == "set_mode":
                aid, mode = str(data.get("agent_id")), data.get("mode")
                e = employees.get(aid)
                if e and mode in MODES:
                    await e.set_mode(mode)
                else:
                    await ws.send_json({"type": "mode_rejected", "agent_id": aid, "reason": (
                        "Mode inconnu." if e else "Seuls les employés pilotés changent de mode depuis le jeu.")})
            elif kind == "dismiss":
                aid = str(data.get("agent_id"))
                if e := employees.get(aid):
                    await e.dismiss()
                elif observer and aid in observer.watched:
                    await observer.hide(aid)
            elif kind == "interrupt":
                aid = str(data.get("agent_id"))
                if e := employees.get(aid):
                    await e.interrupt()
                else:
                    await ws.send_json({"type": "interrupt_rejected", "agent_id": aid,
                                        "reason": "Seuls les employés pilotés peuvent être interrompus depuis le jeu."})
            elif kind == "close_chat":
                hub.chats.get(str(data.get("agent_id")), set()).discard(ws)
            elif kind == "permission_decision":
                rid, allow = str(data.get("request_id")), bool(data.get("allow"))
                fut = hub.pending.get(rid)
                if fut and not fut.done():
                    fut.set_result((allow, data.get("answers")))
                    hub.permissions["total"] += 1
                    hub.permissions["denied"] += not allow
                    # Ferme la popup sur les autres onglets
                    await hub.emit({"type": "permission_resolved", "request_id": rid, "allow": allow})
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)
        for subs in hub.chats.values():
            subs.discard(ws)
