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
