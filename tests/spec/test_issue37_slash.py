"""Tests d'intention pour l'issue #37 : commandes slash depuis le panneau de chat d'un employé piloté.

Écrits en boîte noire depuis l'issue et son dernier commentaire (qui fixe le contrat), sans lire le
corps des fonctions de backend/. Seul le backend est couvert ; l'autocomplétion (front) est vérifiée à la main.

Contrat public supposé (dernier commentaire de #37) :
- Une fois connecté, le backend lit `await client.get_server_info()["commands"]` et émet
  `{"type": "commands", "agent_id", "commands": [{"name", "description", "argumentHint"}]}` ;
  la liste est gardée dans le snapshot sous `commands` (supposé : dict agent_id → liste).
- Ticket `/model <nom>` → `client.set_model(nom)`, aucun `query` ; ticket terminé `ok: true`.
- Ticket `/clear` → nouvelle session SDK (nouveau client, pas de resume), `session_id` remis à None,
  événement `chat_cleared` (on accepte tout type commençant par « chat_clear ») ; ticket terminé `ok: true`.
- Autre `/nom args` présent dans la liste → envoyé tel quel comme prompt.
- `/nom` absent de la liste (liste connue) → `ticket_rejected` avec `reason`, rien n'est envoyé.

Hypothèses : le `session_id` de l'employé vient du `ResultMessage` du faux SDK (« s-fake ») ; l'employé
est accessible via `backend.app.employees[agent_id]`. « Fatigue à 0 » après /clear non testé (champ non fixé).

Harnais : faux ClaudeSDKClient de test_issue36_interrupt, étendu avec `get_server_info` et `set_model`.
"""
import uuid

import anyio
import anyio.from_thread
import pytest
from fastapi.testclient import TestClient

import backend.app as app_mod
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until
from tests.spec.test_issue25_chat import append, prompt, transcript_path
from tests.spec.test_issue36_interrupt import INSTANCES, FakeClient, hire, is_

COMMANDS = [
    {"name": "compact", "description": "Compacte la conversation", "argumentHint": ""},
    {"name": "model", "description": "Change de modèle", "argumentHint": "[model]"},
    {"name": "clear", "description": "Nouvelle conversation", "argumentHint": ""},
    {"name": "code-review", "description": "Revue de code", "argumentHint": "[pr]"},
]


class SlashClient(FakeClient):
    def __init__(self, options):
        super().__init__(options)
        self.models: list[str] = []

    async def get_server_info(self):
        return {"commands": [dict(c) for c in COMMANDS]}

    async def set_model(self, model=None):
        self.models.append(model)


@pytest.fixture
def cfg(tmp_path):
    c = tmp_path / "claude-config"
    (c / "projects").mkdir(parents=True)
    return c


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", cfg)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", SlashClient)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        yield c
        c.portal = None


def ready(ws, tmp_path):
    """Employé piloté connecté, liste de commandes reçue, inactif ; renvoie (agent_id, session)."""
    aid, tid, session = hire(ws, tmp_path, f"rapide-{uuid.uuid4().hex}")
    recv_until(ws, is_("ticket_done", ticket_id=tid))
    drain(ws)
    return aid, session


def send(ws, aid, title):
    """Envoie un ticket à `aid` et renvoie son ticket_id."""
    ws.send_json({"type": "new_ticket", "title": title, "agent_id": aid})
    return recv_until(ws, lambda e: e.get("type") == "ticket_created"
                      and e["ticket"]["title"] == title)["ticket"]["id"]


def sessions_of(session):
    return [i for i in INSTANCES if isinstance(i, SlashClient) and i.cwd == session.cwd]


def all_prompts(session):
    return [p for s in sessions_of(session) for p in s.prompts]


def names(cmds):
    return sorted(c["name"] for c in cmds)


# ---------------------------------------------------------------- liste des commandes


