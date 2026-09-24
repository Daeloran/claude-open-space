"""Tests d'intention pour l'issue #17 : fenêtre de contexte des sessions observées depuis les réglages.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Aucun accès au vrai ~/.claude : tout vit dans `tmp_path`.

Contrat public supposé (`backend/observer.py`) :
- `context_window_for(cwd: str | Path, config_dir: Path, default: int) -> int` : lit le champ
  `model` de `<cwd>/.claude/settings.local.json`, puis `<cwd>/.claude/settings.json`, puis
  `<config_dir>/settings.json` ; le premier fichier qui définit `model` gagne. `[1m]` dans ce
  modèle → 1 000 000, sinon `default`. Fichiers absents / invalides / sans `model` : ignorés,
  sans exception.
- `Observer(config_dir, emit, pid_alive=..., context_window=200000)` utilise cette fenêtre par
  session (cwd du registre `sessions/*.json`) pour ses événements `context`. Règles de secours
  inchangées : `[1m]` dans le modèle du transcript → 1M ; tokens > fenêtre → 1M.
  (Contrat existant de l'Observer : voir la docstring de tests/spec/test_issue13_observer.py.)
"""
import asyncio
import json
import uuid
from pathlib import Path

import pytest

ONE_M = 1_000_000


def observer_mod():
    import backend.observer as m  # import tardif : échec par test, pas à la collecte

    return m


# ---------------------------------------------------------------- fixtures (copiées de #13)


def new_sid():
    return str(uuid.uuid4())


def write_session(cfg, pid, sid, cwd, entrypoint="cli", status="idle", name=None):
    d = cfg / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "pid": pid, "cwd": str(cwd), "entrypoint": entrypoint, "kind": "interactive",
        "name": name or f"{Path(cwd).name}-{pid}", "status": status, "sessionId": sid,
        "startedAt": 1790000000000, "version": "2.1.281",
    }
    (d / f"{pid}.json").write_text(json.dumps(rec))
    return rec


def transcript_path(cfg, sid, cwd):
    d = cfg / "projects" / str(cwd).replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sid}.jsonl"


def append(path, *records):
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def usage(inp=10, read=40000, create=9990, out=50):
    return {"input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": create, "output_tokens": out}


def assistant(content, u=None, model="claude-opus-5-5", sidechain=False, sid="x", cwd="/x"):
    msg = {"role": "assistant", "model": model, "content": content}
    if u is not None:
        msg["usage"] = u
    return {"type": "assistant", "isSidechain": sidechain, "sessionId": sid, "cwd": cwd, "message": msg}


def text(u, model="claude-opus-5-5"):
    return assistant([{"type": "text", "text": "ok"}], u=u, model=model)


def prompt(t="salut"):
    return {"type": "user", "message": {"role": "user", "content": t}}


class Alive:
    def __init__(self, *pids):
        self.pids = set(pids)

    def __call__(self, pid):
        return pid in self.pids


def aid(sid):
    return "o-" + sid[:8]


@pytest.fixture
def cfg(tmp_path):
    c = tmp_path / "cfg"
    (c / "sessions").mkdir(parents=True)
    (c / "projects").mkdir()
    return c


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "eter"
    d.mkdir()
    return d


def user_settings(cfg, content):
    p = cfg / "settings.json"
    p.write_text(content if isinstance(content, str) else json.dumps(content))


def project_settings(proj, content, local=False):
    d = proj / ".claude"
    d.mkdir(exist_ok=True)
    p = d / ("settings.local.json" if local else "settings.json")
    p.write_text(content if isinstance(content, str) else json.dumps(content))


class Harness:
    def __init__(self, cfg, alive, **kw):
        self.events: list[dict] = []
        self.loop = asyncio.new_event_loop()

        async def emit(ev):
            self.events.append(ev)

        self.obs = observer_mod().Observer(cfg, emit, pid_alive=alive, **kw)

    def poll(self):
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


def contexts(events, sid):
    return [e for e in events if e.get("type") == "context" and e.get("agent_id") == aid(sid)]


def ratio_after_new_line(cfg, proj, harness, u, model="claude-opus-5-5", **kw):
    """Session observée rejointe, puis une nouvelle ligne assistant avec `usage` → ratio émis."""
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt())
    write_session(cfg, 101, sid, proj)
    h = harness(cfg, Alive(101), **kw)
    h.poll()
    append(t, text(u, model=model))
    ctx = contexts(h.poll(), sid)
    assert len(ctx) == 1
    return ctx[0]["ratio"]


U_192K = usage(inp=3, read=180000, create=12586)  # 192 589 tokens


# ---------------------------------------------------------------- context_window_for


def test_reglage_utilisateur_1m(cfg, proj):
    user_settings(cfg, {"model": "opus[1m]"})
    assert observer_mod().context_window_for(proj, cfg, 200000) == ONE_M


def test_cwd_en_chaine(cfg, proj):
    user_settings(cfg, {"model": "opus[1m]"})
    assert observer_mod().context_window_for(str(proj), cfg, 200000) == ONE_M


def test_reglage_utilisateur_sans_1m(cfg, proj):
    user_settings(cfg, {"model": "sonnet"})
    assert observer_mod().context_window_for(proj, cfg, 200000) == 200000


def test_reglage_utilisateur_sans_model(cfg, proj):
    user_settings(cfg, {"theme": "dark", "permissions": {"allow": []}})
    assert observer_mod().context_window_for(proj, cfg, 123456) == 123456


def test_aucun_reglage(cfg, proj):
    assert observer_mod().context_window_for(proj, cfg, 123456) == 123456


def test_cwd_inexistant(cfg, tmp_path):
    assert observer_mod().context_window_for(tmp_path / "absent", cfg, 123456) == 123456


