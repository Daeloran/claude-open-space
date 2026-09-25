"""Tests d'intention pour l'issue #61 : un employé piloté reprend seul après la remise à zéro de la limite.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; l'affichage « en pause jusqu'à HH:MM » (front) est vérifié à la main.
Les employés terminal (observés) sont hors scope.

Contrat public supposé (noms laissés ouverts par l'issue, choisis ici) :
- Quand la réponse du SDK contient un `RateLimitEvent` dont `rate_limit_info.status == "rejected"`,
  le ticket n'est PAS terminé : pas de `ticket_done`. Le hub diffuse
  `{"type": "paused", "agent_id": A, "until": <ISO 8601 ou null>}` ; `until` correspond à `resets_at`
  (plus une marge ≤ 120 s). L'entrée de A dans `snapshot.agents` porte `paused_until` (non nul pendant
  la pause, nul/absent après la reprise) ; le ticket reste non terminé dans `snapshot.tickets`.
- L'attente passe par une coroutine de module `backend.app.wait_until_reset(seconds)`, remplacée ici
  pour enregistrer le délai et rendre la main tout de suite (ou bloquer sur une porte).
  Délai : `resets_at - maintenant` + marge ≤ 120 s ; sans `resets_at` : 300 s (+ marge ≤ 120 s).
- Après l'attente, le prompt exactement `"continue"` est envoyé à la MÊME instance de session
  (`backend.app.ClaudeSDKClient`, remplacée par `FakeClient`) ; le ticket se termine par
  `ticket_done` `ok: true` à la fin de cette réponse.
- Nouveau rejet sur la reprise : nouvelle pause (nouvel événement `paused`), toujours pas d'échec.
- Les tickets en attente ne sont pas envoyés à la session tant que la reprise n'est pas finie.
- `interrupt` pendant la pause : pause annulée, `ticket_done` `ok: false` avec une raison contenant
  « interrompu », aucun `"continue"` envoyé. `dismiss` pendant la pause : `agent_left` comme aujourd'hui,
  aucun `"continue"` envoyé.

Hypothèses du faux SDK : une réponse rejetée émet un `RateLimitEvent` `rejected` puis un
`ResultMessage` erroné (`is_error=True`, 429) — c'est ce que produit la CLI en pratique.

Harnais : TestClient sans lifespan, portail partagé (test_issue12_routing / test_issue36_interrupt).
"""
import asyncio
import json
import time
import uuid
from datetime import datetime

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo, ResultMessage
from fastapi.testclient import TestClient

import backend.app as app_mod
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until

MARGIN = 120
NO_RESET_DELAY = 300
INSTANCES: list["FakeClient"] = []
LOG: list[tuple[str, object]] = []  # ordre global des attentes et des prompts
PLAN = {"rejections": 0, "resets_at": None}  # scénario de la prochaine session créée


def result(is_error=False):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
        num_turns=1, session_id="s-fake", total_cost_usd=0.0,
        usage={"input_tokens": 1, "output_tokens": 1},
        result="Claude AI usage limit reached" if is_error else "ok",
        api_error_status=429 if is_error else None,
    )


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.cwd = str(options.cwd)
        self.prompts: list[str] = []
        self.rejections = PLAN["rejections"]
        self.resets_at = PLAN["resets_at"]
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
        p = prompt if isinstance(prompt, str) else str(prompt)
        self.prompts.append(p)
        LOG.append(("query", p))

    async def interrupt(self):
        pass

    async def receive_response(self):
        await asyncio.sleep(0)
        if self.rejections > 0:
            self.rejections -= 1
            info = RateLimitInfo(status="rejected", resets_at=self.resets_at, rate_limit_type="five_hour")
            yield RateLimitEvent(rate_limit_info=info, uuid=uuid.uuid4().hex, session_id="s-fake")
            yield result(is_error=True)
        else:
            yield result()

    async def get_context_usage(self):
        return {"totalTokens": 1000, "maxTokens": 100000}


