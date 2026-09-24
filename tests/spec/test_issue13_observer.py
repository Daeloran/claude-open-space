"""Tests d'intention pour l'issue #13 : sessions Claude Code du terminal affichées comme employés observés.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Aucun accès au vrai ~/.claude : tout vit dans `tmp_path`.

Contrat public supposé — nouveau module `backend/observer.py` :
- `live_sessions(config_dir: Path, pid_alive=<callable pid -> bool>) -> list[dict]` :
  lit `config_dir/sessions/*.json` ; ne garde que les sessions dont le pid est vivant et
  `entrypoint == "cli"` ; chaque dict contient EXACTEMENT `session_id`, `pid`, `cwd`, `name`,
  `status`, `project` (basename du cwd). Fichier illisible / JSON invalide / champs manquants :
  ignoré sans exception. N'ouvre jamais de fichier `*.key`.
- `TranscriptTail(path: Path, from_end: bool = False)` ; `read_new() -> list[dict]` renvoie les
  enregistrements JSON complets ajoutés depuis le dernier appel (au premier appel : depuis le
  début, ou depuis la fin courante du fichier si `from_end=True`). Une dernière ligne sans `\\n`
  n'est renvoyée qu'une fois terminée, et une seule fois. Fichier absent / supprimé : `[]`.
  Lignes JSON invalides ignorées.
- `Observer(config_dir: Path, emit, pid_alive=<callable>, context_window: int = 200000)`,
  `async poll()` = une itération ; `emit` est une coroutine `async def emit(event)`.
  - nouvelle session vivante → `{"type": "observed_joined", "agent": {"id": "o-" + session_id[:8],
    "name", "cwd", "project", "status", "observed": True}}` ; l'historique du transcript n'est pas
    rejoué en `tool_use`, mais un `context` initial est émis depuis le DERNIER enregistrement
    assistant non sidechain ayant un `usage`.
  - session disparue du registre ou pid mort → `{"type": "observed_left", "agent_id"}` (une fois).
  - changement de statut → `{"type": "observed_status", "agent_id", "status"}`.
  - nouvelle ligne assistant avec `tool_use` → `{"type": "tool_use", "agent_id", "tool", "summary"}`
    (summary non vide) ; nouvelle ligne `tool_result` → `{"type": "tool_result", "agent_id",
    "tool": <nom de l'outil correspondant, sinon "?">, "ok": not is_error}`.
  - nouvelle ligne assistant non sidechain avec `usage` → `{"type": "context", "agent_id", "ratio"}`,
    ratio = (input + cache_read + cache_creation) / fenêtre, borné [0, 1] ; fenêtre =
    `context_window`, ou 1 000 000 si le modèle contient `[1m]` ou si les tokens dépassent
    `context_window`.
  - aucun événement ne contient `messagingSocketPath`, `peerProtocol`, `pidDomain`, `procStart`
    ni le contenu d'un `.key`.
- Intégration : `backend.app.CLAUDE_CONFIG_DIR` (monkeypatchable) ; le `snapshot` contient les
  employés observés dans `agents` (avec `observed: True`) ; un `new_ticket` avec l'`agent_id`
  d'un employé observé → `ticket_rejected`, aucune session Claude créée.

Hypothèses ajoutées :
- Le transcript d'une session est trouvé par `config_dir/projects/*/<sessionId>.jsonl` (le nom
  du dossier projet n'est pas interprété).
- Une session rejointe dont le transcript n'existe pas encore : quand il apparaît, ses lignes
  sont traitées depuis le début (première invite d'une nouvelle session).
- `observed_joined` est émis avant tout autre événement de cet employé.
- Les lignes sidechain ne produisent pas d'événement `context`.
- Un `.key` est détecté comme « lu » s'il passe par `open`/`io.open`/`os.open`/`Path.open`/
  `Path.read_text`/`Path.read_bytes` ; il est en plus rendu illisible (chmod 000).
"""
import asyncio
import builtins
import io
import json
import os
import pathlib
import uuid
from pathlib import Path

import anyio
import anyio.from_thread
import pytest
from fastapi.testclient import TestClient

import backend.app as app_mod

ORIGIN = {"origin": "http://testserver"}
TIMEOUT = 3
SOCKET = "/tmp/claude-secret-messaging-socket.sock"
KEY_SECRET = "KEY-SECRET-0123456789abcdef"
FORBIDDEN = ("messagingSocketPath", "peerProtocol", "pidDomain", "procStart", SOCKET, KEY_SECRET)


