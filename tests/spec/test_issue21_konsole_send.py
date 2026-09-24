"""Tests d'intention pour l'issue #21 : donner une tâche à une session terminal (Konsole).

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Rien de réel : pas de D-Bus, pas de vrai /proc, aucun texte envoyé à un vrai terminal.

Contrat public supposé — nouveau module `backend/konsole.py` :
- `PROC_ROOT: Path` (défaut `/proc`), monkeypatchable.
- `locate(pid) -> tuple[str, str] | None` : lit `PROC_ROOT/<pid>/environ` (octets séparés par `\\0`)
  et renvoie `(KONSOLE_DBUS_SERVICE, KONSOLE_DBUS_SESSION)`, ou None si l'une manque / fichier
  illisible. Aucune autre variable n'est exposée.
- `async dbus_call(service, path, method, *args: str) -> str` : point d'appel D-Bus unique, résolu
  à l'exécution (monkeypatché ici). Méthodes : `org.kde.konsole.Session.foregroundProcessId`
  (renvoie p. ex. `"(13192,)"`) et `org.kde.konsole.Session.sendText` (un argument texte).
  `path` = valeur de `KONSOLE_DBUS_SESSION`, `service` = valeur de `KONSOLE_DBUS_SERVICE`.
- `sanitize(text) -> str` : retire les caractères Unicode de catégorie Cc sauf `\\n` et `\\t`.
- `async send_prompt(pid, text) -> str | None` : None si envoyé, sinon une raison (texte non vide).
  Une ligne → `sendText(texte)` puis `sendText("\\r")` ; multi-ligne →
  `sendText("\\x1b[200~" + texte + "\\x1b[201~")` puis `sendText("\\r")` (texte = sanitize(text)).
  Aucun `sendText` si le foreground != pid. Erreur D-Bus → raison, pas d'exception.

Intégration app (hypothèses ajoutées) :
- l'agent d'un `observed_joined` porte son `pid` ; un employé observé injecté via `hub.emit`
  suffit à le rendre destinataire de tickets.
- `new_ticket` vers cet agent : envoi OK → `ticket_created` puis `ticket_assigned` (agent_id) ;
  échec → `ticket_rejected` avec `reason`, sans `ticket_created`.
- Un `observed_status` busy puis idle émis via `hub.emit` pour cet agent → `ticket_done` du ticket.
"""

import asyncio
import json
import subprocess
import uuid

import anyio
import anyio.from_thread
import pytest
from fastapi.testclient import TestClient

import backend.app as app_mod

ORIGIN = {"origin": "http://testserver"}
TIMEOUT = 3
PID = 4242421  # pid fictif : jamais celui d'une vraie session
SERVICE = "org.kde.konsole-99999"
SESSION = "/Sessions/7"
SEND = "org.kde.konsole.Session.sendText"
FG = "org.kde.konsole.Session.foregroundProcessId"
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"


# ---------------------------------------------------------------- simulation /proc + D-Bus


def write_environ(root, pid, **env):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


def konsole_env(**extra):
    return {"HOME": "/home/x", "SECRET_TOKEN": "xyz-very-secret", "KONSOLE_DBUS_SERVICE": SERVICE,
            "KONSOLE_DBUS_SESSION": SESSION, "PATH": "/usr/bin", **extra}


class FakeDBus:
    def __init__(self, foreground=PID, error=None):
        self.foreground = foreground
        self.error = error
        self.calls = []

    async def __call__(self, service, path, method, *args):
        self.calls.append((service, path, method, args))
        if self.error:
            raise self.error
        if method == FG:
            return f"({self.foreground},)"
        return ""

    def sent(self):
        return [c[3] for c in self.calls if c[2] == SEND]


