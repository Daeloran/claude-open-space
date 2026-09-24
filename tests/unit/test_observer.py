"""Tests de logique de l'observation des sessions terminal (issue #13)."""
import asyncio
import json
import os

import backend.app as app_mod
from backend.observer import Observer, TranscriptTail, pid_alive


def test_pid_alive():
    assert pid_alive(os.getpid())
    assert not pid_alive(0) and not pid_alive(-1)  # os.kill(0, …) viserait le groupe de processus
    assert not pid_alive(2**22 + 12345)  # au-delà de pid_max Linux : inexistant


def test_pid_alive_process_d_un_autre_utilisateur(monkeypatch):
    def deny(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", deny)
    assert pid_alive(1)


def test_ratio_heuristique_1m():
    o = Observer("/nope", None, context_window=200000)
    u = lambda n: {"model": "claude-opus-5", "usage": {"input_tokens": n}}  # noqa: E731
    assert o._ratio(u(100000)) == 0.5
    assert o._ratio(u(200000)) == 1.0          # pile la fenêtre : pas de bascule
    assert o._ratio(u(200001)) == 200001 / 1_000_000
    assert o._ratio({"model": "x[1m]", "usage": {"input_tokens": 100000}}) == 0.1
    assert o._ratio({"model": "<synthetic>", "usage": {"input_tokens": 0}}) is None  # pas une mesure
    assert o._ratio({"usage": {"input_tokens": "abc"}}) is None


def test_transcript_cherche_jusqu_a_trouver_puis_garde(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    (cfg / "sessions").mkdir(parents=True)
    (cfg / "sessions" / "101.json").write_text(json.dumps(
        {"pid": 101, "cwd": str(tmp_path), "entrypoint": "cli", "sessionId": "abcdef12-x", "name": "n", "status": "idle"}))
    o = Observer(cfg, None, pid_alive=lambda pid: True)
    calls = []
    real = Observer._transcript
    monkeypatch.setattr(Observer, "_transcript", lambda self, sid: calls.append(sid) or real(self, sid))
    o._scan()
    o._scan()
    assert len(calls) == 2  # pas encore de transcript : cherché à chaque passe
    (cfg / "projects" / "p").mkdir(parents=True)
    (cfg / "projects" / "p" / "abcdef12-x.jsonl").write_text("")
    o._scan()
    o._scan()
    o._scan()
    assert len(calls) == 3  # trouvé une fois, puis gardé


def test_tail_fichier_tronque_repart_du_debut(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"n": 1}\n{"n": 2}\n')
    tail = TranscriptTail(p)
    assert len(tail.read_new()) == 2
    p.write_text('{"n": 3}\n')
    assert tail.read_new() == [{"n": 3}]


def test_boucle_d_observation_resiste_aux_erreurs():
    class Flaky:
        n = 0

        async def poll(self):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("boum")

    async def run():
        obs = Flaky()
        task = asyncio.create_task(app_mod.observe_terminal(obs, interval=0))
        while obs.n < 3:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return obs.n, task.cancelled()

    n, cancelled = asyncio.run(run())
    assert n >= 3 and cancelled


def test_hub_suit_les_employes_observes():
    h = app_mod.Hub()

    async def run():
        await h.emit({"type": "observed_joined", "agent": {"id": "o-1", "name": "n", "status": "idle"}})
        await h.emit({"type": "observed_status", "agent_id": "o-1", "status": "busy"})
        await h.emit({"type": "context", "agent_id": "o-1", "ratio": 0.3})

    asyncio.run(run())
    snap = h.snapshot()
    assert snap["agents"] == [{"id": "o-1", "name": "n", "status": "busy", "observed": True}]
    asyncio.run(h.emit({"type": "observed_left", "agent_id": "o-1"}))
    assert h.snapshot()["agents"] == [] and h.snapshot()["context"] == {}
