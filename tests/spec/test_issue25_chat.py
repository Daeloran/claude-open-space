"""Tests d'intention pour l'issue #25 : panneau de discussion avec un employé terminal.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Aucun accès au vrai ~/.claude (tout vit dans `tmp_path`), aucun D-Bus, aucun sous-processus.

Contrat public supposé :
- `backend.observer.chat_entries(records: list[dict]) -> list[dict]` : enregistrements de transcript
  → entrées de discussion `{"role": "user"|"assistant", "kind": "text"|"tool_use"|"tool_result",
  "text", "tool", "summary", "ok", "ts"}` (champs selon le type), dans l'ordre. Exclus : blocs
  `thinking`, enregistrements `isMeta`, lignes sidechain, enregistrements non conversationnels
  (attachment, permission-mode, system, file-history-snapshot…). `<system-reminder>…</system-reminder>`
  retirés des textes utilisateur. Sorties d'outil tronquées à 2 000 caractères, le texte tronqué se
  terminant par une indication contenant « tronqu ».
- WebSocket (`/ws`, origin `http://testserver`) :
  - `{"type": "open_chat", "agent_id"}` → `{"type": "chat_history", "agent_id", "entries"}` au seul
    client demandeur, 200 dernières entrées au plus, ordre chronologique.
  - ensuite, chaque nouvelle entrée du transcript lue par l'observateur → `{"type": "chat_entry",
    "agent_id", "entry"}` aux seuls clients abonnés ; `{"type": "close_chat", "agent_id"}` arrête l'envoi.
  - `agent_id` inconnu ou sans transcript → `chat_history` avec `entries == []` et un champ `error`.

Hypothèses ajoutées (câblage de test) :
- Le backend retrouve le transcript d'un employé observé via le global `backend.app.observer`
  (instance de `Observer`, créée au démarrage). Les tests n'exécutent pas le lifespan : ils créent un
  vrai registre de sessions + transcript dans `tmp_path`, instancient
  `Observer(cfg, hub.emit, pid_alive=<factice>)`, l'affectent à `backend.app.observer` et appellent
  `poll()` à la main (dans la boucle du portail WebSocket). L'`observed_joined` est donc émis par
  l'observateur lui-même, avec l'id `"o-" + sessionId[:8]`.
- Les messages d'un même client WebSocket sont traités dans l'ordre : une réponse `chat_history` à
  un `open_chat` ultérieur prouve qu'un `close_chat` antérieur a été pris en compte.
- Le texte d'une entrée `text` contient le texte d'origine (espaces de bord éventuellement retirés).
- `ts` est renseigné (valeur non vide) pour un enregistrement horodaté.
- La commande d'un outil peut apparaître dans les événements diffusés (résumé `tool_use` existant) ;
  seuls les prompts, réponses et sorties d'outils sont vérifiés comme non diffusés.
"""
import asyncio
import json
import logging
import subprocess
import uuid
from pathlib import Path

import anyio
import anyio.from_thread
import pytest
from fastapi.testclient import TestClient

import backend.app as app_mod

ORIGIN = {"origin": "http://testserver"}
TIMEOUT = 3
PID = 4242425  # pid fictif : jamais celui d'une vraie session
TS = "2026-09-24T10:00:00Z"


def observer_mod():
    import backend.observer as m  # import tardif : échec par test, pas à la collecte

    return m


def chat_entries(records):
    return observer_mod().chat_entries(records)


# ---------------------------------------------------------------- enregistrements de transcript


def prompt(text, meta=False, sidechain=False, ts=TS):
    r = {"type": "user", "isSidechain": sidechain, "timestamp": ts,
         "message": {"role": "user", "content": text}}
    if meta:
        r["isMeta"] = True
    return r


def assistant(content, sidechain=False, ts=TS, stop="tool_use"):
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": ts, "message": {
        "role": "assistant", "model": "claude-opus-5-5", "content": content,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 100,
                  "cache_creation_input_tokens": 10, "output_tokens": 5},
        "stop_reason": stop}}


def text_block(t):
    return {"type": "text", "text": t}


def thinking_block(t):
    return {"type": "thinking", "thinking": t}


def tool_use_block(tid="toolu_1", name="Bash", inp=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {"command": "ls"}}