def observer_mod():
    import backend.observer as m  # import tardif : échec par test, pas à la collecte

    return m


# ---------------------------------------------------------------- fixtures de faux ~/.claude


def new_sid():
    return str(uuid.uuid4())


def write_session(cfg, pid, sid, cwd, entrypoint="cli", status="idle", name=None, key=True):
    d = cfg / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "pid": pid, "cwd": str(cwd), "entrypoint": entrypoint, "kind": "interactive",
        "name": name or f"{Path(cwd).name}-{pid}", "status": status, "sessionId": sid,
        "startedAt": 1790000000000, "version": "2.1.281", "messagingSocketPath": SOCKET,
        "peerProtocol": 1, "procStart": "76202", "pidDomain": "linux:abcdef",
    }
    (d / f"{pid}.json").write_text(json.dumps(rec))
    if key:
        k = d / f"{pid}.{uuid.uuid4().hex[:12]}.key"
        k.write_text(KEY_SECRET)
        k.chmod(0)
    return rec


def remove_session(cfg, pid):
    (cfg / "sessions" / f"{pid}.json").unlink()


def transcript_path(cfg, sid, cwd):
    d = cfg / "projects" / str(cwd).replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sid}.jsonl"


def append(path, *records, raw=None):
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        if raw is not None:
            f.write(raw)


def usage(inp=10, read=40000, create=9990, out=50):
    return {"input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": create, "output_tokens": out}


def assistant(content, u=None, model="claude-opus-5", sidechain=False, sid="x", cwd="/x"):
    msg = {"role": "assistant", "model": model, "content": content}
    if u is not None:
        msg["usage"] = u
    return {"type": "assistant", "isSidechain": sidechain, "sessionId": sid, "cwd": cwd, "message": msg}


def tool_use(tid="toolu_1", name="Bash", inp=None, u=None, **kw):
    return assistant([{"type": "tool_use", "id": tid, "name": name, "input": inp or {"command": "ls"}}], u=u, **kw)


def tool_result(tid="toolu_1", is_error=False):
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": "ok"}]}}


def prompt(text="salut"):
    return {"type": "user", "message": {"role": "user", "content": text}}


class Alive:
    """pid_alive factice : ensemble de pids vivants, modifiable pendant le test."""

    def __init__(self, *pids):
        self.pids = set(pids)

    def __call__(self, pid):
        return pid in self.pids


def aid(sid):
    return "o-" + sid[:8]


@pytest.fixture
def cfg(tmp_path):
    c = tmp_path / "claude-config"
    (c / "sessions").mkdir(parents=True)
    (c / "projects").mkdir()
    return c


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "eter"
    d.mkdir()
    return d


class Harness:
    """Observer + liste des événements émis ; `poll()` synchrone pour les tests."""

    def __init__(self, cfg, alive, **kw):
        self.events: list[dict] = []
        self.loop = asyncio.new_event_loop()

        async def emit(ev):
            self.events.append(ev)

        self.obs = observer_mod().Observer(cfg, emit, pid_alive=alive, **kw)

    def poll(self):
        """Une itération ; renvoie uniquement les événements émis par cette itération."""
        n = len(self.events)
        self.loop.run_until_complete(self.obs.poll())
        return self.events[n:]

    def close(self):
        self.loop.close()


