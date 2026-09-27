"""Tests d'intention pour l'issue #23 : répondre depuis l'Open Space aux demandes de permission d'un terminal.

Écrits en boîte noire depuis le commentaire « Revised scope » de l'issue (qui remplace les critères de la
description), sans lire le corps des fonctions de backend/.
Rien de réel : pas de D-Bus, pas de vrai /proc, pas de vrai terminal, pas de vrai ~/.claude.

Contrat public supposé :
- `backend/konsole.py` : `async send_keys(pid, keys: str) -> str | None` — même garde que `send_prompt`
  (onglet trouvé via `PROC_ROOT/<pid>/environ`, `foregroundProcessId` == pid vérifié avant tout envoi),
  puis `sendText(keys)` tel quel : pas de `\\r` ajouté, pas de nettoyage (Échap passe). None si envoyé,
  sinon une raison (texte non vide), jamais d'exception. Mêmes points de simulation que #21 :
  `PROC_ROOT` et `dbus_call(service, path, method, *args)` monkeypatchés.
- Détection : une session terminal observée passe à `status: "waiting"` + `waitingFor: "permission prompt"`
  dans `sessions/<pid>.json` → un seul `permission_request` `{request_id, agent_id, tool, summary}`,
  `tool`/`summary` venant du dernier `tool_use` du transcript sans `tool_result`. Aucune demande pour un
  autre `waitingFor`, pour `AskUserQuestion` / `ExitPlanMode`, ni sans `tool_use` en attente.
- `permission_decision {request_id, allow}` (existant) : allow → `sendText("1")`, refus → `sendText("\\x1b")` ;
  `permission_resolved {request_id, allow}` émis (format de #4).
- La session quitte « permission prompt » (statut changé, `tool_result` écrit, session disparue) pendant
  que la demande est ouverte → `permission_resolved`, rien d'envoyé.
- Au moment d'envoyer : foreground != pid, ou fichier de session plus en `waiting`/`permission prompt`
  → rien d'envoyé.
- L'écran n'est jamais lu (aucun appel D-Bus `getDisplayedText`, ni rien d'autre que
  `foregroundProcessId` / `sendText`).

Câblage de test (comme #35 / #40) : la boucle `backend.app.observe_terminal(observer, 0.05)` tourne dans
le portail avec un `Observer(cfg, hub.emit, pid_alive=<factice>)` affecté à `backend.app.observer`
(le lifespan ne tourne pas) ; `backend.app.CLAUDE_CONFIG_DIR` pointe sur un faux ~/.claude dans tmp_path.
Pour vérifier la garde « au moment d'envoyer », la boucle est arrêtée après réception du
`permission_request` puis le fichier de session est modifié : seule la relecture à l'envoi peut l'empêcher.
Attentes bornées à 3 s.
"""
import asyncio
import json
import uuid

import pytest

import backend.app as app_mod
from tests.spec.test_issue21_konsole_send import FG, SEND, SESSION, SERVICE, FakeDBus, konsole_env, write_environ
from tests.spec.test_issue25_chat import (  # noqa: F401
    Alive, append, assistant, cfg, client, connect, drain, handshake, no_subprocess, prompt, recv_until,
    tool_result, tool_use_block, transcript_path,
)

PID = 4242423  # pid fictif : jamais celui d'une vraie session
PERM = "permission prompt"
CMD = "rm -rf build-CANARI-23"
ALLOWED_METHODS = {FG, SEND}


def run(coro):
    return asyncio.run(coro)


def write_session(cfg, sid, cwd, status="busy", waiting_for=None):
    rec = {"pid": PID, "cwd": str(cwd), "entrypoint": "cli", "kind": "interactive", "name": "eter-23",
           "status": status, "sessionId": sid, "startedAt": 1790000000000, "version": "2.1.281"}
    if waiting_for is not None:
        rec["waitingFor"] = waiting_for
    (cfg / "sessions" / f"{PID}.json").write_text(json.dumps(rec))


# ---------------------------------------------------------------- Konsole simulée


@pytest.fixture
def konsole(tmp_path, monkeypatch, no_subprocess):
    import backend.konsole as m  # import tardif : échec par test, pas à la collecte

    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(m, "PROC_ROOT", proc)
    m.fake = FakeDBus(foreground=PID)
    monkeypatch.setattr(m, "dbus_call", m.fake)
    m.proc = proc
    write_environ(proc, PID, **konsole_env())
    yield m
    methods = {c[2] for c in m.fake.calls}
    assert methods <= ALLOWED_METHODS, f"l'écran ne doit jamais être lu : {methods - ALLOWED_METHODS}"


# ---------------------------------------------------------------- send_keys (unitaire)


