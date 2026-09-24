"""Tests d'intention pour l'issue #12 : tickets assignés à un employé, un projet par employé.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.

Contrat public supposé :
- Module `backend/projects.py` :
  `recent_projects(config_dir: Path, extra: str | None = None, limit: int = 20) -> list[dict]`,
  chaque élément `{"cwd": <chemin absolu>, "name": <nom du dossier>}`.
  Lit le champ `cwd` des lignes JSON des fichiers `config_dir/projects/*/*.jsonl` ;
  récence = mtime du fichier transcript (plus récent d'abord) ; `extra` (OPENSPACE_CWD) en tête
  s'il existe ; sans doublon ; dossiers inexistants exclus ; lignes JSON invalides ignorées ;
  au plus `limit` éléments.
- `backend.app.CLAUDE_CONFIG_DIR` (Path) est lu à l'exécution : monkeypatché vers un `tmp_path`.
- `backend.app.ClaudeSDKClient` est résolu à l'exécution : remplacé par `FakeClient`
  (async context manager ; `__init__(options)` enregistre `options.cwd` ; `query(prompt)` ;
  `receive_response()` renvoie un `ResultMessage` ; `get_context_usage()`).
- À la connexion : `hello` contient `team` (liste de {id, name, cwd, project}) et `projects`
  (résultat de `recent_projects`). Module fraîchement importé : `team` vide.
- `{"type": "new_ticket", "title": T, "cwd": D}` (D existant) : `agent_hired`
  {"agent": {id, name, cwd: D, project: basename(D)}}, puis `ticket_created`, `ticket_assigned`
  (agent_id du nouvel employé), puis `ticket_done`. La session a été créée avec cwd == D et a reçu T.
- `{"type": "new_ticket", "title": T, "agent_id": A}` (A existant) : `ticket_assigned` avec
  agent_id == A ; T reçu par la même instance de session que le ticket précédent de A ;
  aucun nouvel `agent_hired`.
- Deux tickets d'affilée pour A : traités dans l'ordre d'envoi, l'un après l'autre, par la même instance.
- `agent_id` inconnu, `cwd` inexistant, ou ni l'un ni l'autre : `{"type": "ticket_rejected",
  "reason": <texte>}` ; aucun `agent_hired`, aucune session créée, aucun `ticket_assigned`.
- Le `snapshot` contient `agents` (même forme que `hello.team`) incluant les employés recrutés.

Hypothèses ajoutées :
- Le prompt reçu par la session *contient* le titre du ticket (il peut être enrichi).
- `ticket_created` porte `ticket.title` et `ticket.id` ; `ticket_assigned`/`ticket_done` portent `ticket_id`.
- Le test « démarrage sans employé » tourne dans un sous-processus (module fraîchement importé),
  car `hub` et la liste des employés sont des singletons partagés par toute la session pytest.

Pas de vrai Claude, pas de vrai ~/.claude. TestClient sans bloc `with` (lifespan non lancé),
portail partagé comme dans test_issue4_replay.py. Vérifications par ids / différences.
"""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import ResultMessage
from fastapi.testclient import TestClient

import backend.app as app_mod

ORIGIN = {"origin": "http://testserver"}
TIMEOUT = 3
ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------- fausse session Claude

INSTANCES: list = []


class FakeClient:
    def __init__(self, options):
        self.cwd = str(getattr(options, "cwd", None))
        self.prompts: list[str] = []
        self.active = 0
        self.max_active = 0
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
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.prompts.append(prompt if isinstance(prompt, str) else str(prompt))

    async def receive_response(self):
        await asyncio.sleep(0.05)
        self.active -= 1
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id=f"s-{id(self)}", total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1}, result="ok",
        )

    async def get_context_usage(self):
        return {"totalTokens": 1000, "maxTokens": 100000}


def instances_for(cwd) -> list:
    return [i for i in INSTANCES if i.cwd == str(cwd)]