@pytest.fixture
def harness():
    made = []

    def make(cfg, alive, **kw):
        h = Harness(cfg, alive, **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def of(events, type_, agent=None):
    out = [e for e in events if e.get("type") == type_]
    if agent is not None:
        out = [e for e in out if e.get("agent_id") == agent
               or (e.get("agent") or {}).get("id") == agent]
    return out


# ---------------------------------------------------------------- détection de lecture des .key


@pytest.fixture
def key_reads(monkeypatch):
    """Enregistre toute ouverture d'un fichier `*.key`."""
    seen: list[str] = []

    def spy(fn):
        def wrapper(file, *a, **k):
            if str(os.fspath(file) if not isinstance(file, int) else "").endswith(".key"):
                seen.append(str(file))
            return fn(file, *a, **k)
        return wrapper

    def spy_method(fn):
        def wrapper(self, *a, **k):
            if str(self).endswith(".key"):
                seen.append(str(self))
            return fn(self, *a, **k)
        return wrapper

    monkeypatch.setattr(builtins, "open", spy(builtins.open))
    monkeypatch.setattr(io, "open", spy(io.open))
    monkeypatch.setattr(os, "open", spy(os.open))
    for name in ("open", "read_text", "read_bytes"):
        monkeypatch.setattr(pathlib.Path, name, spy_method(getattr(pathlib.Path, name)))
    return seen


# ---------------------------------------------------------------- live_sessions


def test_seules_les_sessions_cli_vivantes_sont_listees(cfg, proj):
    sid_ok, sid_dead, sid_sdk = new_sid(), new_sid(), new_sid()
    write_session(cfg, 101, sid_ok, proj, name="eter-86", status="busy")
    write_session(cfg, 102, sid_dead, proj)
    write_session(cfg, 103, sid_sdk, proj, entrypoint="sdk-py")
    got = observer_mod().live_sessions(cfg, pid_alive=Alive(101, 103))
    assert got == [{"session_id": sid_ok, "pid": 101, "cwd": str(proj), "name": "eter-86",
                    "status": "busy", "project": "eter"}]


def test_live_sessions_ne_contient_que_les_champs_autorises(cfg, proj):
    write_session(cfg, 101, new_sid(), proj)
    (s,) = observer_mod().live_sessions(cfg, pid_alive=Alive(101))
    assert set(s) == {"session_id", "pid", "cwd", "name", "status", "project"}
    blob = json.dumps(s)
    for f in FORBIDDEN:
        assert f not in blob


def test_live_sessions_n_ouvre_jamais_les_fichiers_key(cfg, proj, key_reads):
    write_session(cfg, 101, new_sid(), proj)
    write_session(cfg, 102, new_sid(), proj)
    got = observer_mod().live_sessions(cfg, pid_alive=Alive(101, 102))
    assert len(got) == 2
    assert key_reads == []


def test_live_sessions_fichiers_invalides_ignores_sans_crash(cfg, proj):
    sid = new_sid()
    write_session(cfg, 101, sid, proj)
    d = cfg / "sessions"
    (d / "200.json").write_text("{pas du json")
    (d / "201.json").write_text("")
    (d / "202.json").write_text(json.dumps([1, 2, 3]))
    (d / "203.json").write_text(json.dumps({"pid": 203, "entrypoint": "cli"}))  # champs manquants
    (d / "204.json").write_text(json.dumps({"pid": "abc", "cwd": str(proj), "entrypoint": "cli",
                                            "sessionId": new_sid(), "name": "x", "status": "idle"}))
    unreadable = d / "205.json"
    unreadable.write_text(json.dumps({"pid": 205, "cwd": str(proj), "entrypoint": "cli",
                                      "sessionId": new_sid(), "name": "x", "status": "idle"}))
    unreadable.chmod(0)
    (d / "206.json").mkdir()  # un dossier nommé comme un fichier de session
    try:
        got = observer_mod().live_sessions(cfg, pid_alive=lambda pid: True)
    finally:
        unreadable.chmod(0o600)
    assert [s["session_id"] for s in got] == [sid]


def test_live_sessions_config_absente(tmp_path):
    assert observer_mod().live_sessions(tmp_path / "nope", pid_alive=lambda pid: True) == []


# ---------------------------------------------------------------- TranscriptTail


def test_tail_lit_depuis_le_debut_puis_seulement_les_nouvelles_lignes(tmp_path):
    p = tmp_path / "t.jsonl"
    append(p, {"n": 1}, {"n": 2})
    tail = observer_mod().TranscriptTail(p)
    assert tail.read_new() == [{"n": 1}, {"n": 2}]
    assert tail.read_new() == []
    append(p, {"n": 3})
    assert tail.read_new() == [{"n": 3}]
    assert tail.read_new() == []


def test_tail_from_end_ignore_l_existant(tmp_path):
    p = tmp_path / "t.jsonl"
    append(p, {"n": 1}, {"n": 2})
    tail = observer_mod().TranscriptTail(p, from_end=True)
    assert tail.read_new() == []
    append(p, {"n": 3})
    assert tail.read_new() == [{"n": 3}]


def test_tail_ligne_en_cours_d_ecriture_renvoyee_une_seule_fois(tmp_path):
    p = tmp_path / "t.jsonl"
    append(p, {"n": 1}, raw='{"n": ')
    tail = observer_mod().TranscriptTail(p)
    assert tail.read_new() == [{"n": 1}]
    assert tail.read_new() == []
    append(p, raw='2}\n')
    assert tail.read_new() == [{"n": 2}]
    assert tail.read_new() == []


def test_tail_lignes_invalides_ignorees(tmp_path):
    p = tmp_path / "t.jsonl"
    append(p, {"n": 1}, raw="{pas du json\n\n")
    append(p, {"n": 2})
    assert observer_mod().TranscriptTail(p).read_new() == [{"n": 1}, {"n": 2}]


def test_tail_fichier_absent_ou_supprime(tmp_path):
    p = tmp_path / "t.jsonl"
    tail = observer_mod().TranscriptTail(p)
    assert tail.read_new() == []
    append(p, {"n": 1})
    assert tail.read_new() == [{"n": 1}]
    p.unlink()
    assert tail.read_new() == []
    assert observer_mod().TranscriptTail(tmp_path / "jamais.jsonl", from_end=True).read_new() == []


# ---------------------------------------------------------------- Observer : registre


def test_observer_nouvelle_session_rejoint_l_open_space(cfg, proj, harness):
    sid = new_sid()
    write_session(cfg, 101, sid, proj, name="eter-86", status="busy")
    write_session(cfg, 102, new_sid(), proj)                      # pid mort
    write_session(cfg, 103, new_sid(), proj, entrypoint="sdk-py")  # session du jeu
    h = harness(cfg, Alive(101, 103))
    evs = h.poll()
    joined = of(evs, "observed_joined")
    assert joined == [{"type": "observed_joined", "agent": {
        "id": aid(sid), "name": "eter-86", "cwd": str(proj), "project": "eter",
        "status": "busy", "observed": True}}]
    assert of(h.poll(), "observed_joined") == [], "pas de réémission au tour suivant"


def test_observer_session_qui_apparait_plus_tard(cfg, proj, harness):
    h = harness(cfg, Alive(101))
    assert of(h.poll(), "observed_joined") == []
    sid = new_sid()
    write_session(cfg, 101, sid, proj)
    assert [e["agent"]["id"] for e in of(h.poll(), "observed_joined")] == [aid(sid)]


def test_observer_session_retiree_du_registre_part(cfg, proj, harness):
    sid = new_sid()
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    remove_session(cfg, 101)
    assert of(h.poll(), "observed_left") == [{"type": "observed_left", "agent_id": aid(sid)}]
    assert of(h.poll(), "observed_left") == [], "départ émis une seule fois"


def test_observer_pid_mort_part(cfg, proj, harness):
    sid = new_sid()
    write_session(cfg, 101, sid, proj)
    alive = Alive(101)
    h = harness(cfg, alive)
    h.poll()
    alive.pids.clear()
    assert of(h.poll(), "observed_left") == [{"type": "observed_left", "agent_id": aid(sid)}]
    assert of(h.poll(), "observed_left") == []


def test_observer_changement_de_statut(cfg, proj, harness):
    sid = new_sid()
    write_session(cfg, 101, sid, proj, status="idle")
    h = harness(cfg, Alive(101))
    h.poll()
    assert of(h.poll(), "observed_status") == [], "pas de changement, pas d'événement"
    write_session(cfg, 101, sid, proj, status="busy", key=False)
    assert of(h.poll(), "observed_status") == [
        {"type": "observed_status", "agent_id": aid(sid), "status": "busy"}]
    assert of(h.poll(), "observed_status") == []


# ---------------------------------------------------------------- Observer : transcript


def test_historique_non_rejoue_mais_contexte_initial(cfg, proj, harness):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt(), tool_use("toolu_a", u=usage(read=10000, create=0, inp=0)),
           tool_result("toolu_a"),
           assistant([{"type": "text", "text": "fini"}], u=usage(inp=10, read=40000, create=9990)),
           tool_use("toolu_s", u=usage(read=190000), sidechain=True),  # sidechain : ignoré
           {"type": "system", "subtype": "x"})
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    evs = h.poll()
    assert of(evs, "tool_use") == [] and of(evs, "tool_result") == []
    ctx = of(evs, "context", aid(sid))
    assert len(ctx) == 1
    assert ctx[0]["ratio"] == pytest.approx(0.25)
    types = [e["type"] for e in evs if e.get("agent_id") == aid(sid) or (e.get("agent") or {}).get("id") == aid(sid)]
    assert types[0] == "observed_joined"


