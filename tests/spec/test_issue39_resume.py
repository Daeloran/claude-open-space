"""Tests d'intention pour l'issue #39 : reprendre une session passée (ou Konsole terminée) en employé piloté.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; le bouton « Reprendre dans le jeu » (front) est vérifié à la main.

Contrat public supposé :
- WS `{"type": "resume", "session_id": U}` : si `$CLAUDE_CONFIG_DIR/projects/<dossier>/<U>.jsonl` existe et
  qu'aucune session CLI vivante ne le possède, un employé piloté est recruté : `agent_hired` avec
  `agent.cwd` = cwd de la session (champ `cwd` des enregistrements du transcript), dont `session_id` vaut
  déjà U (`open_chat` renvoie aussitôt l'historique) et dont les options SDK portent `resume=U`
  (`backend.app.employees[id].options.resume == U`).
- Refus `{"type": "resume_rejected", "session_id", "reason"}` à l'émetteur, rien de recruté, quand :
  session_id hors `[\\w-]+` (ex. « ../x »), transcript introuvable, session vivante dans
  `$CLAUDE_CONFIG_DIR/sessions/*.json` (même `sessionId`, pid vivant), ou session déjà reprise dans le jeu.
- WS `{"type": "list_sessions"}` → `{"type": "sessions", "sessions": [{"session_id", "cwd", "project",
  "title", "updated"}]}`, plus récentes d'abord ; `title` = texte du premier prompt (éventuellement tronqué) ;
  une session possédée par un processus CLI vivant est absente ou marquée `"live": true`.

Hypothèses de câblage :
- Vivacité : la session « vivante » utilise le pid du processus de test (vivant pour de vrai) ; en plus,
  `backend.observer.pid_alive` est remplacé et `backend.app.observer` est un `Observer` sur le registre de
  test avec le même `pid_alive` factice. La session « morte » utilise un pid > pid_max (jamais vivant).
- Le SDK est remplacé par `FakeClient` (`backend.app.ClaudeSDKClient`) ; le lifespan ne tourne pas.
"""
import json
import os
import re
import uuid

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import ResultMessage
from fastapi.testclient import TestClient

import backend.app as app_mod
import backend.observer as observer_mod
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until

LIVE_PID = os.getpid()
DEAD_PID = 4242425  # > pid_max Linux : jamais vivant
TS = "2026-09-24T10:00:00Z"


class FakeClient:
    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def connect(self, *a, **k):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt, *a, **k):
        pass

    async def interrupt(self):
        pass

    async def receive_response(self):
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id=str(self.options.resume or "s-fake"), total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1}, result="ok",
        )

    async def get_context_usage(self):
        return {"totalTokens": 1000, "maxTokens": 100000}


class Alive:
    def __init__(self, *pids):
        self.pids = set(pids)

    def __call__(self, pid):
        return pid in self.pids


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = tmp_path / "claude-config"
    (c / "projects").mkdir(parents=True)
    (c / "sessions").mkdir(parents=True)
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", c)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(c))
    return c


@pytest.fixture
def client(cfg, monkeypatch):
    alive = Alive(LIVE_PID)
    monkeypatch.setattr(observer_mod, "pid_alive", alive)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(app_mod, "observer", observer_mod.Observer(cfg, app_mod.hub.emit, pid_alive=alive),
                        raising=False)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        yield c
        c.portal = None


# ---------------------------------------------------------------- données


def rec_prompt(text, cwd, sid):
    return {"type": "user", "isSidechain": False, "timestamp": TS, "cwd": str(cwd), "sessionId": sid,
            "message": {"role": "user", "content": text}}