def instance_with_prompt(title):
    found = [i for i in INSTANCES if any(title in p for p in i.prompts)]
    assert len(found) == 1, f"le ticket {title!r} doit être reçu par exactement une session"
    return found[0]


# ---------------------------------------------------------------- helpers WebSocket (cf. issue #4)


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "claude-config"
    (cfg / "projects").mkdir(parents=True)
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", cfg)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        c.config_dir = cfg
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


def drain(ws, duration=0.3):
    out = []
    try:
        while True:
            out.append(recv(ws, duration))
    except TimeoutError:
        return out


def handshake(ws):
    hello = recv(ws)
    assert hello["type"] == "hello"
    snap = recv(ws)
    assert snap["type"] == "snapshot"
    return hello, snap


def project_dir(tmp_path, name=None):
    d = tmp_path / (name or f"proj-{uuid.uuid4().hex[:8]}")
    d.mkdir()
    return d


def hire(ws, cwd):
    """Envoie un ticket vers un nouvel employé dans `cwd` ; renvoie (agent, title, events)."""
    title = f"t-{uuid.uuid4().hex}"
    ws.send_json({"type": "new_ticket", "title": title, "cwd": str(cwd)})
    seen = []
    hired = recv_until(ws, lambda e: e.get("type") == "agent_hired"
                       and e["agent"]["cwd"] == str(cwd), seen=seen)
    created = recv_until(ws, lambda e: e.get("type") == "ticket_created"
                         and e["ticket"]["title"] == title, seen=seen)
    tid = created["ticket"]["id"]
    recv_until(ws, lambda e: e.get("type") == "ticket_done" and e.get("ticket_id") == tid, seen=seen)
    return hired["agent"], title, seen


# ---------------------------------------------------------------- recent_projects


def recent_projects(*a, **k):
    from backend.projects import recent_projects as rp  # import tardif : échec par test, pas à la collecte

    return rp(*a, **k)


def write_transcript(cfg, slug, cwds, mtime, extra_lines=()):
    d = cfg / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{uuid.uuid4().hex}.jsonl"
    lines = [json.dumps({"type": "user", "cwd": str(c)}) for c in cwds] + list(extra_lines)
    f.write_text("\n".join(lines) + "\n")
    os.utime(f, (mtime, mtime))
    return f


def test_projets_tries_par_recence_avec_nom_court(tmp_path):
    cfg = tmp_path / "cfg"
    a, b, c = (project_dir(tmp_path, n) for n in ("alpha", "beta", "gamma"))
    write_transcript(cfg, "s1", [a], 1000)
    write_transcript(cfg, "s2", [b], 3000)
    write_transcript(cfg, "s3", [c], 2000)
    assert recent_projects(cfg) == [
        {"cwd": str(b), "name": "beta"},
        {"cwd": str(c), "name": "gamma"},
        {"cwd": str(a), "name": "alpha"},
    ]


def test_projets_sans_doublon(tmp_path):
    cfg = tmp_path / "cfg"
    a, b = project_dir(tmp_path, "alpha"), project_dir(tmp_path, "beta")
    write_transcript(cfg, "s1", [a, a], 1000)
    write_transcript(cfg, "s2", [b], 2000)
    write_transcript(cfg, "s3", [a], 3000)
    cwds = [p["cwd"] for p in recent_projects(cfg)]
    assert cwds == [str(a), str(b)]


def test_projets_inexistants_et_lignes_invalides_ignores(tmp_path):
    cfg = tmp_path / "cfg"
    a = project_dir(tmp_path, "alpha")
    ghost = tmp_path / "disparu"
    write_transcript(cfg, "s1", [a], 1000, extra_lines=["{pas du json", "", json.dumps({"type": "x"})])
    write_transcript(cfg, "s2", [ghost], 2000)
    assert recent_projects(cfg) == [{"cwd": str(a), "name": "alpha"}]


