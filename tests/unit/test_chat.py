"""Tests de logique du panneau de discussion (issue #25). Transcripts fictifs uniquement."""
import json

from fastapi.testclient import TestClient

import backend.app as app_mod
from backend.observer import chat_history


def prompt(i):
    return {"type": "user", "timestamp": "t", "message": {"role": "user", "content": f"msg-{i:04d}"}}


def write(path, records, tail=""):
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail)


def test_lecture_depuis_la_fin_par_fenetres(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    write(p, [prompt(i) for i in range(3000)], tail='{"type": "user", "mess')  # dernière ligne en cours d'écriture
    reads = []
    real_open = open

    def spy(*a, **k):
        f = real_open(*a, **k)
        r = f.read
        f.read = lambda n=-1: reads.append(n) or r(n)
        return f

    monkeypatch.setattr("builtins.open", spy)
    entries = chat_history(p, limit=5, chunk=256)
    assert [e["text"] for e in entries] == [f"msg-{i:04d}" for i in range(2995, 3000)]
    assert max(reads) < p.stat().st_size // 10, "pas de lecture intégrale"


def test_plafond_de_lecture(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, [prompt(i) for i in range(1000)])
    entries = chat_history(p, limit=200, max_bytes=2048, chunk=512)
    assert 0 < len(entries) < 200 and entries[-1]["text"] == "msg-0999"


def test_petit_fichier_et_fichier_vide(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, [prompt(1), prompt(2)])
    assert [e["text"] for e in chat_history(p)] == ["msg-0001", "msg-0002"]
    p.write_text("")
    assert chat_history(p) == []


def test_abonnements_nettoyes_a_la_deconnexion(tmp_path, monkeypatch):
    class W:
        watched = {"o-1": {"tail": type("T", (), {"path": tmp_path / "t.jsonl"})()}}

    write(tmp_path / "t.jsonl", [prompt(1)])
    monkeypatch.setattr(app_mod, "observer", W())
    with TestClient(app_mod.app).websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        for _ in range(3):
            ws.receive_json()  # hello, snapshot, plan_usage
        ws.send_json({"type": "open_chat", "agent_id": "o-1"})
        assert ws.receive_json()["entries"][0]["text"] == "msg-0001"
        assert len(app_mod.hub.chats["o-1"]) == 1
    assert not app_mod.hub.chats.get("o-1")
