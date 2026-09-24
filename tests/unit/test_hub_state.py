"""Tests de logique de l'état du Hub (issue #4)."""
import asyncio

from backend.app import DONE_KEPT, Employee, Hub, hub


def test_tickets_termines_bornes_les_actifs_gardes():
    h = Hub()

    async def run():
        await h.emit({"type": "ticket_created", "ticket": {"id": "q", "title": "q"}})
        for i in range(DONE_KEPT + 5):
            await h.emit({"type": "ticket_created", "ticket": {"id": f"t{i}", "title": "x"}})
            await h.emit({"type": "ticket_done", "ticket_id": f"t{i}", "ok": True})

    asyncio.run(run())
    ids = [t["id"] for t in h.snapshot()["tickets"]]
    assert "q" in ids  # un ticket en attente n'est jamais oublié
    assert len(ids) == DONE_KEPT + 1
    assert ids[1] == "t5"  # les plus anciens terminés sont partis


def test_evenements_inconnus_ou_orphelins_ignores():
    h = Hub()
    asyncio.run(h.emit({"type": "ticket_done", "ticket_id": "absent"}))
    asyncio.run(h.emit({"type": "message", "agent_id": "e0", "text": "hi"}))
    assert h.snapshot()["tickets"] == []


def test_demande_annulee_sort_de_l_etat():
    async def run():
        task = asyncio.create_task(Employee(0, "Léa").can_use_tool("Bash", {"command": "ls"}, None))
        while not hub.requests:
            await asyncio.sleep(0)
        rid = next(iter(hub.requests))
        assert rid in hub.pending
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rid

    rid = asyncio.run(run())
    assert rid not in hub.requests and rid not in hub.pending


def test_ticket_terminal_echoue_si_la_session_part():
    h = Hub()
    h.terminal["o-1"] = {"id": "t1", "busy": True}

    async def run():
        await h.emit({"type": "ticket_created", "ticket": {"id": "t1", "title": "x"}})
        await h.emit({"type": "observed_left", "agent_id": "o-1"})

    asyncio.run(run())
    assert h.snapshot()["tickets"][0]["status"] == "done" and h.snapshot()["tickets"][0]["ok"] is False
    assert h.terminal == {}


def test_ticket_terminal_pas_encore_envoye_ignore():
    h = Hub()
    h.terminal["o-1"] = {"id": None, "busy": False}  # envoi en cours
    asyncio.run(h.emit({"type": "observed_status", "agent_id": "o-1", "status": "busy"}))
    assert h.terminal["o-1"] == {"id": None, "busy": False}


def test_reponse_rapide_sans_busy_termine_le_ticket_terminal(monkeypatch):
    import backend.app as app_mod
    h = Hub()
    monkeypatch.setattr(app_mod, "hub", h)
    sent = []

    async def fake_send(pid, text):
        sent.append(text)

    monkeypatch.setattr(app_mod.konsole, "send_prompt", fake_send)
    agent = {"id": "o-1", "pid": 7, "observed": True}
    done = []

    async def run():
        assert await app_mod.type_in_terminal(agent, "a") is None
        h.terminal["o-1"]["id"] = "t1"
        await h.emit({"type": "ticket_created", "ticket": {"id": "t1", "title": "a"}})
        # fin de réponse d'avant l'envoi : ignorée
        await h.emit({"type": "observed_turn_end", "agent_id": "o-1", "at": "2020-01-01T00:00:00Z"})
        assert h.board["t1"]["status"] == "queued"
        # réponse rapide : jamais vue busy, mais sa fin est dans le transcript
        await h.emit({"type": "observed_turn_end", "agent_id": "o-1", "at": "2999-01-01T00:00:00Z"})
        done.append(h.board["t1"]["status"])
        await h.emit({"type": "observed_status", "agent_id": "o-1", "status": "idle"})  # pas de second ticket_done
        assert await app_mod.type_in_terminal(agent, "b") is None  # session de nouveau disponible

    asyncio.run(run())
    assert done == ["done"] and h.board["t1"]["ok"] is True
    assert sent == ["a", "b"]


def test_attente_ne_clot_pas_le_ticket_terminal():
    h = Hub()
    h.terminal["o-1"] = {"id": "t1", "busy": False}

    async def run():
        await h.emit({"type": "ticket_created", "ticket": {"id": "t1", "title": "a"}})
        await h.emit({"type": "observed_status", "agent_id": "o-1", "status": "busy"})
        await h.emit({"type": "observed_status", "agent_id": "o-1", "status": "waiting", "waiting_for": "input needed"})
        assert h.board["t1"]["status"] == "queued"  # il attend ta réponse, pas fini
        await h.emit({"type": "observed_status", "agent_id": "o-1", "status": "idle"})

    asyncio.run(run())
    assert h.board["t1"]["status"] == "done"


def test_snapshot_garde_waiting_for_seulement_en_attente():
    h = Hub()

    async def run():
        await h.emit({"type": "observed_joined", "agent": {"id": "o-1", "name": "x", "status": "idle"}})
        await h.emit({"type": "observed_status", "agent_id": "o-1", "status": "waiting", "waiting_for": "dialog open"})
        assert h.agents["o-1"]["waiting_for"] == "dialog open"
        await h.emit({"type": "observed_status", "agent_id": "o-1", "status": "busy"})

    asyncio.run(run())
    assert "waiting_for" not in h.agents["o-1"]


def test_rien_n_est_tape_pendant_une_attente(monkeypatch):
    import backend.app as app_mod
    h = Hub()
    monkeypatch.setattr(app_mod, "hub", h)
    sent = []

    async def fake_send(pid, text):
        sent.append(text)

    monkeypatch.setattr(app_mod.konsole, "send_prompt", fake_send)

    async def run():
        await h.emit({"type": "observed_joined", "agent": {"id": "o-1", "name": "x", "status": "waiting",
                                                          "waiting_for": "input needed"}})
        return await app_mod.type_in_terminal({"id": "o-1", "pid": 7, "observed": True}, "1")

    assert "attend ta réponse" in asyncio.run(run())
    assert sent == [] and "o-1" not in h.terminal