def test_pas_de_contexte_initial_sans_usage(cfg, proj, harness):
    sid = new_sid()
    append(transcript_path(cfg, sid, proj), prompt(), assistant([{"type": "text", "text": "hi"}]))
    write_session(cfg, 101, sid, proj)
    assert of(harness(cfg, Alive(101)).poll(), "context") == []


def test_nouveau_tool_use_emis_sans_reemettre_les_anciens(cfg, proj, harness):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt(), tool_use("toolu_old", name="Read", inp={"file_path": "/a/b.py"}))
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    append(t, tool_use("toolu_1", name="Bash", inp={"command": "ls -la"}))
    evs = h.poll()
    uses = of(evs, "tool_use")
    assert len(uses) == 1
    assert uses[0]["agent_id"] == aid(sid)
    assert uses[0]["tool"] == "Bash"
    assert isinstance(uses[0]["summary"], str) and uses[0]["summary"].strip()
    assert of(h.poll(), "tool_use") == [], "une ligne n'est traitée qu'une fois"


def test_tool_result_porte_le_nom_de_l_outil(cfg, proj, harness):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt())
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    append(t, tool_use("toolu_1", name="Grep", inp={"pattern": "x"}))
    h.poll()
    append(t, tool_result("toolu_1", is_error=False), tool_result("toolu_zz", is_error=True))
    res = of(h.poll(), "tool_result")
    assert res == [
        {"type": "tool_result", "agent_id": aid(sid), "tool": "Grep", "ok": True},
        {"type": "tool_result", "agent_id": aid(sid), "tool": "?", "ok": False},
    ]


