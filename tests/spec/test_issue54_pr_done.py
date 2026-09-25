"""Tests d'intention pour l'issue #54 : un ticket passe à « livré » dès qu'une PR/MR est créée.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; le compteur « Tickets livrés » du front est vérifié via les
événements `ticket_done` (un seul par ticket) et le `snapshot`.

Contrat public supposé :
- Employé piloté : pendant son ticket, un `AssistantMessage` contenant un
  `ToolUseBlock(name="Bash", input={"command": "gh pr create ..."})` (ou `glab mr create ...`)
  suivi d'un `UserMessage` avec `ToolResultBlock(tool_use_id=<même id>, is_error=False)`
  → `{"type": "ticket_done", "ticket_id", "ok": True, ...}` émis tout de suite, avant les
  événements des messages suivants du même tour. La fin du tour (`ResultMessage`) n'émet plus
  rien pour ce ticket.
- Résultat en erreur (`is_error=True`) : pas de clôture anticipée ; un seul `ticket_done` en fin de tour.
- Ticket sans commande PR : un seul `ticket_done` en fin de tour (comportement actuel).
- Employé terminal avec un ticket envoyé depuis le jeu (Konsole, cf. #21) : mêmes règles à partir
  du transcript lu par l'`Observer` (ligne assistant `tool_use` Bash `gh pr create`, puis ligne
  `tool_result` non en erreur) → `ticket_done` une fois, même si la session est encore busy ;
  le `end_turn`/idle suivant n'en émet pas un second.
- Le `snapshot` liste le ticket une seule fois, statut `done` ; le ticket suivant du même employé
  est traité normalement.

Hypothèses : les événements d'un `tool_use` ultérieur (`{"type": "tool_use", "agent_id", "tool"}`)
sont émis sur /ws pendant le tour ; l'`Observer` du terminal émet via `backend.app.hub.emit`
(câblage de test_issue35_piloted_chat) ; les lignes du transcript portent un `timestamp` ISO
postérieur à l'envoi du ticket, comme la CLI réelle.

Harnais : faux SDK façon test_issue41_todos (le prompt choisit le script renvoyé), faux Konsole
façon test_issue21_konsole_send, faux ~/.claude façon test_issue13_observer.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage
from fastapi.testclient import TestClient

import backend.app as app_mod
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until
from tests.spec.test_issue13_observer import assistant as base_assistant
from tests.spec.test_issue13_observer import (
    Alive, aid, append, new_sid, transcript_path, write_session,
)
from tests.spec.test_issue21_konsole_send import PID, FakeDBus, konsole_env, write_environ

GH = "gh pr create --fill"
GLAB = "glab mr create --fill"

# titre du ticket -> (commande Bash, is_error du résultat) ; absent = pas de commande
SCRIPTS: dict[str, tuple[str, bool]] = {}


def _id():
    return f"toolu_{uuid.uuid4().hex[:8]}"


class FakeClient:
    def __init__(self, options):
        self.prompts: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def connect(self, *a, **k):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt, *a, **k):
        self.prompts.append(prompt if isinstance(prompt, str) else str(prompt))

    async def receive_response(self):
        prompt = self.prompts[-1] if self.prompts else ""
        for key, (cmd, is_error) in SCRIPTS.items():
            if key in prompt:
                tid = _id()
                yield AssistantMessage(
                    content=[ToolUseBlock(id=tid, name="Bash", input={"command": cmd})], model="claude-opus-5")
                yield UserMessage(content=[ToolResultBlock(
                    tool_use_id=tid, content="https://github.com/o/r/pull/7", is_error=is_error)])
                await asyncio.sleep(0.05)
                # travail postérieur à la PR : ses événements doivent suivre le ticket_done
                later = _id()
                yield AssistantMessage(
                    content=[ToolUseBlock(id=later, name="Read", input={"file_path": "/tmp/APRES-PR"})],
                    model="claude-opus-5")
                yield UserMessage(content=[ToolResultBlock(tool_use_id=later, content="x", is_error=False)])
        await asyncio.sleep(0)
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s-fake", total_cost_usd=0.01,
            usage={"input_tokens": 1, "output_tokens": 1}, result="ok",
        )

    async def get_context_usage(self):
        return {"totalTokens": 1000, "maxTokens": 100000}


@pytest.fixture
def client(tmp_path, monkeypatch):
    c_dir = tmp_path / "claude-config"
    (c_dir / "projects").mkdir(parents=True)
    (c_dir / "sessions").mkdir()
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", c_dir)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        c.config_dir = c_dir
        yield c
        c.portal = None


def is_(type_, **fields):
    return lambda e: e.get("type") == type_ and all(e.get(k) == v for k, v in fields.items())


def piloted_ticket(ws, tmp_path, script=None, aid_=None):
    """Envoie un ticket piloté ; renvoie (ticket_id, agent_id, événements jusqu'au calme)."""
    title = f"pr-{uuid.uuid4().hex}"
    if script is not None:
        SCRIPTS[title] = script
    msg = {"type": "new_ticket", "title": title}
    if aid_ is None:
        cwd = tmp_path / f"proj-{uuid.uuid4().hex[:8]}"
        cwd.mkdir()
        msg["cwd"] = str(cwd)
    else:
        msg["agent_id"] = aid_
    seen: list[dict] = []
    ws.send_json(msg)
    tid = recv_until(ws, lambda e: e.get("type") == "ticket_created"
                     and e["ticket"]["title"] == title, seen=seen)["ticket"]["id"]
    assigned = recv_until(ws, is_("ticket_assigned", ticket_id=tid), seen=seen)
    recv_until(ws, is_("ticket_done", ticket_id=tid), seen=seen)
    seen += drain(ws)  # fin du tour : un éventuel second ticket_done arriverait ici
    return tid, assigned["agent_id"], seen


def dones(seen, tid):
    return [e for e in seen if e.get("type") == "ticket_done" and e.get("ticket_id") == tid]


def index_of(seen, pred):
    return next(i for i, e in enumerate(seen) if pred(e))


def later_tool(aid_):
    return lambda e: e.get("type") == "tool_use" and e.get("agent_id") == aid_ and e.get("tool") == "Read"


def snapshot_tickets(client, tid):
    with connect(client) as ws:
        _, snap = handshake(ws)
    return [t for t in snap["tickets"] if t["id"] == tid]


# ---------------------------------------------------------------- employé piloté


@pytest.mark.parametrize("cmd", [GH, GLAB])
def test_pr_reussie_clot_le_ticket_avant_la_suite_du_tour(client, tmp_path, cmd):
    with connect(client) as ws:
        handshake(ws)
        tid, aid_, seen = piloted_ticket(ws, tmp_path, (cmd, False))
    d = dones(seen, tid)
    assert len(d) == 1, seen
    assert d[0].get("ok") is True, d
    assert any(later_tool(aid_)(e) for e in seen), "le faux SDK a bien joué la suite du tour"
    assert index_of(seen, is_("ticket_done", ticket_id=tid)) < index_of(seen, later_tool(aid_)), seen


@pytest.mark.parametrize("cmd", [GH, GLAB])
def test_pr_reussie_ticket_compte_une_fois_et_suivant_normal(client, tmp_path, cmd):
    with connect(client) as ws:
        handshake(ws)
        tid, aid_, _ = piloted_ticket(ws, tmp_path, (cmd, False))
        tid2, aid2, seen2 = piloted_ticket(ws, tmp_path, aid_=aid_)
    assert aid2 == aid_
    assert len(dones(seen2, tid2)) == 1, seen2
    assert dones(seen2, tid) == [], "aucun ticket_done tardif pour le ticket déjà livré"
    tickets = snapshot_tickets(client, tid)
    assert len(tickets) == 1 and tickets[0]["status"] == "done", tickets
    assert snapshot_tickets(client, tid2)[0]["status"] == "done"


def test_pr_en_erreur_ne_clot_qu_en_fin_de_tour(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        tid, aid_, seen = piloted_ticket(ws, tmp_path, (GH, True))
    d = dones(seen, tid)
    assert len(d) == 1, seen
    assert index_of(seen, is_("ticket_done", ticket_id=tid)) > index_of(seen, later_tool(aid_)), seen


def test_ticket_sans_pr_clot_en_fin_de_tour(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        tid, _, seen = piloted_ticket(ws, tmp_path)
    d = dones(seen, tid)
    assert len(d) == 1, seen
    assert d[0].get("ok") is True, d


# ---------------------------------------------------------------- employé terminal (Konsole + transcript)


def ts():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stamped(rec):
    return {**rec, "timestamp": ts()}


def bash_use(tid, cmd):
    return stamped(assistant([{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}],
                             stop="tool_use"))


def bash_result(tid, is_error=False):
    return stamped({"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
         "content": "https://github.com/o/r/pull/7"}]}})