def test_projets_limites_a_20(tmp_path):
    cfg = tmp_path / "cfg"
    dirs = [project_dir(tmp_path, f"p{i:02d}") for i in range(25)]
    for i, d in enumerate(dirs):
        write_transcript(cfg, f"s{i}", [d], 1000 + i)
    got = recent_projects(cfg)
    assert len(got) == 20
    assert [p["cwd"] for p in got] == [str(d) for d in reversed(dirs)][:20]
    assert len(recent_projects(cfg, limit=5)) == 5


def test_projet_extra_en_tete_sans_doublon(tmp_path):
    cfg = tmp_path / "cfg"
    a, b = project_dir(tmp_path, "alpha"), project_dir(tmp_path, "beta")
    write_transcript(cfg, "s1", [a], 1000)
    write_transcript(cfg, "s2", [b], 2000)
    cwds = [p["cwd"] for p in recent_projects(cfg, extra=str(a))]
    assert cwds == [str(a), str(b)]


def test_projet_extra_inexistant_exclu(tmp_path):
    cfg = tmp_path / "cfg"
    a = project_dir(tmp_path, "alpha")
    write_transcript(cfg, "s1", [a], 1000)
    assert [p["cwd"] for p in recent_projects(cfg, extra=str(tmp_path / "nope"))] == [str(a)]


def test_projets_config_vide(tmp_path):
    assert recent_projects(tmp_path / "vide") == []


# ---------------------------------------------------------------- connexion


def test_demarrage_sans_employe_pilote(tmp_path):
    """Module fraîchement importé (sous-processus) : hello.team et snapshot.agents vides."""
    script = (
        "import json\n"
        "from fastapi.testclient import TestClient\n"
        "from backend.app import app\n"
        "with TestClient(app).websocket_connect('/ws', headers={'origin': 'http://testserver'}) as ws:\n"
        "    hello = ws.receive_json(); snap = ws.receive_json()\n"
        "print(json.dumps({'hello': hello, 'snap': snap}))\n"
    )
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"), "HOME": str(tmp_path)}
    env.pop("OPENSPACE_CWD", None)
    out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["hello"]["team"] == []
    assert isinstance(data["hello"]["projects"], list)
    assert data["snap"]["agents"] == []


def test_hello_contient_les_projets_recents(client, tmp_path):
    d = project_dir(tmp_path, "eter")
    write_transcript(client.config_dir, "s1", [d], 5000)
    with connect(client) as ws:
        hello, _ = handshake(ws)
    assert {"cwd": str(d), "name": "eter"} in hello["projects"]
    assert isinstance(hello["team"], list)


# ---------------------------------------------------------------- routage des tickets


def test_ticket_avec_cwd_recrute_un_employe_dans_ce_dossier(client, tmp_path):
    d = project_dir(tmp_path, "eter")
    with connect(client) as ws:
        handshake(ws)
        agent, title, seen = hire(ws, d)
    assert agent["cwd"] == str(d)
    assert agent["project"] == "eter"
    assert agent["id"] and agent["name"]
    types = [e["type"] for e in seen]
    assert types.index("agent_hired") < types.index("ticket_created")
    assigned = [e for e in seen if e["type"] == "ticket_assigned"]
    assert [e["agent_id"] for e in assigned] == [agent["id"]]
    sessions = instances_for(d)
    assert len(sessions) == 1
    assert instance_with_prompt(title) is sessions[0]


def test_employe_recrute_visible_dans_hello_et_snapshot(client, tmp_path):
    d = project_dir(tmp_path)
    with connect(client) as ws:
        handshake(ws)
        agent, _, _ = hire(ws, d)
    with connect(client) as ws:
        hello, snap = handshake(ws)
    expected = {"id": agent["id"], "name": agent["name"], "cwd": str(d), "project": d.name}
    for team in (hello["team"], snap["agents"]):
        match = [a for a in team if a["id"] == agent["id"]]
        assert len(match) == 1
        assert {k: match[0][k] for k in expected} == expected


