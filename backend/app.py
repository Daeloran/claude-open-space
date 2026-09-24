"""L'Open Space : pilote des sessions Claude Code (Agent SDK) et diffuse des événements de jeu au front.

Chaque « employé » est une session ClaudeSDKClient persistante : son contexte grossit
d'un ticket à l'autre, d'où la jauge de fatigue et les pauses café (compaction).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .events import deliverable_for, summarize_tool

WORKDIR = os.environ.get("OPENSPACE_CWD", os.getcwd())
TEAM = [n.strip() for n in os.environ.get("OPENSPACE_TEAM", "Léa,Hugo,Inès").split(",") if n.strip()]
CONTEXT_WINDOW = int(os.environ.get("OPENSPACE_CONTEXT", "200000"))
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
# Outils sans risque : pas de passage par le bureau du manager
AUTO_TOOLS = ["Read", "Glob", "Grep", "LS", "TodoWrite", "WebSearch", "Task"]
INTERN_NAMES = ["Tom", "Chloé", "Malik", "Jade", "Noé", "Zoé"]


@dataclass
class Ticket:
    id: str
    title: str


class Hub:
    """Diffusion WebSocket, file de tickets et validations en attente."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.tickets: asyncio.Queue[Ticket] = asyncio.Queue()
        self.ticket_seq = 0

    async def emit(self, event: dict) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                self.clients.discard(ws)


hub = Hub()


class Employee:
    def __init__(self, idx: int, name: str) -> None:
        self.id = f"e{idx}"
        self.name = name
        self.subagents: dict[str, str] = {}   # tool_use_id du Task -> id du stagiaire
        self.tool_names: dict[str, str] = {}  # tool_use_id -> nom de l'outil
        self.intern_seq = 0
        self.options = ClaudeAgentOptions(
            cwd=WORKDIR,
            allowed_tools=AUTO_TOOLS,
            permission_mode="acceptEdits",  # les éditions passent, Bash et le reste demandent
            can_use_tool=self.can_use_tool,
        )

    def actor(self, msg) -> str:
        parent = getattr(msg, "parent_tool_use_id", None)
        return self.subagents.get(parent, self.id) if parent else self.id

    async def can_use_tool(self, tool_name, input_data, context):
        rid = uuid.uuid4().hex[:8]
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        hub.pending[rid] = fut
        await hub.emit({
            "type": "permission_request", "request_id": rid, "agent_id": self.id,
            "tool": tool_name, "summary": summarize_tool(tool_name, input_data),
        })
        try:
            allow = await fut
        finally:
            hub.pending.pop(rid, None)
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
                ticket = await hub.tickets.get()
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
                    hub.tickets.task_done()

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
        elif isinstance(msg, SystemMessage):
            if getattr(msg, "subtype", "") == "compact_boundary":
                await hub.emit({"type": "compaction", "agent_id": self.id})
        elif isinstance(msg, ResultMessage):
            usage = msg.usage or {}
            get = lambda k: int(usage.get(k, 0) or 0)  # noqa: E731
            ctx_tokens = get("input_tokens") + get("cache_read_input_tokens") + get("cache_creation_input_tokens")
            tokens = ctx_tokens + get("output_tokens")
            cost = float(msg.total_cost_usd or 0)
            await hub.emit({"type": "cost", "agent_id": self.id, "usd": cost, "tokens": tokens})
            return cost, tokens
        return 0.0, 0

    async def refresh_context(self, client) -> None:
        """Fatigue = remplissage réel du contexte de la session (maxTokens : limite effective avant compaction)."""
        try:
            usage = await client.get_context_usage()
            ratio = usage["totalTokens"] / (usage.get("maxTokens") or CONTEXT_WINDOW)
        except Exception:
            return  # mesure impossible : on garde la dernière valeur
        await hub.emit({"type": "context", "agent_id": self.id, "ratio": min(1.0, max(0.0, ratio))})


employees = [Employee(i, n) for i, n in enumerate(TEAM)]


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(e.run(), name=f"employee-{e.id}") for e in employees]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="L'Open Space", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub.clients.add(ws)
    await ws.send_json({"type": "hello", "team": [{"id": e.id, "name": e.name} for e in employees]})
    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            if kind == "new_ticket" and str(data.get("title", "")).strip():
                hub.ticket_seq += 1
                ticket = Ticket(f"t{hub.ticket_seq}", str(data["title"]).strip())
                await hub.emit({"type": "ticket_created", "ticket": {"id": ticket.id, "title": ticket.title}})
                await hub.tickets.put(ticket)
            elif kind == "permission_decision":
                fut = hub.pending.get(str(data.get("request_id")))
                if fut and not fut.done():
                    fut.set_result(bool(data.get("allow")))
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)