def end_turn():
    return stamped(assistant([{"type": "text", "text": "PR ouverte."}], stop="end_turn"))


def assistant(content, stop=None):
    rec = base_assistant(content)
    if stop:
        rec["message"]["stop_reason"] = stop
    return rec


@pytest.fixture
def terminal(client, tmp_path, monkeypatch):
    """Session terminal observée (pid PID, onglet Konsole factice) ; l'Observer émet vers le hub."""
    import backend.konsole as kmod
    from backend.observer import Observer

    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(kmod, "PROC_ROOT", proc)
    monkeypatch.setattr(kmod, "dbus_call", FakeDBus())
    write_environ(proc, PID, **konsole_env())

    cfg = client.config_dir
    proj = tmp_path / "eter"
    proj.mkdir()
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, stamped({"type": "user", "message": {"role": "user", "content": "salut"}}))
    write_session(cfg, PID, sid, proj, status="idle")
    alive = Alive(PID)
    obs = Observer(cfg, app_mod.hub.emit, pid_alive=alive)
    monkeypatch.setattr(app_mod, "observer", obs, raising=False)  # comme en prod (cf. #35)

    class T:
        pass

    term = T()
    term.cfg, term.sid, term.proj, term.transcript, term.aid = cfg, sid, proj, t, aid(sid)
    term.poll = lambda: client.portal.call(obs.poll)
    term.status = lambda s: write_session(cfg, PID, sid, proj, status=s)
    term.poll()
    yield term
    alive.pids.clear()
    (cfg / "sessions" / f"{PID}.json").unlink()
    try:
        term.poll()
    except Exception:
        pass


