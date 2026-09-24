"""Tests d'intention pour l'issue #40 : voir ce que font les stagiaires (sous-agents).

Écrits en boîte noire depuis l'issue et son commentaire, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; le panneau en lecture seule (front) est vérifié à la main.

Contrat supposé :
- Disposition des fichiers (vérifiée sur Claude Code 2.1.281, cf. commentaire de l'issue) :
  `$CLAUDE_CONFIG_DIR/projects/<dir>/<session_id>/subagents/agent-<agentId>.jsonl` + `agent-<agentId>.meta.json`
  contenant `{"toolUseId": <id du tool_use Agent dans le transcript parent>, "description"}`. Les
  enregistrements du sous-agent ont `"isSidechain": true` et la même forme user/assistant que le transcript
  principal. Le stagiaire est relié à son fichier par `toolUseId`.
- Stagiaire = id `agent.id` de l'événement `subagent_spawned` (#30 pour le terminal, #2 pour le piloté).
- `open_chat` sur l'id d'un stagiaire → `chat_history` (au seul demandeur) avec les entrées de son fichier
  (format `chat_entries` ; les lignes sidechain y sont montrées) : sa consigne, ses appels d'outil, sorties.
- Nouvelle ligne du fichier du sous-agent → `chat_entry` (agent_id = stagiaire) aux seuls onglets abonnés.
- Session terminal : tool_use dans le fichier du sous-agent → événement `tool_use` avec `agent_id` = stagiaire.
- Stagiaire parti (`subagent_done`) → `open_chat` sur son id : `chat_history` avec `entries == []` et `error`.

Câblage de test (comme #35) : la boucle `backend.app.observe_terminal(observer, 0.05)` tourne dans le portail
avec un `Observer` affecté à `backend.app.observer` (le lifespan ne tourne pas). Pour le piloté, le faux SDK
est un `AssistantMessage` portant un `ToolUseBlock` Agent passé à `await Employee.translate(msg)`, avec
`Employee.session_id` fixé à la main. Attentes bornées à 3 s.
"""
import json
import uuid

import pytest
from claude_agent_sdk import AssistantMessage, ToolUseBlock

import backend.app as app_mod
from tests.spec.test_issue25_chat import (  # noqa: F401
    PID, Alive, append, assistant, cfg, client, connect, drain, handshake, no_subprocess, of, open_chat, prompt,
    recv_until, text_block, tool_result, tool_use_block, transcript_path, write_session,
)
from tests.spec.test_issue35_piloted_chat import loop, piloted  # noqa: F401

AGENT_INPUT = {"description": "explore the repo", "prompt": "TACHE-STAGIAIRE", "subagent_type": "Explore"}


def subagent_files(main_transcript, sid, tool_use_id, *records, agent_id=None):
    """Crée meta + transcript d'un sous-agent de la session `sid` ; renvoie le chemin du .jsonl."""
    agent_id = agent_id or uuid.uuid4().hex[:16]
    d = main_transcript.parent / sid / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"toolUseId": tool_use_id, "description": AGENT_INPUT["description"]}))
    path = d / f"agent-{agent_id}.jsonl"
    path.touch()
    append(path, *records)
    return path


def sub_history(tag=""):
    """Consigne, appel d'outil et sortie d'un sous-agent (lignes sidechain)."""
    return [
        prompt(f"TACHE-STAGIAIRE{tag}", sidechain=True),
        assistant([text_block(f"JE-CHERCHE{tag}"), tool_use_block("toolu_s1", "Grep", {"pattern": "observer"})],
                  sidechain=True),
        tool_result(f"SORTIE-GREP{tag}", tid="toolu_s1", sidechain=True),
    ]


class Terminal:
    """Session terminal observée par la boucle de l'observateur."""

    def __init__(self, ws, cfg, proj):
        self.ws = ws
        self.sid = str(uuid.uuid4())
        self.id = "o-" + self.sid[:8]
        self.path = transcript_path(cfg, self.sid, proj)
        append(self.path, prompt("lance un stagiaire"))
        write_session(cfg, PID, self.sid, proj)
        recv_until(ws, lambda e: e.get("type") == "observed_joined" and e["agent"]["id"] == self.id)

    def spawn(self, tool_use_id="toolu_a1"):
        append(self.path, assistant([tool_use_block(tool_use_id, "Agent", AGENT_INPUT)]))
        ev = recv_until(self.ws, lambda e: e.get("type") == "subagent_spawned" and e.get("parent_id") == self.id)
        return ev["agent"]["id"]


@pytest.fixture
def alive_loop(client, cfg, monkeypatch):
    """Boucle de l'observateur avec le pid fictif vivant (le `loop` de #35 n'en a aucun)."""
    import asyncio

    from backend.observer import Observer

    obs = Observer(cfg, app_mod.hub.emit, pid_alive=Alive(PID))
    monkeypatch.setattr(app_mod, "observer", obs, raising=False)

    async def _start():
        return asyncio.create_task(app_mod.observe_terminal(obs, 0.05))

    task = client.portal.call(_start)
    yield
    client.portal.call(lambda: task.cancel())