def test_projet_sans_1m_l_emporte_sur_utilisateur_1m(cfg, proj):
    user_settings(cfg, {"model": "opus[1m]"})
    project_settings(proj, {"model": "sonnet"})
    assert observer_mod().context_window_for(proj, cfg, 200000) == 200000


def test_local_prioritaire_sur_projet(cfg, proj):
    project_settings(proj, {"model": "sonnet"})
    project_settings(proj, {"model": "opus[1m]"}, local=True)
    assert observer_mod().context_window_for(proj, cfg, 200000) == ONE_M


def test_local_sans_1m_prioritaire_sur_projet_1m(cfg, proj):
    user_settings(cfg, {"model": "opus[1m]"})
    project_settings(proj, {"model": "opus[1m]"})
    project_settings(proj, {"model": "sonnet"}, local=True)
    assert observer_mod().context_window_for(proj, cfg, 200000) == 200000


def test_fichier_sans_model_ignore(cfg, proj):
    project_settings(proj, {"permissions": {"allow": []}}, local=True)
    project_settings(proj, {"env": {}})
    user_settings(cfg, {"model": "opus[1m]"})
    assert observer_mod().context_window_for(proj, cfg, 200000) == ONE_M


@pytest.mark.parametrize("bad", ["{pas du json", "", "[]", "null", '"opus[1m]"'],
                         ids=["syntaxe", "vide", "liste", "null", "chaine"])
def test_reglage_utilisateur_invalide_repli(cfg, proj, bad):
    user_settings(cfg, bad)
    assert observer_mod().context_window_for(proj, cfg, 123456) == 123456


@pytest.mark.parametrize("local", [True, False], ids=["local", "projet"])
def test_reglage_projet_invalide_ignore(cfg, proj, local):
    project_settings(proj, "{pas du json", local=local)
    user_settings(cfg, {"model": "opus[1m]"})
    assert observer_mod().context_window_for(proj, cfg, 200000) == ONE_M


def test_reglage_illisible_repli(cfg, proj):
    user_settings(cfg, {"model": "opus[1m]"})
    project_settings(proj, {"model": "opus[1m]"})
    (proj / ".claude" / "settings.json").chmod(0)
    (cfg / "settings.json").chmod(0)
    try:
        # illisibles : ignorés, sans exception (en root, la lecture réussit → 1M, accepté aussi)
        assert observer_mod().context_window_for(proj, cfg, 123456) in (123456, ONE_M)
    finally:
        (proj / ".claude" / "settings.json").chmod(0o644)
        (cfg / "settings.json").chmod(0o644)


# ---------------------------------------------------------------- Observer.poll()


def test_exemple_issue_nouvelle_ligne(cfg, proj, harness):
    user_settings(cfg, {"model": "opus[1m]"})
    assert ratio_after_new_line(cfg, proj, harness, U_192K) == pytest.approx(0.1926, abs=1e-4)


def test_exemple_issue_contexte_initial(cfg, proj, harness):
    user_settings(cfg, {"model": "opus[1m]"})
    sid = new_sid()
    append(transcript_path(cfg, sid, proj), prompt(), text(U_192K))
    write_session(cfg, 101, sid, proj)
    ctx = contexts(harness(cfg, Alive(101)).poll(), sid)
    assert len(ctx) == 1
    assert ctx[0]["ratio"] == pytest.approx(0.1926, abs=1e-4)


def test_observer_sans_model_utilise_context_window(cfg, proj, harness):
    user_settings(cfg, {"theme": "dark"})
    r = ratio_after_new_line(cfg, proj, harness, usage(inp=0, read=50000, create=0), context_window=100000)
    assert r == pytest.approx(0.5)


def test_observer_projet_sans_1m_l_emporte(cfg, proj, harness):
    user_settings(cfg, {"model": "opus[1m]"})
    project_settings(proj, {"model": "sonnet"})
    r = ratio_after_new_line(cfg, proj, harness, usage(inp=0, read=100000, create=0))
    assert r == pytest.approx(0.5)


def test_observer_reglage_invalide_repli(cfg, proj, harness):
    user_settings(cfg, "{pas du json")
    r = ratio_after_new_line(cfg, proj, harness, usage(inp=0, read=100000, create=0))
    assert r == pytest.approx(0.5)


def test_observer_fenetre_par_session(cfg, tmp_path, harness):
    """Deux sessions, deux projets : chacune sa fenêtre."""
    user_settings(cfg, {"model": "opus[1m]"})
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    project_settings(b, {"model": "sonnet"})
    sa, sb = new_sid(), new_sid()
    ta, tb = transcript_path(cfg, sa, a), transcript_path(cfg, sb, b)
    append(ta, prompt())
    append(tb, prompt())
    write_session(cfg, 101, sa, a)
    write_session(cfg, 102, sb, b)
    h = harness(cfg, Alive(101, 102))
    h.poll()
    u = usage(inp=0, read=100000, create=0)
    append(ta, text(u))
    append(tb, text(u))
    evs = h.poll()
    assert [e["ratio"] for e in contexts(evs, sa)] == [pytest.approx(0.1)]
    assert [e["ratio"] for e in contexts(evs, sb)] == [pytest.approx(0.5)]


def test_secours_modele_transcript_1m(cfg, proj, harness):
    project_settings(proj, {"model": "sonnet"})
    r = ratio_after_new_line(cfg, proj, harness, usage(inp=0, read=100000, create=0), model="claude-opus-5-5[1m]")
    assert r == pytest.approx(0.1)


def test_secours_depassement_fenetre(cfg, proj, harness):
    project_settings(proj, {"model": "sonnet"})
    r = ratio_after_new_line(cfg, proj, harness, usage(inp=0, read=250000, create=0))
    assert r == pytest.approx(0.25)