def tool_result(content="sortie", tid="toolu_1", is_error=False, sidechain=False, ts=TS):
    return {"type": "user", "isSidechain": sidechain, "timestamp": ts, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": content}]}}


def attachment(text="PIECE-JOINTE-SECRETE"):
    return {"type": "attachment", "isSidechain": False, "timestamp": TS,
            "attachment": {"type": "file", "content": text}}


NON_CONVERSATIONAL = [
    attachment(),
    {"type": "permission-mode", "permissionMode": "acceptEdits", "sessionId": "x"},
    {"type": "system", "subtype": "local_command", "content": "MODE-SYSTEME", "isSidechain": False, "timestamp": TS},
    {"type": "file-history-snapshot", "messageId": "m", "snapshot": {}},
    {"type": "summary", "summary": "RESUME-SESSION", "leafUuid": "u"},
]


def full_transcript(tag=""):
    """Transcript simulé de l'issue ; renvoie (records, textes attendus, textes interdits)."""
    records = [
        *NON_CONVERSATIONAL,
        prompt(f"PROMPT-META{tag}", meta=True),
        prompt(f"PROMPT-UTILISATEUR{tag}<system-reminder>RAPPEL-SYSTEME{tag}</system-reminder>"),
        assistant([thinking_block(f"PENSEE-PRIVEE{tag}"), text_block(f"REPONSE **md**{tag}"), tool_use_block()]),
        tool_result(f"SORTIE-OUTIL{tag}"),
        prompt(f"PROMPT-SOUS-AGENT{tag}", sidechain=True),
        assistant([text_block(f"REPONSE-SOUS-AGENT{tag}")], sidechain=True),
        tool_result(f"SORTIE-SOUS-AGENT{tag}", tid="toolu_9", sidechain=True),
    ]
    forbidden = [f"PROMPT-META{tag}", f"RAPPEL-SYSTEME{tag}", f"PENSEE-PRIVEE{tag}", f"PROMPT-SOUS-AGENT{tag}",
                 f"REPONSE-SOUS-AGENT{tag}", f"SORTIE-SOUS-AGENT{tag}", "PIECE-JOINTE-SECRETE",
                 "MODE-SYSTEME", "RESUME-SESSION"]
    return records, forbidden


def assert_full_transcript_entries(entries, tag=""):
    kinds = [(e.get("role"), e.get("kind")) for e in entries]
    assert [k for _, k in kinds] == ["text", "text", "tool_use", "tool_result"], entries
    assert kinds[0][0] == "user" and kinds[1][0] == "assistant" and kinds[2][0] == "assistant"
    assert f"PROMPT-UTILISATEUR{tag}" in entries[0]["text"]
    assert f"REPONSE **md**{tag}" in entries[1]["text"], "le Markdown brut est transmis, rendu côté front"
    assert entries[2]["tool"] == "Bash"
    assert isinstance(entries[2].get("summary"), str) and entries[2]["summary"]
    assert f"SORTIE-OUTIL{tag}" in entries[3]["text"]
    assert entries[3]["ok"] is True


# ---------------------------------------------------------------- chat_entries (conversion)


def test_conversion_ne_garde_que_prompt_texte_tool_use_tool_result_dans_l_ordre():
    records, forbidden = full_transcript()
    entries = chat_entries(records)
    assert_full_transcript_entries(entries)
    dump = json.dumps(entries, ensure_ascii=False)
    for f in forbidden:
        assert f not in dump, f


def test_system_reminder_retire_du_prompt():
    (e,) = chat_entries([prompt("Corrige le bug<system-reminder>\nbruit interne\n</system-reminder> vite")])
    assert e["role"] == "user" and e["kind"] == "text"
    assert "bruit interne" not in e["text"] and "system-reminder" not in e["text"]
    assert "Corrige le bug" in e["text"] and "vite" in e["text"]


def test_plusieurs_system_reminders_retires():
    (e,) = chat_entries([prompt("<system-reminder>A1</system-reminder>Bonjour<system-reminder>B2</system-reminder>")])
    assert "A1" not in e["text"] and "B2" not in e["text"] and "Bonjour" in e["text"]


def test_horodatage_present():
    (e,) = chat_entries([prompt("salut")])
    assert e.get("ts")


def test_tool_result_en_erreur():
    (e,) = chat_entries([tool_result("boom", is_error=True)])
    assert e["kind"] == "tool_result" and e["ok"] is False and "boom" in e["text"]


def test_tool_result_contenu_en_liste_de_blocs():
    (e,) = chat_entries([tool_result([text_block("PARTIE-UN"), text_block("PARTIE-DEUX")])])
    assert e["kind"] == "tool_result"
    assert "PARTIE-UN" in e["text"] and "PARTIE-DEUX" in e["text"]


def _assert_truncated(text, n_expected=2000):
    assert text.count("x") == n_expected, len(text)
    assert "tronqu" in text[n_expected:].lower(), text[-80:]
    assert len(text) < n_expected + 300


def test_sortie_d_outil_de_10000_caracteres_tronquee_a_2000():
    (e,) = chat_entries([tool_result("x" * 10_000)])
    _assert_truncated(e["text"])


def test_sortie_d_outil_en_blocs_tronquee_aussi():
    (e,) = chat_entries([tool_result([text_block("x" * 6_000), text_block("x" * 4_000)])])
    _assert_truncated(e["text"])


def test_sortie_courte_non_tronquee():
    out = "y" * 1_999
    (e,) = chat_entries([tool_result(out)])
    assert e["text"] == out


def test_enregistrements_non_conversationnels_sans_crash():
    weird = [{}, {"type": "user"}, {"type": "assistant", "message": {}}, {"type": "user", "message": {"content": None}},
             {"type": "assistant", "message": {"role": "assistant", "content": "texte brut"}}, *NON_CONVERSATIONAL]
    entries = chat_entries(weird)
    assert isinstance(entries, list)
    dump = json.dumps(entries, ensure_ascii=False)
    assert "PIECE-JOINTE-SECRETE" not in dump and "MODE-SYSTEME" not in dump


# ---------------------------------------------------------------- registre + transcript dans tmp_path


def write_session(cfg, pid, sid, cwd, status="idle", name="eter-86"):
    d = cfg / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "cwd": str(cwd), "entrypoint": "cli", "kind": "interactive", "name": name,
        "status": status, "sessionId": sid, "startedAt": 1790000000000, "version": "2.1.281"}))