class Waits:
    """Remplaçant de `wait_until_reset` : note le délai ; bloque tant que `block` est vrai."""

    def __init__(self, portal):
        self.portal = portal
        self.delays: list[float] = []
        self.block = False
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def __call__(self, seconds):
        self.delays.append(seconds)
        LOG.append(("wait", seconds))
        self.entered.set()
        if self.block:
            await self.gate.wait()

    def wait_entered(self):
        async def _w():
            await asyncio.wait_for(self.entered.wait(), 3)
        self.portal.call(_w)

    def release(self):
        async def _r():
            self.gate.set()
        self.portal.call(_r)


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "claude-config"
    (cfg / "projects").mkdir(parents=True)
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", cfg)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    LOG.clear()
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        c.waits = Waits(portal)
        # raising=False : si la fonction n'existe pas encore, le test échoue sur le comportement
        monkeypatch.setattr(app_mod, "wait_until_reset", c.waits, raising=False)
        yield c
        c.waits.release()  # ne jamais laisser une attente bloquée derrière soi
        c.portal = None


def is_(type_, **fields):
    return lambda e: e.get("type") == type_ and all(e.get(k) == v for k, v in fields.items())


def hire(ws, tmp_path, rejections, resets_at):
    """Recrute un employé dont la session sera rejetée `rejections` fois ; renvoie (aid, tid, session)."""
    PLAN.update(rejections=rejections, resets_at=resets_at)
    cwd = tmp_path / f"proj-{uuid.uuid4().hex[:8]}"
    cwd.mkdir()
    title = f"limite-{uuid.uuid4().hex}"
    ws.send_json({"type": "new_ticket", "title": title, "cwd": str(cwd)})
    aid = recv_until(ws, lambda e: e.get("type") == "agent_hired" and e["agent"]["cwd"] == str(cwd))["agent"]["id"]
    tid = recv_until(ws, lambda e: e.get("type") == "ticket_created" and e["ticket"]["title"] == title)["ticket"]["id"]
    recv_until(ws, is_("ticket_assigned", ticket_id=tid))
    (session,) = [i for i in INSTANCES if i.cwd == str(cwd)]
    return aid, tid, session


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- pause puis reprise


def test_limite_atteinte_met_en_pause_puis_reprend_avec_continue(client, tmp_path):
    resets_at = int(time.time()) + 600
    seen: list[dict] = []
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = hire(ws, tmp_path, 1, resets_at)
        paused = recv_until(ws, is_("paused", agent_id=aid), seen=seen)
        done = recv_until(ws, is_("ticket_done", ticket_id=tid), seen=seen)
        seen += drain(ws)
    assert paused.get("until"), paused
    assert resets_at - 1 <= ts(paused["until"]) <= resets_at + MARGIN, paused
    assert [e for e in seen if is_("ticket_done", ticket_id=tid)(e)] == [done], "un seul ticket_done"
    assert done["ok"] is True, done
    assert session.prompts[1:] == ["continue"], session.prompts
    assert [i for i in INSTANCES if "continue" in i.prompts] == [session], "reprise sur la même session"
    (delay,) = client.waits.delays
    assert 600 - 10 <= delay <= 600 + MARGIN, delay
    assert LOG.index(("wait", delay)) < LOG.index(("query", "continue")), "continue après l'attente"


def test_pendant_la_pause_le_ticket_reste_en_cours_et_le_snapshot_garde_la_pause(client, tmp_path):
    client.waits.block = True
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = hire(ws, tmp_path, 1, int(time.time()) + 600)
        recv_until(ws, is_("paused", agent_id=aid))
        client.waits.wait_entered()
        during = drain(ws)
    assert not [e for e in during if is_("ticket_done", ticket_id=tid)(e)], during
    assert "continue" not in session.prompts
    with connect(client) as ws:
        _, snap = handshake(ws)
    (agent,) = [a for a in snap["agents"] if a["id"] == aid]
    assert agent.get("paused_until"), agent
    assert [t["status"] for t in snap["tickets"] if t["id"] == tid] != ["done"], snap["tickets"]

    with connect(client) as ws:
        handshake(ws)
        client.waits.release()
        done = recv_until(ws, is_("ticket_done", ticket_id=tid))
        drain(ws)
    assert done["ok"] is True, done
    with connect(client) as ws:
        _, snap = handshake(ws)
    (agent,) = [a for a in snap["agents"] if a["id"] == aid]
    assert not agent.get("paused_until"), "la pause disparaît du snapshot après la reprise"