def rec_answer(text, cwd, sid):
    return {"type": "assistant", "isSidechain": False, "timestamp": TS, "cwd": str(cwd), "sessionId": sid,
            "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 1, "output_tokens": 1}, "stop_reason": "end_turn"}}


def make_session(cfg, tmp_path, first_prompt=None, answer="REPONSE-PASSEE", mtime=None):
    """Transcript d'une session passée dans un nouveau dossier projet ; renvoie (session_id, cwd, path)."""
    sid = str(uuid.uuid4())
    cwd = tmp_path / f"proj-{uuid.uuid4().hex[:8]}"
    cwd.mkdir()
    d = cfg / "projects" / str(cwd).replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    first_prompt = first_prompt or f"PROMPT-PASSE-{sid[:8]}"
    with open(path, "w") as f:
        f.write(json.dumps({"type": "permission-mode", "permissionMode": "default", "sessionId": sid}) + "\n")
        f.write(json.dumps(rec_prompt(first_prompt, cwd, sid)) + "\n")
        f.write(json.dumps(rec_answer(answer, cwd, sid)) + "\n")
        f.write(json.dumps(rec_prompt("PROMPT-SUIVANT", cwd, sid)) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return sid, cwd, path


def register_cli(cfg, pid, sid, cwd):
    (cfg / "sessions" / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "cwd": str(cwd), "entrypoint": "cli", "kind": "interactive", "name": "eter-86",
        "status": "idle", "sessionId": sid, "startedAt": 1790000000000, "version": "2.1.281"}))


def is_(type_, **fields):
    return lambda e: e.get("type") == type_ and all(e.get(k) == v for k, v in fields.items())


def resume(ws, sid):
    ws.send_json({"type": "resume", "session_id": sid})
    return recv_until(ws, lambda e: (e.get("type") == "agent_hired")
                      or (e.get("type") == "resume_rejected" and e.get("session_id") == sid))


def assert_rejected(ev, sid):
    assert ev["type"] == "resume_rejected", ev
    assert ev["session_id"] == sid
    assert isinstance(ev.get("reason"), str) and ev["reason"].strip(), ev


def resumed_employees(sid):
    return [e for e in app_mod.employees.values() if getattr(getattr(e, "options", None), "resume", None) == sid
            or getattr(e, "session_id", None) == sid]


# ---------------------------------------------------------------- reprise acceptée


def test_reprise_d_une_session_terminee_cree_un_employe_pilote(client, cfg, tmp_path):
    sid, cwd, _ = make_session(cfg, tmp_path)
    with connect(client) as ws:
        handshake(ws)
        ev = resume(ws, sid)
    assert ev["type"] == "agent_hired", ev
    assert ev["agent"]["cwd"] == str(cwd)
    emp = app_mod.employees[ev["agent"]["id"]]
    assert emp.options.resume == sid
    assert emp.session_id == sid


def test_l_historique_precedent_est_disponible_aussitot(client, cfg, tmp_path):
    sid, _, _ = make_session(cfg, tmp_path, first_prompt="PROMPT-HISTO", answer="REPONSE-HISTO")
    with connect(client) as ws:
        handshake(ws)
        aid = resume(ws, sid)["agent"]["id"]
        ws.send_json({"type": "open_chat", "agent_id": aid})
        hist = recv_until(ws, is_("chat_history", agent_id=aid))
    assert not hist.get("error"), hist
    texts = [(e["role"], e["kind"], e.get("text", "")) for e in hist["entries"]]
    assert any(r == "user" and "PROMPT-HISTO" in t for r, _, t in texts), texts
    assert any(r == "assistant" and "REPONSE-HISTO" in t for r, _, t in texts), texts


def test_session_konsole_terminee_peut_etre_reprise(client, cfg, tmp_path):
    sid, cwd, _ = make_session(cfg, tmp_path)
    register_cli(cfg, DEAD_PID, sid, cwd)  # entrée restée au registre, processus mort
    with connect(client) as ws:
        handshake(ws)
        ev = resume(ws, sid)
    assert ev["type"] == "agent_hired", ev
    assert app_mod.employees[ev["agent"]["id"]].options.resume == sid


# ---------------------------------------------------------------- refus


def test_refus_si_un_processus_cli_vivant_possede_la_session(client, cfg, tmp_path):
    sid, cwd, _ = make_session(cfg, tmp_path)
    register_cli(cfg, LIVE_PID, sid, cwd)
    before = len(app_mod.employees)
    with connect(client) as ws:
        handshake(ws)
        ev = resume(ws, sid)
        after = drain(ws)
    assert_rejected(ev, sid)
    assert len(app_mod.employees) == before
    assert resumed_employees(sid) == []
    assert not [e for e in after if e.get("type") == "agent_hired"], after