def transcript_path(cfg, sid, cwd):
    d = cfg / "projects" / str(cwd).replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sid}.jsonl"


def append(path, *records):
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class Alive:
    def __init__(self, *pids):
        self.pids = set(pids)

    def __call__(self, pid):
        return pid in self.pids


# ---------------------------------------------------------------- portail WebSocket partagé


@pytest.fixture
def no_subprocess(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("aucun sous-processus réel dans les tests")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    import backend.konsole as k

    async def no_dbus(*a, **kw):
        raise RuntimeError("aucun D-Bus dans les tests")

    monkeypatch.setattr(k, "dbus_call", no_dbus)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = tmp_path / "claude-config"
    (c / "sessions").mkdir(parents=True)
    (c / "projects").mkdir()
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", c)
    return c


@pytest.fixture
def client(cfg, no_subprocess):
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


class Employee:
    """Session terminal observée : registre + transcript dans tmp_path, observateur branché sur l'app."""

    def __init__(self, client, cfg, proj, with_transcript=True):
        self.client = client
        self.sid = str(uuid.uuid4())
        self.id = "o-" + self.sid[:8]
        write_session(cfg, PID, self.sid, proj)
        self.path = transcript_path(cfg, self.sid, proj)
        if with_transcript:
            self.path.touch()
        self.obs = observer_mod().Observer(cfg, app_mod.hub.emit, pid_alive=Alive(PID))

    def poll(self):
        self.client.portal.call(self.obs.poll)

    def write(self, *records):
        append(self.path, *records)


@pytest.fixture
def employee(client, cfg, tmp_path, monkeypatch):
    made = []

    def make(records=(), with_transcript=True):
        proj = tmp_path / "eter"
        proj.mkdir(exist_ok=True)
        e = Employee(client, cfg, proj, with_transcript)
        if records:
            e.write(*records)
        monkeypatch.setattr(app_mod, "observer", e.obs, raising=False)
        e.poll()  # observed_joined émis par l'observateur
        made.append(e)
        return e

    yield make
    for e in made:
        client.portal.call(app_mod.hub.emit, {"type": "observed_left", "agent_id": e.id})


def open_chat(ws, agent_id, seen=None):
    ws.send_json({"type": "open_chat", "agent_id": agent_id})
    return recv_until(ws, lambda ev: ev.get("type") == "chat_history" and ev.get("agent_id") == agent_id,
                      seen=seen)


def of(events, type_):
    return [e for e in events if e.get("type") == type_]


# ---------------------------------------------------------------- open_chat → chat_history


def test_historique_filtre_et_ordonne(client, employee):
    records, forbidden = full_transcript()
    emp = employee(records)
    with connect(client) as ws:
        handshake(ws)
        hist = open_chat(ws, emp.id)
    assert "error" not in hist or not hist["error"]
    assert_full_transcript_entries(hist["entries"])
    dump = json.dumps(hist, ensure_ascii=False)
    for f in forbidden:
        assert f not in dump, f


def test_historique_system_reminder_retire(client, employee):
    emp = employee([prompt("Salut<system-reminder>BRUIT-RAPPEL</system-reminder>")])
    with connect(client) as ws:
        handshake(ws)
        (e,) = open_chat(ws, emp.id)["entries"]
    assert "Salut" in e["text"] and "BRUIT-RAPPEL" not in e["text"] and "system-reminder" not in e["text"]


def test_historique_sortie_d_outil_tronquee(client, employee):
    emp = employee([assistant([tool_use_block()]), tool_result("x" * 10_000)])
    with connect(client) as ws:
        handshake(ws)
        entries = open_chat(ws, emp.id)["entries"]
    _assert_truncated(entries[-1]["text"])


def test_historique_limite_aux_200_dernieres_entrees(client, employee):
    emp = employee([prompt(f"msg-{i:03d}") for i in range(250)])
    with connect(client) as ws:
        handshake(ws)
        entries = open_chat(ws, emp.id)["entries"]
    assert len(entries) == 200
    assert [e["text"] for e in entries] == [f"msg-{i:03d}" for i in range(50, 250)]


def test_historique_envoye_au_seul_demandeur(client, employee):
    emp = employee([prompt("PROMPT-PRIVE-A"), assistant([text_block("REPONSE-PRIVEE-B")], stop="end_turn")])
    other_seen = []
    with connect(client) as other, connect(client) as ws:
        handshake(other)
        handshake(ws)
        open_chat(ws, emp.id)
        drain(other, other_seen)
    assert of(other_seen, "chat_history") == []
    dump = json.dumps(other_seen, ensure_ascii=False)
    assert "PROMPT-PRIVE-A" not in dump and "REPONSE-PRIVEE-B" not in dump


# ---------------------------------------------------------------- erreurs


def test_agent_inconnu_liste_vide_et_erreur(client, employee):
    employee([prompt("x")])
    unknown = "o-" + uuid.uuid4().hex[:8]
    with connect(client) as ws:
        handshake(ws)
        hist = open_chat(ws, unknown)
    assert hist["entries"] == []
    assert hist.get("error")
    with connect(client) as ws:  # le serveur répond toujours
        handshake(ws)


def test_agent_inconnu_sans_observateur(client, monkeypatch):
    monkeypatch.setattr(app_mod, "observer", None, raising=False)
    with connect(client) as ws:
        handshake(ws)
        hist = open_chat(ws, "o-deadbeef")
    assert hist["entries"] == [] and hist.get("error")


def test_employe_sans_transcript_liste_vide_et_erreur(client, employee):
    emp = employee(with_transcript=False)
    with connect(client) as ws:
        handshake(ws)
        hist = open_chat(ws, emp.id)
    assert hist["entries"] == [] and hist.get("error")


# ---------------------------------------------------------------- chat_entry en direct


def test_nouvelle_entree_envoyee_au_seul_abonne_puis_plus_rien_apres_close(client, employee):
    emp = employee([prompt("historique")])
    seen, other_seen = [], []
    with connect(client) as other, connect(client) as ws:
        handshake(other)
        handshake(ws)
        open_chat(ws, emp.id)

        emp.write(prompt("NOUVEAU-PROMPT-LIVE"))
        emp.poll()
        live = recv_until(ws, lambda e: e.get("type") == "chat_entry", seen=seen)
        assert live["agent_id"] == emp.id
        assert live["entry"]["role"] == "user" and live["entry"]["kind"] == "text"
        assert "NOUVEAU-PROMPT-LIVE" in live["entry"]["text"]

        ws.send_json({"type": "close_chat", "agent_id": emp.id})
        open_chat(ws, "o-" + uuid.uuid4().hex[:8])  # synchronisation : close_chat traité
        seen.clear()
        emp.write(prompt("APRES-FERMETURE"))
        emp.poll()
        drain(ws, seen)
        drain(other, other_seen)
    assert of(seen, "chat_entry") == []
    assert of(other_seen, "chat_entry") == []
    dump = json.dumps(other_seen, ensure_ascii=False)
    assert "NOUVEAU-PROMPT-LIVE" not in dump and "APRES-FERMETURE" not in dump


def test_entrees_en_direct_filtrees_et_ordonnees(client, employee):
    emp = employee([prompt("historique")])
    records, forbidden = full_transcript(tag="-LIVE")
    seen = []
    with connect(client) as ws:
        handshake(ws)
        open_chat(ws, emp.id)
        emp.write(*records)
        emp.poll()
        for _ in range(4):
            recv_until(ws, lambda e: e.get("type") == "chat_entry", seen=seen)
        drain(ws, seen)
    lives = of(seen, "chat_entry")
    assert all(e["agent_id"] == emp.id for e in lives)
    assert_full_transcript_entries([e["entry"] for e in lives], tag="-LIVE")
    dump = json.dumps(lives, ensure_ascii=False)
    for f in forbidden:
        assert f not in dump, f


def test_sortie_d_outil_en_direct_tronquee(client, employee):
    emp = employee([prompt("historique")])
    seen = []
    with connect(client) as ws:
        handshake(ws)
        open_chat(ws, emp.id)
        emp.write(assistant([tool_use_block()]), tool_result("x" * 10_000))
        emp.poll()
        recv_until(ws, lambda e: e.get("type") == "chat_entry" and e["entry"]["kind"] == "tool_result", seen=seen)
    res = [e["entry"] for e in of(seen, "chat_entry") if e["entry"]["kind"] == "tool_result"]
    _assert_truncated(res[0]["text"])


def test_sans_open_chat_aucune_entree(client, employee):
    emp = employee([prompt("historique")])
    seen = []
    with connect(client) as ws:
        handshake(ws)
        emp.write(prompt("PERSONNE-NE-REGARDE"), tool_result("SORTIE-NON-DIFFUSEE"))
        emp.poll()
        drain(ws, seen)
    assert of(seen, "chat_entry") == []
    dump = json.dumps(seen, ensure_ascii=False)
    assert "PERSONNE-NE-REGARDE" not in dump and "SORTIE-NON-DIFFUSEE" not in dump


def test_contenu_jamais_diffuse_aux_autres_clients(client, employee):
    emp = employee([prompt("HIST-PROMPT"), assistant([text_block("HIST-REPONSE"), tool_use_block()]),
                    tool_result("HIST-SORTIE")])
    other_seen = []
    with connect(client) as other, connect(client) as ws:
        other_seen.append(handshake(other))
        handshake(ws)
        open_chat(ws, emp.id)
        emp.write(prompt("LIVE-PROMPT"), assistant([text_block("LIVE-REPONSE"), tool_use_block("toolu_2")]),
                  tool_result("LIVE-SORTIE", tid="toolu_2"))
        emp.poll()
        recv_until(ws, lambda e: e.get("type") == "chat_entry" and "LIVE-SORTIE" in (e["entry"].get("text") or ""))
        drain(other, other_seen)
    dump = json.dumps(other_seen, ensure_ascii=False)
    for marker in ("HIST-PROMPT", "HIST-REPONSE", "HIST-SORTIE", "LIVE-PROMPT", "LIVE-REPONSE", "LIVE-SORTIE"):
        assert marker not in dump, marker


# ---------------------------------------------------------------- confidentialité


def test_contenu_jamais_journalise(client, employee, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    emp = employee([prompt("JOURNAL-HIST"), tool_result("JOURNAL-SORTIE-HIST")])
    with connect(client) as ws:
        handshake(ws)
        open_chat(ws, emp.id)
        emp.write(prompt("JOURNAL-LIVE"))
        emp.poll()
        recv_until(ws, lambda e: e.get("type") == "chat_entry")
        ws.send_json({"type": "close_chat", "agent_id": emp.id})
        open_chat(ws, "o-" + uuid.uuid4().hex[:8])
    out, err = capfd.readouterr()
    logs = caplog.text + out + err
    for marker in ("JOURNAL-HIST", "JOURNAL-SORTIE-HIST", "JOURNAL-LIVE"):
        assert marker not in logs, marker


def test_aucune_route_http_n_expose_le_transcript(client, employee):
    emp = employee([prompt("HTTP-SECRET-PROMPT")])
    for route in app_mod.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods or path == "/ws":
            continue
        url = path.replace("{agent_id}", emp.id).replace("{session_id}", emp.sid)
        if "{" in url:
            url = url.split("{")[0] + emp.id
        r = client.get(url)
        assert "HTTP-SECRET-PROMPT" not in r.text, path