@pytest.mark.parametrize("keys", ["1", "\x1b"])
def test_send_keys_envoie_tel_quel_sans_entree(konsole, keys):
    assert run(konsole.send_keys(PID, keys)) is None
    assert konsole.fake.sent() == [(keys,)]
    for service, path, _, _ in konsole.fake.calls:
        assert (service, path) == (SERVICE, SESSION)
    methods = [c[2] for c in konsole.fake.calls]
    assert methods.index(FG) < methods.index(SEND), "foreground vérifié avant tout envoi"


@pytest.mark.parametrize("fg", [PID + 1, 1])
def test_send_keys_foreground_different_rien_envoye(konsole, fg):
    konsole.fake.foreground = fg
    reason = run(konsole.send_keys(PID, "1"))
    assert isinstance(reason, str) and reason
    assert konsole.fake.sent() == []


def test_send_keys_session_non_konsole(konsole):
    write_environ(konsole.proc, PID, HOME="/home/x")
    reason = run(konsole.send_keys(PID, "1"))
    assert isinstance(reason, str) and reason
    assert konsole.fake.sent() == []


def test_send_keys_dbus_en_erreur_raison_sans_exception(konsole):
    konsole.fake.error = RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")
    reason = run(konsole.send_keys(PID, "1"))
    assert isinstance(reason, str) and reason
    assert konsole.fake.sent() == []


# ---------------------------------------------------------------- intégration : boucle de l'observateur


@pytest.fixture
def loop(client, cfg, konsole, monkeypatch):
    """Boucle de l'observateur comme en prod ; `loop.stop()` la fige (garde « au moment d'envoyer »)."""
    from backend.observer import Observer

    obs = Observer(cfg, app_mod.hub.emit, pid_alive=Alive(PID))
    monkeypatch.setattr(app_mod, "observer", obs, raising=False)

    async def _start():
        return asyncio.create_task(app_mod.observe_terminal(obs, 0.05))

    task = client.portal.call(_start)

    class Handle:
        @staticmethod
        def stop():
            client.portal.call(lambda: task.cancel())
            drain_portal(client)

    yield Handle
    client.portal.call(lambda: task.cancel())


def drain_portal(client):
    async def _tick():
        await asyncio.sleep(0.1)

    client.portal.call(_tick)


class Terminal:
    """Session Claude Code d'un onglet Konsole, observée par la boucle."""

    def __init__(self, ws, cfg, proj):
        self.ws, self.cfg, self.proj = ws, cfg, proj
        self.sid = str(uuid.uuid4())
        self.id = "o-" + self.sid[:8]
        self.path = transcript_path(cfg, self.sid, proj)
        append(self.path, prompt("nettoie le build"))
        write_session(cfg, self.sid, proj)
        recv_until(ws, lambda e: e.get("type") == "observed_joined" and e["agent"]["id"] == self.id)

    def write(self, *records):
        append(self.path, *records)

    def status(self, status, waiting_for=None):
        write_session(self.cfg, self.sid, self.proj, status, waiting_for)

    def pending_bash(self, tid="toolu_bash"):
        self.write(assistant([tool_use_block(tid, "Bash", {"command": CMD})]))
        self.status("waiting", PERM)

    def request(self, seen=None):
        return recv_until(self.ws, lambda e: e.get("type") == "permission_request"
                          and e.get("agent_id") == self.id, seen=seen)


@pytest.fixture
def term(client, cfg, tmp_path, loop):
    proj = tmp_path / "eter"
    proj.mkdir(exist_ok=True)
    made = []

    def make(ws):
        t = Terminal(ws, cfg, proj)
        made.append(t)
        return t

    yield make
    for t in made:
        client.portal.call(app_mod.hub.emit, {"type": "observed_left", "agent_id": t.id})


def requests_for(seen, t):
    return [e for e in seen if e.get("type") == "permission_request" and e.get("agent_id") == t.id]


def resolved(seen, rid):
    return [e for e in seen if e.get("type") == "permission_resolved" and e.get("request_id") == rid]


def decide(ws, rid, allow):
    ws.send_json({"type": "permission_decision", "request_id": rid, "allow": allow})


# ---------------------------------------------------------------- détection


def test_permission_prompt_emet_une_demande_avec_l_outil_du_transcript(client, konsole, term):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        t = term(ws)
        t.write(assistant([tool_use_block("toolu_read", "Read", {"file_path": "/tmp/x.py"})]),
                tool_result("contenu", tid="toolu_read"))
        t.pending_bash()
        ev = t.request(seen)
        drain(ws, seen, timeout=0.5)  # plusieurs tours de boucle, fichier toujours en attente
    assert ev["tool"] == "Bash"
    assert "CANARI-23" in ev["summary"]
    assert isinstance(ev["request_id"], str) and ev["request_id"]
    assert len(requests_for(seen, t)) == 1, "émis une seule fois"
    assert konsole.fake.sent() == [], "rien n'est tapé sans décision"