def send_terminal_ticket(ws, term):
    title = f"pr-{uuid.uuid4().hex}"
    seen: list[dict] = []
    ws.send_json({"type": "new_ticket", "title": title, "agent_id": term.aid})
    tid = recv_until(ws, lambda e: e.get("type") in ("ticket_created", "ticket_rejected")
                     and (e.get("type") == "ticket_rejected" or e["ticket"]["title"] == title),
                     seen=seen)
    assert tid.get("type") == "ticket_created", tid
    tid = tid["ticket"]["id"]
    recv_until(ws, is_("ticket_assigned", ticket_id=tid), seen=seen)
    return tid, title, seen


def test_terminal_pr_reussie_clot_le_ticket_une_fois(client, terminal):
    with connect(client) as ws:
        handshake(ws)
        tid, title, seen = send_terminal_ticket(ws, terminal)
        terminal.status("busy")
        append(terminal.transcript, stamped({"type": "user", "message": {"role": "user", "content": title}}))
        terminal.poll()
        seen += drain(ws)
        assert dones(seen, tid) == [], "busy seul ne clôt pas le ticket"

        use = _id()
        append(terminal.transcript, bash_use(use, GH), bash_result(use))
        terminal.poll()  # session toujours busy, pas de end_turn
        done = recv_until(ws, is_("ticket_done", ticket_id=tid), seen=seen)
        assert done.get("ok", True) is True, done

        append(terminal.transcript, end_turn())
        terminal.status("idle")
        terminal.poll()
        seen += drain(ws)
    assert len(dones(seen, tid)) == 1, seen
    tickets = snapshot_tickets(client, tid)
    assert len(tickets) == 1 and tickets[0]["status"] == "done", tickets


def test_terminal_pr_en_erreur_attend_la_fin_du_tour(client, terminal):
    with connect(client) as ws:
        handshake(ws)
        tid, title, seen = send_terminal_ticket(ws, terminal)
        terminal.status("busy")
        append(terminal.transcript, stamped({"type": "user", "message": {"role": "user", "content": title}}))
        use = _id()
        append(terminal.transcript, bash_use(use, GH), bash_result(use, is_error=True))
        terminal.poll()
        seen += drain(ws)
        assert dones(seen, tid) == [], seen
        append(terminal.transcript, end_turn())
        terminal.status("idle")
        terminal.poll()
        recv_until(ws, is_("ticket_done", ticket_id=tid), seen=seen)
        seen += drain(ws)
    assert len(dones(seen, tid)) == 1, seen