def test_transcript_cree_apres_l_arrivee_de_la_session(cfg, proj, harness):
    sid = new_sid()
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    append(transcript_path(cfg, sid, proj), prompt(), tool_use("toolu_1", name="Bash"))
    uses = of(h.poll(), "tool_use")
    assert [(e["agent_id"], e["tool"]) for e in uses] == [(aid(sid), "Bash")]


@pytest.mark.parametrize("model,u,window,expected", [
    ("claude-opus-5", usage(inp=10, read=40000, create=9990), 200000, 0.25),
    ("claude-opus-5", usage(inp=0, read=50000, create=0), 100000, 0.5),
    ("claude-opus-5[1m]", usage(inp=10, read=40000, create=9990), 200000, 0.05),
    ("claude-opus-5", usage(inp=0, read=250000, create=0), 200000, 0.25),  # dépasse → 1M
    ("claude-opus-5[1m]", usage(inp=0, read=3000000, create=0), 200000, 1.0),  # borné
], ids=["standard", "fenetre_configuree", "modele_1m", "depassement_1m", "borne_a_1"])
def test_contexte_ratio(cfg, proj, harness, model, u, window, expected):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt())
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101), context_window=window)
    h.poll()
    append(t, assistant([{"type": "text", "text": "ok"}], u=u, model=model))
    ctx = of(h.poll(), "context")
    assert len(ctx) == 1
    assert ctx[0]["agent_id"] == aid(sid)
    assert ctx[0]["ratio"] == pytest.approx(expected)


def test_contexte_ignore_les_sidechains(cfg, proj, harness):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt())
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    append(t, assistant([{"type": "text", "text": "sub"}], u=usage(read=150000), sidechain=True))
    assert of(h.poll(), "context") == []


def test_ligne_tronquee_puis_completee(cfg, proj, harness):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt())
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    line = json.dumps(tool_use("toolu_1", name="Bash"))
    append(t, raw=line[:25])
    assert of(h.poll(), "tool_use") == []
    append(t, raw=line[25:] + "\n")
    assert len(of(h.poll(), "tool_use")) == 1
    assert of(h.poll(), "tool_use") == []


def test_lignes_invalides_et_formats_inattendus_sans_crash(cfg, proj, harness):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt())
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    append(t, {"type": "assistant"}, {"type": "assistant", "message": "pas un dict"},
           {"type": "assistant", "message": {"content": "texte brut", "usage": "?"}},
           {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
           {"type": "attachment"}, {"type": "mode"}, [1, 2], "chaine", 42,
           raw="{tronqué\n")
    append(t, tool_use("toolu_9", name="Bash"))
    assert [e["tool"] for e in of(h.poll(), "tool_use")] == ["Bash"]


def test_transcript_supprime_en_cours_de_route(cfg, proj, harness):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt())
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    append(t, tool_use())
    t.unlink()
    h.poll()  # ne lève pas
    h.poll()