def test_evenement_commands_apres_connexion(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, tid, _ = hire(ws, tmp_path, f"rapide-{uuid.uuid4().hex}")
        ev = recv_until(ws, is_("commands", agent_id=aid))
    assert names(ev["commands"]) == names(COMMANDS)
    model = [c for c in ev["commands"] if c["name"] == "model"][0]
    assert model["argumentHint"] == "[model]"
    assert "description" in model


def test_snapshot_garde_les_commandes(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, _ = ready(ws, tmp_path)
    with connect(client) as ws:
        _, snap = handshake(ws)
    assert names(snap["commands"][aid]) == names(COMMANDS)


# ---------------------------------------------------------------- /model


def test_model_appelle_set_model_sans_query(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        tid = send(ws, aid, "/model sonnet")
        done = recv_until(ws, is_("ticket_done", ticket_id=tid))
    assert done["ok"] is True, done
    assert [m for s in sessions_of(session) for m in s.models] == ["sonnet"]
    assert not [p for p in all_prompts(session) if "/model" in p]


# ---------------------------------------------------------------- /clear


def test_clear_nouvelle_session_et_historique_vide(client, cfg, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        emp = app_mod.employees[aid]
        assert emp.session_id, "l'employé doit avoir une session après son premier ticket"
        append(transcript_path(cfg, emp.session_id, session.cwd), prompt("AVANT-CLEAR"))
        ws.send_json({"type": "open_chat", "agent_id": aid})
        before = recv_until(ws, is_("chat_history", agent_id=aid))
        assert any("AVANT-CLEAR" in (e.get("text") or "") for e in before["entries"]), before

        tid = send(ws, aid, "/clear")
        cleared = recv_until(ws, lambda e: str(e.get("type", "")).startswith("chat_clear"))
        done = recv_until(ws, is_("ticket_done", ticket_id=tid))
        assert emp.session_id is None
        ws.send_json({"type": "open_chat", "agent_id": aid})
        after = recv_until(ws, is_("chat_history", agent_id=aid))
    assert cleared.get("agent_id") == aid, cleared
    assert done["ok"] is True, done
    assert after["entries"] == [], after
    assert not [p for p in all_prompts(session) if "/clear" in p]


def test_ticket_apres_clear_sur_une_nouvelle_session(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        tid = send(ws, aid, "/clear")
        recv_until(ws, is_("ticket_done", ticket_id=tid))
        title = f"suite-{uuid.uuid4().hex}"
        tid2 = send(ws, aid, title)
        done2 = recv_until(ws, is_("ticket_done", ticket_id=tid2))
    assert done2["ok"] is True, done2
    (fresh,) = [s for s in sessions_of(session) if any(title in p for p in s.prompts)]
    assert fresh is not session, "après /clear le ticket doit partir sur un nouveau client SDK"
    assert not getattr(fresh.options, "resume", None), "la nouvelle session ne doit pas reprendre l'ancienne"


# ---------------------------------------------------------------- commandes passées telles quelles


@pytest.mark.parametrize("cmd", ["/code-review 12", "/compact"])
def test_commande_listee_envoyee_comme_prompt(client, tmp_path, cmd):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        tid = send(ws, aid, cmd)
        done = recv_until(ws, is_("ticket_done", ticket_id=tid))
    assert done["ok"] is True, done
    assert cmd in session.prompts, session.prompts


def test_commande_inconnue_rejetee_rien_envoye(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        ws.send_json({"type": "new_ticket", "title": "/login", "agent_id": aid})
        rej = recv_until(ws, lambda e: e.get("type") == "ticket_rejected")
        after = drain(ws)
    assert isinstance(rej.get("reason"), str) and rej["reason"].strip(), rej
    assert not [p for p in all_prompts(session) if "/login" in p]
    assert not [e for e in after if e.get("type") == "ticket_assigned"], after


def test_ticket_texte_normal_inchange(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        title = f"corrige le bug {uuid.uuid4().hex}"
        tid = send(ws, aid, title)
        done = recv_until(ws, is_("ticket_done", ticket_id=tid))
    assert done["ok"] is True, done
    assert any(title in p for p in session.prompts)
    assert session.models == []