@pytest.fixture
def konsole(tmp_path, monkeypatch):
    import backend.konsole as m  # import tardif : échec par test, pas à la collecte

    def forbidden(*a, **k):
        raise AssertionError("aucun sous-processus réel dans les tests")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(m, "PROC_ROOT", proc)
    m.fake = FakeDBus()
    monkeypatch.setattr(m, "dbus_call", m.fake)
    m.proc = proc
    return m


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- locate


def test_locate_trouve_l_onglet_sans_exposer_le_reste(konsole):
    write_environ(konsole.proc, PID, **konsole_env())
    loc = konsole.locate(PID)
    assert loc == (SERVICE, SESSION)
    assert "xyz" not in repr(loc) and "SECRET" not in repr(loc)


@pytest.mark.parametrize("missing", ["KONSOLE_DBUS_SERVICE", "KONSOLE_DBUS_SESSION"])
def test_locate_variable_absente(konsole, missing):
    env = konsole_env()
    del env[missing]
    write_environ(konsole.proc, PID, **env)
    assert konsole.locate(PID) is None


def test_locate_environ_absent(konsole):
    assert konsole.locate(PID) is None


def test_locate_environ_illisible(konsole):
    write_environ(konsole.proc, PID, **konsole_env())
    (konsole.proc / str(PID) / "environ").chmod(0)
    try:
        assert konsole.locate(PID) is None
    finally:
        (konsole.proc / str(PID) / "environ").chmod(0o600)


def test_locate_environ_binaire_sans_crash(konsole):
    d = konsole.proc / str(PID)
    d.mkdir()
    (d / "environ").write_bytes(b"\xff\xfe\0=\0NOEQUAL\0KONSOLE_DBUS_SERVICE=" + SERVICE.encode() + b"\0")
    assert konsole.locate(PID) is None


# ---------------------------------------------------------------- sanitize


def test_sanitize_retire_les_controles_garde_newline_et_tab():
    import backend.konsole as m
    assert m.sanitize("a\x1b[31mb\x07c\x00d\re\x7ff\x9bg\n\th") == "a[31mbcdefg\n\th"


def test_sanitize_texte_normal_intact():
    import backend.konsole as m
    s = "Corrige le bug #12 : « émoji » 🚀, chemins /tmp/x"
    assert m.sanitize(s) == s


# ---------------------------------------------------------------- send_prompt


def test_envoi_une_ligne_puis_retour_chariot(konsole):
    write_environ(konsole.proc, PID, **konsole_env())
    assert run(konsole.send_prompt(PID, "fais les tests")) is None
    assert konsole.fake.sent() == [("fais les tests",), ("\r",)]
    for service, path, method, _ in konsole.fake.calls:
        assert (service, path) == (SERVICE, SESSION)
    methods = [c[2] for c in konsole.fake.calls]
    assert methods.index(FG) < methods.index(SEND), "foreground vérifié avant tout envoi"


def test_envoi_multiligne_en_bracketed_paste(konsole):
    write_environ(konsole.proc, PID, **konsole_env())
    assert run(konsole.send_prompt(PID, "ligne 1\x1b[2J\nligne\x07 2")) is None
    assert konsole.fake.sent() == [(PASTE_START + "ligne 1[2J\nligne 2" + PASTE_END,), ("\r",)]


def test_caracteres_de_controle_retires_une_ligne(konsole):
    write_environ(konsole.proc, PID, **konsole_env())
    assert run(konsole.send_prompt(PID, "ls\x1b]0;x\x07\rrm -rf\x00")) is None
    sent = konsole.fake.sent()
    assert sent[-1] == ("\r",)
    body = "".join(a[0] for a in sent[:-1])
    assert "\x1b" not in body and "\x07" not in body and "\r" not in body and "\x00" not in body


@pytest.mark.parametrize("fg", [PID + 1, 1])
def test_foreground_different_rien_envoye(konsole, fg):
    write_environ(konsole.proc, PID, **konsole_env())
    konsole.fake.foreground = fg
    reason = run(konsole.send_prompt(PID, "rm -rf /"))
    assert isinstance(reason, str) and reason
    assert konsole.fake.sent() == []