def test_fichier_de_session_supprime_ou_corrompu_en_cours_de_route(cfg, proj, harness):
    sid = new_sid()
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    (cfg / "sessions" / "101.json").write_text('{"pid": 101, "cwd"')
    h.poll()  # ne lève pas
    import shutil
    shutil.rmtree(cfg / "sessions")
    evs = h.poll()
    assert of(evs, "observed_left") == [{"type": "observed_left", "agent_id": aid(sid)}]


# ---------------------------------------------------------------- secrets


def test_aucun_secret_dans_les_evenements(cfg, proj, harness, key_reads):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt(), assistant([{"type": "text", "text": "x"}], u=usage()))
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101))
    h.poll()
    write_session(cfg, 101, sid, proj, status="busy")
    append(t, tool_use(), tool_result(), assistant([{"type": "text", "text": "y"}], u=usage()))
    h.poll()
    remove_session(cfg, 101)
    h.poll()
    types = {e["type"] for e in h.events}
    assert {"observed_joined", "observed_status", "tool_use", "tool_result", "context", "observed_left"} <= types
    blob = json.dumps(h.events)
    for f in FORBIDDEN:
        assert f not in blob, f
    assert key_reads == []


# ---------------------------------------------------------------- intégration app (WebSocket)


INSTANCES: list = []


class FakeClient:
    def __init__(self, options):
        INSTANCES.append(self)

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

    async def receive_response(self):
        return
        yield

    async def get_context_usage(self):
        return {"totalTokens": 0, "maxTokens": 100000}


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


def handshake(ws):
    hello = recv(ws)
    assert hello["type"] == "hello"
    snap = recv(ws)
    assert snap["type"] == "snapshot"
    return hello, snap


@pytest.fixture
def observed(client, tmp_path):
    """Un employé observé injecté via hub.emit ; retiré en fin de test."""
    sid = new_sid()
    agent = {"id": aid(sid), "name": "eter-86", "cwd": str(tmp_path), "project": tmp_path.name,
             "status": "idle", "observed": True}
    client.portal.call(app_mod.hub.emit, {"type": "observed_joined", "agent": agent})
    yield agent
    client.portal.call(app_mod.hub.emit, {"type": "observed_left", "agent_id": agent["id"]})


def test_snapshot_inclut_les_employes_observes(client, observed):
    with connect(client) as ws:
        _, snap = handshake(ws)
    match = [a for a in snap["agents"] if a["id"] == observed["id"]]
    assert len(match) == 1
    assert match[0]["observed"] is True
    assert {k: match[0][k] for k in ("name", "cwd", "project")} == \
        {k: observed[k] for k in ("name", "cwd", "project")}


def test_employe_observe_retire_du_snapshot_apres_depart(client, tmp_path):
    sid = new_sid()
    agent = {"id": aid(sid), "name": "x", "cwd": str(tmp_path), "project": tmp_path.name,
             "status": "idle", "observed": True}
    client.portal.call(app_mod.hub.emit, {"type": "observed_joined", "agent": agent})
    client.portal.call(app_mod.hub.emit, {"type": "observed_left", "agent_id": agent["id"]})
    with connect(client) as ws:
        _, snap = handshake(ws)
    assert not [a for a in snap["agents"] if a["id"] == agent["id"]]


def test_ticket_vers_employe_observe_refuse(client, observed):
    with connect(client) as ws:
        handshake(ws)
        n = len(INSTANCES)
        title = f"t-{uuid.uuid4().hex}"
        ws.send_json({"type": "new_ticket", "title": title, "agent_id": observed["id"]})
        seen = []
        rej = recv_until(ws, lambda e: e.get("type") == "ticket_rejected", seen=seen)
        try:
            while True:
                seen.append(recv(ws, 0.3))
        except TimeoutError:
            pass
    assert isinstance(rej.get("reason"), str) and rej["reason"]
    assert len(INSTANCES) == n, "aucune session Claude créée"
    assert not [e for e in seen if e.get("type") in ("ticket_assigned", "agent_hired")]