def test_sans_resets_at_reessaie_apres_cinq_minutes(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = hire(ws, tmp_path, 1, None)
        recv_until(ws, is_("paused", agent_id=aid))
        done = recv_until(ws, is_("ticket_done", ticket_id=tid))
    assert done["ok"] is True, done
    (delay,) = client.waits.delays
    assert NO_RESET_DELAY - 10 <= delay <= NO_RESET_DELAY + MARGIN, delay
    assert session.prompts[1:] == ["continue"], session.prompts


def test_deux_rejets_consecutifs_deux_pauses_un_seul_succes(client, tmp_path):
    seen: list[dict] = []
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = hire(ws, tmp_path, 2, int(time.time()) + 600)
        recv_until(ws, is_("ticket_done", ticket_id=tid), seen=seen)
        seen += drain(ws)
    pauses = [e for e in seen if is_("paused", agent_id=aid)(e)]
    dones = [e for e in seen if is_("ticket_done", ticket_id=tid)(e)]
    assert len(pauses) == 2, seen
    assert len(dones) == 1 and dones[0]["ok"] is True, dones
    assert session.prompts[1:] == ["continue", "continue"], session.prompts
    assert len(client.waits.delays) == 2, client.waits.delays


# ---------------------------------------------------------------- file d'attente


def test_ticket_en_attente_non_envoye_pendant_la_pause(client, tmp_path):
    client.waits.block = True
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = hire(ws, tmp_path, 1, int(time.time()) + 600)
        recv_until(ws, is_("paused", agent_id=aid))
        client.waits.wait_entered()
        title2 = f"attente-{uuid.uuid4().hex}"
        ws.send_json({"type": "new_ticket", "title": title2, "agent_id": aid})
        tid2 = recv_until(ws, lambda e: e.get("type") == "ticket_created"
                          and e["ticket"]["title"] == title2)["ticket"]["id"]
        drain(ws)
        assert not any(title2 in p for p in session.prompts), "ticket envoyé pendant la pause"
        client.waits.release()
        done1 = recv_until(ws, is_("ticket_done", ticket_id=tid))
        done2 = recv_until(ws, is_("ticket_done", ticket_id=tid2))
    assert done1["ok"] is True and done2["ok"] is True, (done1, done2)
    i_continue = session.prompts.index("continue")
    i_title2 = next(i for i, p in enumerate(session.prompts) if title2 in p)
    assert i_continue < i_title2, session.prompts


# ---------------------------------------------------------------- interruption / congédiement


def test_interrompre_pendant_la_pause(client, tmp_path):
    client.waits.block = True
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = hire(ws, tmp_path, 1, int(time.time()) + 600)
        recv_until(ws, is_("paused", agent_id=aid))
        client.waits.wait_entered()
        ws.send_json({"type": "interrupt", "agent_id": aid})
        done = recv_until(ws, is_("ticket_done", ticket_id=tid))
        client.waits.release()  # même si l'attente n'a pas été annulée, rien ne doit repartir
        drain(ws)
    assert done["ok"] is False, done
    assert "interrompu" in json.dumps(done, ensure_ascii=False).lower(), done
    assert "continue" not in session.prompts, session.prompts


def test_congedier_pendant_la_pause(client, tmp_path):
    client.waits.block = True
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = hire(ws, tmp_path, 1, int(time.time()) + 600)
        recv_until(ws, is_("paused", agent_id=aid))
        client.waits.wait_entered()
        ws.send_json({"type": "dismiss", "agent_id": aid})
        recv_until(ws, is_("agent_left", agent_id=aid))
        client.waits.release()
        drain(ws)
    assert "continue" not in session.prompts, session.prompts