def test_session_non_konsole_rejetee_sans_dbus(konsole):
    env = konsole_env()
    del env["KONSOLE_DBUS_SESSION"]
    write_environ(konsole.proc, PID, **env)
    reason = run(konsole.send_prompt(PID, "salut"))
    assert isinstance(reason, str) and reason
    assert konsole.fake.sent() == []
    assert "xyz" not in reason


@pytest.mark.parametrize("err", [RuntimeError("dbus down"), OSError("gdbus introuvable"),
                                 FileNotFoundError("gdbus")])
def test_dbus_en_erreur_raison_sans_exception(konsole, err):
    write_environ(konsole.proc, PID, **konsole_env())
    konsole.fake.error = err
    reason = run(konsole.send_prompt(PID, "salut"))
    assert isinstance(reason, str) and reason
    assert konsole.fake.sent() == []


def test_reponse_foreground_inattendue(konsole, monkeypatch):
    write_environ(konsole.proc, PID, **konsole_env())

    async def weird(service, path, method, *args):
        konsole.fake.calls.append((service, path, method, args))
        return "" if method == SEND else "n'importe quoi"

    monkeypatch.setattr(konsole, "dbus_call", weird)
    reason = run(konsole.send_prompt(PID, "salut"))
    assert isinstance(reason, str) and reason
    assert konsole.fake.sent() == []


# ---------------------------------------------------------------- intégration app (WebSocket)


@pytest.fixture
def client(tmp_path, konsole, monkeypatch):
    c_dir = tmp_path / "claude-config"
    (c_dir / "projects").mkdir(parents=True)
    (c_dir / "sessions").mkdir()
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", c_dir)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        yield c
        c.portal = None


def connect(client):
    return client.websocket_connect("/ws", headers=ORIGIN)


def recv(ws, timeout=TIMEOUT):
    async def _r():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    msg = ws.portal.call(_r)
    assert msg["type"] == "websocket.send", msg
    return json.loads(msg["text"])


def recv_until(ws, pred, timeout=TIMEOUT, seen=None):
    while True:
        ev = recv(ws, timeout)
        if seen is not None:
            seen.append(ev)
        if pred(ev):
            return ev


def drain(ws, seen, timeout=0.3):
    try:
        while True:
            seen.append(recv(ws, timeout))
    except TimeoutError:
        pass


def handshake(ws):
    assert recv(ws)["type"] == "hello"
    snap = recv(ws)
    assert snap["type"] == "snapshot"
    return snap


@pytest.fixture
def observed(client, tmp_path):
    agent = {"id": "o-" + uuid.uuid4().hex[:8], "name": "eter-86", "cwd": str(tmp_path),
             "project": tmp_path.name, "status": "idle", "observed": True, "pid": PID}
    client.portal.call(app_mod.hub.emit, {"type": "observed_joined", "agent": agent})
    yield agent
    client.portal.call(app_mod.hub.emit, {"type": "observed_left", "agent_id": agent["id"]})


def status(client, agent, s):
    client.portal.call(app_mod.hub.emit, {"type": "observed_status", "agent_id": agent["id"], "status": s})


def send_ticket(ws, agent, title):
    ws.send_json({"type": "new_ticket", "title": title, "agent_id": agent["id"]})


def test_ticket_tape_dans_l_onglet_et_assigne(client, konsole, observed):
    write_environ(konsole.proc, PID, **konsole_env())
    title = f"t-{uuid.uuid4().hex}"
    seen = []
    with connect(client) as ws:
        handshake(ws)
        send_ticket(ws, observed, title)
        created = recv_until(ws, lambda e: e.get("type") == "ticket_created"
                             and e["ticket"]["title"] == title, seen=seen)
        tid = created["ticket"]["id"]
        assigned = recv_until(ws, lambda e: e.get("type") == "ticket_assigned"
                              and e.get("ticket_id") == tid, seen=seen)
        drain(ws, seen)
    assert assigned["agent_id"] == observed["id"]
    assert konsole.fake.sent() == [(title,), ("\r",)]
    assert not [e for e in seen if e.get("type") == "ticket_rejected"]