@pytest.mark.parametrize("other", ["input needed", "dialog open"])
def test_autre_attente_pas_de_demande(client, konsole, term, other):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        t = term(ws)
        t.write(assistant([tool_use_block("toolu_bash", "Bash", {"command": CMD})]))
        t.status("waiting", other)
        drain(ws, seen, timeout=0.5)
        assert requests_for(seen, t) == [], other
        t.status("waiting", PERM)  # témoin : la même session en permission produit bien une demande
        assert t.request()["tool"] == "Bash"


@pytest.mark.parametrize("tool,inp", [
    ("AskUserQuestion", {"questions": [{"question": "Quelle base ?", "header": "DB", "multiSelect": False,
                                        "options": [{"label": "A", "description": "a"}]}]}),
    ("ExitPlanMode", {"plan": "1. faire\n2. tester"}),
])
def test_ask_user_question_et_exit_plan_mode_pas_de_demande(client, konsole, term, tool, inp):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        t = term(ws)
        t.write(assistant([tool_use_block("toolu_dialog", tool, inp)]))
        t.status("waiting", PERM)
        drain(ws, seen, timeout=0.5)
        assert requests_for(seen, t) == [], tool
        t.status("busy")
        t.write(tool_result("ok", tid="toolu_dialog"))
        t.pending_bash()  # témoin
        assert t.request()["tool"] == "Bash"


def test_sans_tool_use_en_attente_pas_de_demande(client, konsole, term):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        t = term(ws)
        t.write(assistant([tool_use_block("toolu_done", "Bash", {"command": "ls"})]),
                tool_result("ok", tid="toolu_done"))
        t.status("waiting", PERM)
        drain(ws, seen, timeout=0.5)
        assert requests_for(seen, t) == []
        t.status("busy")
        t.pending_bash()  # témoin
        assert t.request()["tool"] == "Bash"


# ---------------------------------------------------------------- décision → touche dans l'onglet


@pytest.mark.parametrize("allow,key", [(True, "1"), (False, "\x1b")])
def test_decision_envoie_la_touche_et_resout(client, konsole, term, allow, key):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        t = term(ws)
        t.pending_bash()
        rid = t.request()["request_id"]
        decide(ws, rid, allow)
        res = recv_until(ws, lambda e: e.get("type") == "permission_resolved"
                         and e.get("request_id") == rid, seen=seen)
        drain(ws, seen)
    assert res["allow"] is allow
    assert konsole.fake.sent() == [(key,)]
    methods = [c[2] for c in konsole.fake.calls]
    assert methods.index(FG) < methods.index(SEND), "foreground vérifié avant l'envoi"
    for service, path, _, _ in konsole.fake.calls:
        assert (service, path) == (SERVICE, SESSION)


# ---------------------------------------------------------------- réponse donnée ailleurs


def _leave_status(t):
    t.status("busy")


def _leave_tool_result(t):
    t.write(tool_result("ok", tid="toolu_bash"))
    t.status("busy")


def _leave_gone(t):
    (t.cfg / "sessions" / f"{PID}.json").unlink()


@pytest.mark.parametrize("leave", [_leave_status, _leave_tool_result, _leave_gone],
                         ids=["repondu_au_terminal", "tool_result", "session_partie"])
def test_sortie_du_permission_prompt_resout_sans_rien_envoyer(client, konsole, term, leave):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        t = term(ws)
        t.pending_bash()
        rid = t.request()["request_id"]
        leave(t)
        recv_until(ws, lambda e: e.get("type") == "permission_resolved" and e.get("request_id") == rid, seen=seen)
        decide(ws, rid, True)  # décision tardive depuis un autre onglet : ignorée
        drain(ws, seen)
    assert konsole.fake.sent() == []


# ---------------------------------------------------------------- gardes au moment d'envoyer


@pytest.mark.parametrize("fg", [PID + 1, 1])
def test_foreground_different_rien_envoye(client, konsole, term, fg):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        t = term(ws)
        t.pending_bash()
        rid = t.request()["request_id"]
        konsole.fake.foreground = fg  # un autre programme au premier plan de l'onglet
        decide(ws, rid, True)
        drain(ws, seen, timeout=0.5)
    assert konsole.fake.sent() == []


@pytest.mark.parametrize("new_status,waiting_for", [("busy", None), ("waiting", "input needed"), ("idle", None)])
def test_fichier_de_session_plus_en_permission_rien_envoye(client, konsole, term, loop, new_status, waiting_for):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        t = term(ws)
        t.pending_bash()
        rid = t.request()["request_id"]
        loop.stop()  # l'observateur ne voit plus le changement : seule la relecture à l'envoi compte
        t.status(new_status, waiting_for)
        decide(ws, rid, True)
        drain(ws, seen, timeout=0.5)
    assert konsole.fake.sent() == []
