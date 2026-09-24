import json

import backend.app as app_mod
from backend.app import Employee


def test_session_retenue_depuis_init_puis_result(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", tmp_path)
    e = Employee(0, "Léa")
    assert e.follow() is None and e.chat_updates() == []
    e.set_session("../x")  # sert dans un glob : refusé
    assert e.session_id is None
    e.set_session("abc-123")
    f = tmp_path / "projects" / "p" / "abc-123.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "avant"}}) + "\n")
    assert e.chat_updates() == []  # suivi depuis la fin : l'historique passe par chat_history
    with f.open("a") as w:
        w.write(json.dumps({"type": "user", "message": {"role": "user", "content": "après"}}) + "\n")
    (ev,) = e.chat_updates()
    assert ev["agent_id"] == "e0" and ev["entry"]["text"] == "après"
    e.set_session("def-456")  # nouvelle session (/clear) : nouveau suivi
    assert e.tail is None
