"""Tests de logique du recrutement : réserve de prénoms, arrêt propre des tâches."""
import asyncio

import backend.app as app_mod


def test_reserve_de_prenoms_cyclique_suffixee(monkeypatch):
    monkeypatch.setattr(app_mod, "TEAM", ["Léa", "Hugo"])
    assert [app_mod.recruit_name(n) for n in range(5)] == ["Léa", "Hugo", "Léa 2", "Hugo 2", "Léa 3"]


class BlockingClient:
    def __init__(self, options):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_arret_du_lifespan_annule_les_employes(tmp_path, monkeypatch):
    async def no_plan():
        await asyncio.Event().wait()

    monkeypatch.setattr(app_mod, "ClaudeSDKClient", BlockingClient)
    monkeypatch.setattr(app_mod, "refresh_plan_usage", no_plan)
    async with app_mod.lifespan(app_mod.app):
        e = await app_mod.hire(str(tmp_path))
        assert e.cwd == str(tmp_path) and e.options.cwd == str(tmp_path)
        task = next(t for t in app_mod.workers if t.get_name() == f"employee-{e.id}")
        await asyncio.sleep(0)
    assert task.cancelled()
    assert task not in app_mod.workers