def test_ticket_termine_apres_busy_puis_idle(client, konsole, observed):
    write_environ(konsole.proc, PID, **konsole_env())
    title = f"t-{uuid.uuid4().hex}"
    seen = []
    with connect(client) as ws:
        handshake(ws)
        send_ticket(ws, observed, title)
        tid = recv_until(ws, lambda e: e.get("type") == "ticket_created"
                         and e["ticket"]["title"] == title, seen=seen)["ticket"]["id"]
        recv_until(ws, lambda e: e.get("type") == "ticket_assigned" and e.get("ticket_id") == tid, seen=seen)
        status(client, observed, "idle")  # idle sans busy préalable : pas de fin
        drain(ws, seen)
        assert not [e for e in seen if e.get("type") == "ticket_done"]
        status(client, observed, "busy")
        drain(ws, seen)
        assert not [e for e in seen if e.get("type") == "ticket_done"], "busy seul ne termine pas"
        status(client, observed, "idle")
        done = recv_until(ws, lambda e: e.get("type") == "ticket_done", seen=seen)
    assert done["ticket_id"] == tid


def _assert_rejected(client, observed):
    title = f"t-{uuid.uuid4().hex}"
    seen = []
    with connect(client) as ws:
        handshake(ws)
        send_ticket(ws, observed, title)
        rej = recv_until(ws, lambda e: e.get("type") == "ticket_rejected", seen=seen)
        drain(ws, seen)
    assert isinstance(rej.get("reason"), str) and rej["reason"]
    assert not [e for e in seen if e.get("type") in ("ticket_created", "ticket_assigned", "agent_hired")]
    return seen


def test_foreground_different_ticket_rejete(client, konsole, observed):
    write_environ(konsole.proc, PID, **konsole_env())
    konsole.fake.foreground = PID + 1  # un shell ou un autre programme au premier plan
    _assert_rejected(client, observed)
    assert konsole.fake.sent() == []


def test_session_non_konsole_ticket_rejete(client, konsole, observed):
    write_environ(konsole.proc, PID, HOME="/home/x", SECRET_TOKEN="xyz-very-secret")
    _assert_rejected(client, observed)
    assert konsole.fake.sent() == []


def test_dbus_indisponible_ticket_rejete_sans_crash(client, konsole, observed):
    write_environ(konsole.proc, PID, **konsole_env())
    konsole.fake.error = RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")
    _assert_rejected(client, observed)
    assert konsole.fake.sent() == []
    # le serveur répond toujours
    with connect(client) as ws:
        handshake(ws)


def test_aucune_autre_variable_d_environnement_dans_les_evenements(client, konsole, observed):
    write_environ(konsole.proc, PID, **konsole_env())
    seen = []
    with connect(client) as ws:
        seen.append(handshake(ws))
        send_ticket(ws, observed, f"t-{uuid.uuid4().hex}")
        recv_until(ws, lambda e: e.get("type") == "ticket_assigned", seen=seen)
        status(client, observed, "busy")
        status(client, observed, "idle")
        recv_until(ws, lambda e: e.get("type") == "ticket_done", seen=seen)
        konsole.fake.foreground = 1
        send_ticket(ws, observed, f"t-{uuid.uuid4().hex}")
        recv_until(ws, lambda e: e.get("type") == "ticket_rejected", seen=seen)
        drain(ws, seen)
    dump = json.dumps(seen)
    for needle in ("SECRET_TOKEN", "xyz-very-secret", "/usr/bin", "/home/x"):
        assert needle not in dump, needle
