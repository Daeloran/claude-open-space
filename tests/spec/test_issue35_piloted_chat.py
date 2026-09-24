"""Tests d'intention pour l'issue #35 : panneau de discussion pour les employés pilotés (SDK).

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; le front et la démo sont vérifiés à la main.

Contrat public supposé :
- Un employé piloté `backend.app.Employee(idx, name)` (id `"e<idx>"`) expose l'id de sa session SDK
  dans l'attribut `session_id` (None avant son premier ticket). Les tests le fixent à la main.
- Son transcript est `$CLAUDE_CONFIG_DIR/projects/<un-dossier>/<session_id>.jsonl`, même format que la CLI.
- L'employé est trouvé via le registre `backend.app.employees` (dict id → Employee).
- WebSocket (`/ws`, origin `http://testserver`), mêmes règles que #25 :
  - `open_chat` → `chat_history` au seul demandeur, entrées au format de `chat_entries` (role/kind/text…) ;
  - nouvelle ligne du transcript → `chat_entry` aux seuls onglets abonnés ; `close_chat` arrête l'envoi ;
  - piloté sans session → `chat_history` avec `entries == []` (motif éventuel), sans crash ni déconnexion.

Hypothèse de câblage : les entrées en direct sont lues par la boucle de l'observateur
(`backend.app.observe_terminal(observer, interval)`), démarrée par les tests dans le portail avec un
intervalle court et un `Observer` vide affecté à `backend.app.observer` (le lifespan ne tourne pas).
Attente bornée à 3 s.
"""
import asyncio
import json
import uuid

import pytest

import backend.app as app_mod
from tests.spec.test_issue25_chat import (  # noqa: F401
    Alive, append, assistant, cfg, client, connect, drain, handshake, no_subprocess, of, open_chat, prompt,
    recv_until, text_block, tool_result, tool_use_block, transcript_path,
)


@pytest.fixture
def loop(client, cfg, monkeypatch):
    """Boucle de l'observateur (comme en prod), sur un registre de sessions terminal vide."""
    from backend.observer import Observer

    obs = Observer(cfg, app_mod.hub.emit, pid_alive=Alive())
    monkeypatch.setattr(app_mod, "observer", obs, raising=False)

    async def _start():
        return asyncio.create_task(app_mod.observe_terminal(obs, 0.05))

    task = client.portal.call(_start)
    yield
    client.portal.call(lambda: task.cancel())


@pytest.fixture
def piloted(client, cfg, tmp_path, monkeypatch, loop):
    def make(records=None):
        idx = 900 + len(app_mod.employees)
        emp = app_mod.Employee(idx, "Léa")
        emp.session_id = None
        if records is not None:
            emp.session_id = str(uuid.uuid4())
            emp.test_transcript = transcript_path(cfg, emp.session_id, tmp_path / "projet")
            emp.test_transcript.touch()
            append(emp.test_transcript, *records)
        monkeypatch.setitem(app_mod.employees, emp.id, emp)
        return emp

    return make


def test_open_chat_renvoie_l_historique_de_la_session(client, piloted):
    emp = piloted([prompt("PROMPT-PILOTE"), assistant([text_block("REPONSE **md**"), tool_use_block()]),
                   tool_result("SORTIE-PILOTE")])
    with connect(client) as ws:
        handshake(ws)
        hist = open_chat(ws, emp.id)
    assert not hist.get("error"), hist
    entries = hist["entries"]
    assert [(e["role"], e["kind"]) for e in entries] == [
        ("user", "text"), ("assistant", "text"), ("assistant", "tool_use"), ("user", "tool_result")], entries
    assert "PROMPT-PILOTE" in entries[0]["text"]
    assert "REPONSE **md**" in entries[1]["text"]
    assert entries[2]["tool"] == "Bash"
    assert "SORTIE-PILOTE" in entries[3]["text"]


def test_nouvelle_ligne_envoyee_au_seul_onglet_abonne(client, piloted):
    emp = piloted([prompt("historique")])
    other_seen = []
    with connect(client) as other, connect(client) as ws:
        handshake(other)
        handshake(ws)
        open_chat(ws, emp.id)
        append(emp.test_transcript, assistant([text_block("REPONSE-LIVE")], stop="end_turn"))
        live = recv_until(ws, lambda e: e.get("type") == "chat_entry")
        drain(other, other_seen)
    assert live["agent_id"] == emp.id
    assert live["entry"]["role"] == "assistant" and live["entry"]["kind"] == "text"
    assert "REPONSE-LIVE" in live["entry"]["text"]
    assert of(other_seen, "chat_entry") == []
    assert "REPONSE-LIVE" not in json.dumps(other_seen, ensure_ascii=False)


def test_pilote_sans_session_historique_vide_sans_crash(client, piloted):
    emp = piloted()
    with connect(client) as ws:
        handshake(ws)
        hist = open_chat(ws, emp.id)
        assert hist["entries"] == []
        # la connexion reste utilisable
        again = open_chat(ws, emp.id)
        assert again["entries"] == []


def test_close_chat_arrete_les_entrees_en_direct(client, piloted):
    emp = piloted([prompt("historique")])
    seen = []
    with connect(client) as ws:
        handshake(ws)
        open_chat(ws, emp.id)
        append(emp.test_transcript, prompt("AVANT-FERMETURE"))
        recv_until(ws, lambda e: e.get("type") == "chat_entry")
        ws.send_json({"type": "close_chat", "agent_id": emp.id})
        open_chat(ws, "o-" + uuid.uuid4().hex[:8])  # synchronisation : close_chat traité
        append(emp.test_transcript, prompt("APRES-FERMETURE"))
        drain(ws, seen, timeout=0.5)
    assert of(seen, "chat_entry") == []
    assert "APRES-FERMETURE" not in json.dumps(seen, ensure_ascii=False)