def test_ticket_vers_employe_existant_meme_session(client, tmp_path):
    d1, d2 = project_dir(tmp_path), project_dir(tmp_path)
    with connect(client) as ws:
        handshake(ws)
        a1, t1, _ = hire(ws, d1)
        a2, _, _ = hire(ws, d2)
        title = f"t-{uuid.uuid4().hex}"
        ws.send_json({"type": "new_ticket", "title": title, "agent_id": a1["id"]})
        seen = []
        created = recv_until(ws, lambda e: e.get("type") == "ticket_created"
                             and e["ticket"]["title"] == title, seen=seen)
        tid = created["ticket"]["id"]
        assigned = recv_until(ws, lambda e: e.get("type") == "ticket_assigned"
                              and e.get("ticket_id") == tid, seen=seen)
        recv_until(ws, lambda e: e.get("type") == "ticket_done" and e.get("ticket_id") == tid, seen=seen)
    assert assigned["agent_id"] == a1["id"]
    assert not [e for e in seen if e["type"] == "agent_hired"]
    assert instance_with_prompt(title) is instance_with_prompt(t1)
    assert len(instances_for(d1)) == 1
    assert instance_with_prompt(title).cwd == str(d1)


def test_deux_tickets_traites_dans_l_ordre_par_la_meme_session(client, tmp_path):
    d = project_dir(tmp_path)
    with connect(client) as ws:
        handshake(ws)
        agent, _, _ = hire(ws, d)
        t1, t2 = f"t1-{uuid.uuid4().hex}", f"t2-{uuid.uuid4().hex}"
        ws.send_json({"type": "new_ticket", "title": t1, "agent_id": agent["id"]})
        ws.send_json({"type": "new_ticket", "title": t2, "agent_id": agent["id"]})
        seen, ids = [], {}
        while len(ids) < 2:
            ev = recv(ws)
            seen.append(ev)
            if ev.get("type") == "ticket_created" and ev["ticket"]["title"] in (t1, t2):
                ids[ev["ticket"]["title"]] = ev["ticket"]["id"]
        id1, id2 = ids[t1], ids[t2]
        recv_until(ws, lambda e: e.get("type") == "ticket_done" and e.get("ticket_id") == id2, seen=seen)

    def pos(type_, tid):
        return next(i for i, e in enumerate(seen) if e.get("type") == type_ and e.get("ticket_id") == tid)

    assert pos("ticket_done", id1) < pos("ticket_assigned", id2), "le second ticket attend la fin du premier"
    for tid in (id1, id2):
        assert seen[pos("ticket_assigned", tid)]["agent_id"] == agent["id"]
    session = instance_with_prompt(t1)
    assert session is instance_with_prompt(t2)
    order = [p for p in session.prompts if t1 in p or t2 in p]
    assert t1 in order[0] and t2 in order[1]
    assert session.max_active == 1


@pytest.mark.parametrize("payload", [
    lambda tmp: {"agent_id": f"inconnu-{uuid.uuid4().hex}"},
    lambda tmp: {"cwd": str(tmp / "n-existe-pas")},
    lambda tmp: {},
], ids=["agent_id_inconnu", "cwd_inexistant", "sans_destination"])
def test_ticket_refuse(client, tmp_path, payload):
    with connect(client) as ws:
        _, snap_before = handshake(ws)
        n_sessions = len(INSTANCES)
        title = f"t-{uuid.uuid4().hex}"
        ws.send_json({"type": "new_ticket", "title": title, **payload(tmp_path)})
        seen = []
        rejected = recv_until(ws, lambda e: e.get("type") == "ticket_rejected", seen=seen)
        seen += drain(ws)
    assert isinstance(rejected.get("reason"), str) and rejected["reason"]
    assert not [e for e in seen if e.get("type") in ("agent_hired", "ticket_assigned")]
    assert len(INSTANCES) == n_sessions
    assert not any(title in p for i in INSTANCES for p in i.prompts)
    with connect(client) as ws:
        _, snap_after = handshake(ws)
    assert {a["id"] for a in snap_after["agents"]} == {a["id"] for a in snap_before["agents"]}
    assert not [t for t in snap_after["tickets"] if t["title"] == title and t["status"] != "queued"]