@pytest.fixture
def term(client, cfg, tmp_path, alive_loop):
    proj = tmp_path / "eter"
    proj.mkdir(exist_ok=True)
    return lambda ws: Terminal(ws, cfg, proj)


def entry_texts(entries):
    return json.dumps(entries, ensure_ascii=False)


def assert_sub_entries(entries, tag=""):
    kinds = [(e.get("role"), e.get("kind")) for e in entries]
    assert ("user", "text") in kinds and ("assistant", "tool_use") in kinds and ("user", "tool_result") in kinds, entries
    dump = entry_texts(entries)
    assert f"TACHE-STAGIAIRE{tag}" in dump
    assert f"SORTIE-GREP{tag}" in dump
    assert any(e.get("kind") == "tool_use" and e.get("tool") == "Grep" for e in entries), entries


# ---------------------------------------------------------------- stagiaire d'un employé terminal


def test_stagiaire_terminal_open_chat_renvoie_ses_entrees(client, term):
    with connect(client) as ws:
        handshake(ws)
        emp = term(ws)
        subagent_files(emp.path, emp.sid, "toolu_a1", *sub_history())
        subagent_files(emp.path, emp.sid, "toolu_autre", prompt("LEURRE-AUTRE-STAGIAIRE", sidechain=True))
        intern = emp.spawn("toolu_a1")
        hist = open_chat(ws, intern)
    assert not hist.get("error"), hist
    assert_sub_entries(hist["entries"])
    dump = entry_texts(hist["entries"])
    assert "LEURRE-AUTRE-STAGIAIRE" not in dump, "le fichier est choisi par toolUseId"
    assert "lance un stagiaire" not in dump, "pas le transcript du parent"


# ---------------------------------------------------------------- stagiaire d'un employé piloté


def test_stagiaire_pilote_open_chat_renvoie_ses_entrees(client, cfg, piloted):
    emp = piloted([prompt("PROMPT-PILOTE")])
    subagent_files(emp.test_transcript, emp.session_id, "toolu_p1", *sub_history("-P"))
    msg = AssistantMessage(content=[ToolUseBlock(id="toolu_p1", name="Agent", input=AGENT_INPUT)],
                           model="claude-opus-5-5", parent_tool_use_id=None)
    with connect(client) as ws:
        handshake(ws)
        client.portal.call(emp.translate, msg)
        ev = recv_until(ws, lambda e: e.get("type") == "subagent_spawned" and e.get("parent_id") == emp.id)
        hist = open_chat(ws, ev["agent"]["id"])
    assert not hist.get("error"), hist
    assert_sub_entries(hist["entries"], "-P")
    assert "PROMPT-PILOTE" not in entry_texts(hist["entries"])


# ---------------------------------------------------------------- en direct


def test_nouvelle_ligne_du_sous_agent_envoyee_au_seul_onglet_abonne(client, term):
    other_seen = []
    with connect(client) as other, connect(client) as ws:
        handshake(other)
        handshake(ws)
        emp = term(ws)
        sub = subagent_files(emp.path, emp.sid, "toolu_a1", prompt("TACHE-STAGIAIRE", sidechain=True))
        intern = emp.spawn("toolu_a1")
        open_chat(ws, intern)
        append(sub, assistant([text_block("RAPPORT-FINAL-LIVE")], sidechain=True, stop="end_turn"))
        live = recv_until(ws, lambda e: e.get("type") == "chat_entry" and e.get("agent_id") == intern)
        drain(other, other_seen)
    assert live["entry"]["role"] == "assistant" and live["entry"]["kind"] == "text"
    assert "RAPPORT-FINAL-LIVE" in live["entry"]["text"]
    assert of(other_seen, "chat_entry") == []
    assert "RAPPORT-FINAL-LIVE" not in json.dumps(other_seen, ensure_ascii=False)


def test_outil_du_sous_agent_terminal_anime_le_stagiaire(client, term):
    with connect(client) as ws:
        handshake(ws)
        emp = term(ws)
        sub = subagent_files(emp.path, emp.sid, "toolu_a1", prompt("TACHE-STAGIAIRE", sidechain=True))
        intern = emp.spawn("toolu_a1")
        append(sub, assistant([tool_use_block("toolu_s2", "Read", {"file_path": "/tmp/x.py"})], sidechain=True))
        ev = recv_until(ws, lambda e: e.get("type") == "tool_use" and e.get("agent_id") == intern)
    assert ev.get("tool") == "Read"


# ---------------------------------------------------------------- stagiaire parti


def test_stagiaire_parti_open_chat_refuse_avec_motif(client, term):
    with connect(client) as ws:
        handshake(ws)
        emp = term(ws)
        subagent_files(emp.path, emp.sid, "toolu_a1", *sub_history())
        intern = emp.spawn("toolu_a1")
        append(emp.path, tool_result("rapport", tid="toolu_a1"))
        recv_until(ws, lambda e: e.get("type") == "subagent_done" and e.get("agent_id") == intern)
        hist = open_chat(ws, intern)
    assert hist["entries"] == []
    assert hist.get("error")
