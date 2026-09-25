"""Tests d'intention pour l'issue #62 : « Dépensé » ne doit pas sur-compter le total cumulé du SDK.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.

Contrat public supposé :
- En streaming, `ResultMessage.total_cost_usd` est un total cumulé de la session SDK.
- Chaque réponse d'un employé piloté émet `{"type": "cost", "agent_id", "usd", "tokens"}` avec
  `usd` = coût de CETTE réponse (delta du cumul) ; `snapshot.totals.usd` = somme des deltas.
- `ticket_done.usd` = coût du ticket seul.
- La base du cumul repart de 0 à un nouveau client (`/clear`) et à un `ConversationResetMessage`.
- Un cumul inférieur au précédent (reset inattendu) compte pour sa valeur, jamais négatif.
- Front : le stat « Dépensé » (id `sCost`) porte un `title` mentionnant « API ».

Harnais : faux SDK dont le prompt choisit le script (`COST=<cumul>`, `RESET` → un
`ConversationResetMessage` avant le résultat), cf. test_issue36_interrupt / test_issue37_slash.
Le hub étant partagé entre tests, `totals.usd` est mesuré en différence avant/après.
"""
import re
import uuid
from pathlib import Path

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import ConversationResetMessage, ResultMessage
from fastapi.testclient import TestClient

import backend.app as app_mod
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until

INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
COST = re.compile(r"COST=([0-9.]+)")


class FakeClient:
    def __init__(self, options):
        self.options = options
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

    async def get_server_info(self):
        return {"commands": [{"name": "clear", "description": "Nouvelle conversation", "argumentHint": ""}]}

    async def receive_response(self):
        prompt = self.prompts[-1] if self.prompts else ""
        if "RESET" in prompt:
            yield ConversationResetMessage(
                new_conversation_id=f"c-{uuid.uuid4().hex}", uuid=uuid.uuid4().hex, session_id="s-fake")
        m = COST.search(prompt)
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s-fake", total_cost_usd=float(m.group(1)) if m else 0.0,
            usage={"input_tokens": 1, "output_tokens": 1}, result="ok",
        )

    async def get_context_usage(self):
        return {"totalTokens": 1000, "maxTokens": 100000}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "claude-config"
    (cfg / "projects").mkdir(parents=True)
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", cfg)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        yield c
        c.portal = None


def is_(type_, **fields):
    return lambda e: e.get("type") == type_ and all(e.get(k) == v for k, v in fields.items())


def total_usd(client):
    with connect(client) as ws:
        _, snap = handshake(ws)
    return snap["totals"]["usd"]


class Pilot:
    """Un employé piloté ; `ticket(label)` renvoie (ticket_done, événements `cost` de l'employé)."""

    def __init__(self, ws, tmp_path):
        self.ws, self.aid = ws, None
        self.cwd = tmp_path / f"proj-{uuid.uuid4().hex[:8]}"
        self.cwd.mkdir()

    def ticket(self, label):
        title = label if label.startswith("/") else f"{label} {uuid.uuid4().hex}"
        msg = {"type": "new_ticket", "title": title}
        if self.aid is None:
            msg["cwd"] = str(self.cwd)
        else:
            msg["agent_id"] = self.aid
        seen: list[dict] = []
        self.ws.send_json(msg)
        tid = recv_until(self.ws, lambda e: e.get("type") == "ticket_created"
                         and e["ticket"]["title"] == title, seen=seen)["ticket"]["id"]
        self.aid = recv_until(self.ws, is_("ticket_assigned", ticket_id=tid), seen=seen)["agent_id"]
        done = recv_until(self.ws, is_("ticket_done", ticket_id=tid), seen=seen)
        seen += drain(self.ws)
        return done, [e["usd"] for e in seen if is_("cost", agent_id=self.aid)(e)]


def run(client, tmp_path, *labels):
    """Joue les tickets `labels` sur un même employé ; renvoie (dones, coûts, delta de totals.usd)."""
    before = total_usd(client)
    with connect(client) as ws:
        handshake(ws)
        p = Pilot(ws, tmp_path)
        results = [p.ticket(label) for label in labels]
    return [d for d, _ in results], [c for _, cs in results for c in cs], total_usd(client) - before


# ---------------------------------------------------------------- cumul → delta


def test_deux_reponses_cumulees_emettent_des_deltas(client, tmp_path):
    dones, costs, total = run(client, tmp_path, "COST=0.10", "COST=0.25")
    assert costs == [pytest.approx(0.10), pytest.approx(0.15)], costs
    assert total == pytest.approx(0.25)


def test_ticket_done_usd_est_le_cout_du_ticket(client, tmp_path):
    dones, _, _ = run(client, tmp_path, "COST=0.10", "COST=0.25")
    assert [d["usd"] for d in dones] == [pytest.approx(0.10), pytest.approx(0.15)], dones


# ---------------------------------------------------------------- remise à zéro de la base


def test_clear_repart_de_zero(client, tmp_path):
    dones, costs, total = run(client, tmp_path, "COST=0.10", "COST=0.25", "/clear", "COST=0.05")
    assert dones[2]["ok"] is True, dones[2]
    assert [c for c in costs if c] == [pytest.approx(0.10), pytest.approx(0.15), pytest.approx(0.05)], costs
    assert dones[3]["usd"] == pytest.approx(0.05)
    assert total == pytest.approx(0.30)


def test_conversation_reset_repart_de_zero(client, tmp_path):
    dones, costs, total = run(client, tmp_path, "COST=0.10", "COST=0.25", "RESET COST=0.05")
    assert costs == [pytest.approx(0.10), pytest.approx(0.15), pytest.approx(0.05)], costs
    assert dones[2]["usd"] == pytest.approx(0.05)
    assert total == pytest.approx(0.30)


def test_cumul_decroissant_compte_sa_valeur_jamais_negatif(client, tmp_path):
    dones, costs, total = run(client, tmp_path, "COST=0.10", "COST=0.25", "COST=0.05")
    assert all(c >= 0 for c in costs), costs
    assert costs == [pytest.approx(0.10), pytest.approx(0.15), pytest.approx(0.05)], costs
    assert dones[2]["usd"] == pytest.approx(0.05)
    assert total == pytest.approx(0.30)


# ---------------------------------------------------------------- front


def test_infobulle_sur_depense():
    html = INDEX.read_text(encoding="utf-8")
    stat = re.search(r"<div[^>]*>(?:(?!</div>).)*?id=\"sCost\"(?:(?!</div>).)*?</div>", html, re.S)
    assert stat and "Dépensé" in stat.group(0), "stat « Dépensé » introuvable"
    assert re.search(r'title="[^"]*API[^"]*"', stat.group(0)), stat.group(0)