@pytest.mark.parametrize("bad", ["../x", "../../etc/passwd", "a/b", "abc def", "x;rm", ""])
def test_refus_si_session_id_invalide(client, cfg, bad):
    before = len(app_mod.employees)
    with connect(client) as ws:
        handshake(ws)
        ev = resume(ws, bad)
        after = drain(ws)
    assert_rejected(ev, bad)
    assert len(app_mod.employees) == before
    assert not [e for e in after if e.get("type") == "agent_hired"], after


def test_refus_si_transcript_introuvable(client, cfg):
    sid = str(uuid.uuid4())
    before = len(app_mod.employees)
    with connect(client) as ws:
        handshake(ws)
        ev = resume(ws, sid)
    assert_rejected(ev, sid)
    assert len(app_mod.employees) == before


def test_refus_si_session_deja_reprise_dans_le_jeu(client, cfg, tmp_path):
    sid, _, _ = make_session(cfg, tmp_path)
    with connect(client) as ws:
        handshake(ws)
        first = resume(ws, sid)
        assert first["type"] == "agent_hired", first
        before = len(app_mod.employees)
        second = resume(ws, sid)
    assert_rejected(second, sid)
    assert len(app_mod.employees) == before


def test_le_refus_n_est_envoye_qu_a_l_emetteur(client, cfg):
    bad = "../x"
    with connect(client) as other, connect(client) as ws:
        handshake(other)
        handshake(ws)
        assert_rejected(resume(ws, bad), bad)
        seen = drain(other)
    assert not [e for e in seen if e.get("type") == "resume_rejected"], seen


# ---------------------------------------------------------------- liste des sessions reprenables


def list_sessions(ws):
    ws.send_json({"type": "list_sessions"})
    return recv_until(ws, is_("sessions"))["sessions"]


def test_liste_des_sessions_reprenables_plus_recentes_d_abord(client, cfg, tmp_path):
    old = make_session(cfg, tmp_path, first_prompt="PROMPT-ANCIEN", mtime=1_700_000_000)
    new = make_session(cfg, tmp_path, first_prompt="PROMPT-RECENT", mtime=1_790_000_000)
    with connect(client) as ws:
        handshake(ws)
        sessions = list_sessions(ws)
    by_id = {s["session_id"]: s for s in sessions}
    assert old[0] in by_id and new[0] in by_id, sessions
    ids = [s["session_id"] for s in sessions]
    assert ids.index(new[0]) < ids.index(old[0])
    for (sid, cwd, _), title in ((old, "PROMPT-ANCIEN"), (new, "PROMPT-RECENT")):
        s = by_id[sid]
        assert s["cwd"] == str(cwd)
        assert cwd.name in s["project"], s
        assert title in s["title"], s
        assert isinstance(s["updated"], (str, int, float)) and s["updated"] not in ("", None), s
    assert by_id[new[0]]["updated"] != by_id[old[0]]["updated"]


def test_titre_du_premier_prompt_tronque(client, cfg, tmp_path):
    long = "DEBUT-TITRE " + "z" * 5000
    sid, _, _ = make_session(cfg, tmp_path, first_prompt=long)
    with connect(client) as ws:
        handshake(ws)
        (s,) = [s for s in list_sessions(ws) if s["session_id"] == sid]
    assert s["title"].startswith("DEBUT-TITRE")
    assert len(s["title"]) < 1000, len(s["title"])


def test_session_d_un_processus_cli_vivant_absente_ou_marquee_live(client, cfg, tmp_path):
    live_sid, cwd, _ = make_session(cfg, tmp_path)
    register_cli(cfg, LIVE_PID, live_sid, cwd)
    dead_sid, dcwd, _ = make_session(cfg, tmp_path)
    register_cli(cfg, DEAD_PID, dead_sid, dcwd)
    with connect(client) as ws:
        handshake(ws)
        sessions = list_sessions(ws)
    for s in sessions:
        if s["session_id"] == live_sid:
            assert s.get("live") is True, s
    (dead,) = [s for s in sessions if s["session_id"] == dead_sid]
    assert not dead.get("live"), dead


def test_ids_listes_valides_et_reprenables(client, cfg, tmp_path):
    sid, _, _ = make_session(cfg, tmp_path)
    with connect(client) as ws:
        handshake(ws)
        sessions = list_sessions(ws)
        assert all(re.fullmatch(r"[\w-]+", s["session_id"]) for s in sessions), sessions
        ev = resume(ws, sid)
    assert ev["type"] == "agent_hired", ev
