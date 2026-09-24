"""Tests d'intention pour l'issue #33 : répondre aux AskUserQuestion des employés pilotés au bureau du manager.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; la carte (front) et le mode démo sont vérifiés à la main.

Contrat public supposé :
- `Employee(idx, name).can_use_tool(tool, input, context)` émet un `permission_request` (via `hub`)
  puis attend la décision envoyée sur /ws : `{"type": "permission_decision", "request_id", "allow", "answers"}`.
- Pour AskUserQuestion, l'événement porte
  `questions: [{"question", "header", "multi", "options": [{"label", "description"}]}]`.
- `allow: true` → `PermissionResultAllow` dont `updated_input` = input d'origine + `answers` validées
  (clés inconnues retirées, valeurs non-chaînes retirées, longueur bornée).
- `allow: false` → `PermissionResultDeny` dont le message dit que le manager n'a pas répondu.
- Autres outils : comportement inchangé (message de refus actuel observé avant #33).

Même harnais que test_issue4_replay : TestClient sans lifespan, portail partagé (une seule boucle).
"""
import asyncio
import copy

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from backend.app import Employee
from tests.spec.test_issue4_replay import TIMEOUT, client, connect, handshake, recv_until  # noqa: F401

Q1, Q2 = "Quelle base ?", "Quels tests ?"
ASK = {"questions": [
    {"question": Q1, "header": "DB", "multiSelect": False,
     "options": [{"label": "Postgres", "description": "relationnel"},
                 {"label": "SQLite", "description": "fichier"}]},
    {"question": Q2, "header": "Tests", "multiSelect": True,
     "options": [{"label": "unit", "description": "u"}, {"label": "e2e", "description": "e"}]},
]}
BASH_DENY = "Refusé par le manager. Propose une autre approche."


def start(client, tool, inp):
    """Lance can_use_tool dans la boucle du portail ; renvoie la tâche."""
    emp = Employee(0, "Léa")

    async def _start():
        return asyncio.create_task(emp.can_use_tool(tool, copy.deepcopy(inp), None))

    return client.portal.call(_start)


def result(client, task):
    async def _get():
        return await asyncio.wait_for(task, TIMEOUT)

    return client.portal.call(_get)


def ask(client, tool, inp, decision):
    """Demande → permission_request reçu sur /ws → décision envoyée sur /ws → (événement, résultat SDK)."""
    with connect(client) as ws:
        handshake(ws)
        task = start(client, tool, inp)
        ev = recv_until(ws, "permission_request")
        assert ev["tool"] == tool
        ws.send_json({"type": "permission_decision", "request_id": ev["request_id"], **decision})
        recv_until(ws, "permission_resolved")
    return ev, result(client, task)


# ---------------------------------------------------------------- permission_request


def test_permission_request_expose_les_questions(client):
    ev, _ = ask(client, "AskUserQuestion", ASK, {"allow": False})
    assert ev["questions"] == [
        {"question": Q1, "header": "DB", "multi": False,
         "options": [{"label": "Postgres", "description": "relationnel"},
                     {"label": "SQLite", "description": "fichier"}]},
        {"question": Q2, "header": "Tests", "multi": True,
         "options": [{"label": "unit", "description": "u"}, {"label": "e2e", "description": "e"}]},
    ]


def test_snapshot_rejoue_la_question_en_attente(client):
    with connect(client) as ws:
        handshake(ws)
        task = start(client, "AskUserQuestion", ASK)
        ev = recv_until(ws, "permission_request")
    with connect(client) as ws:
        _, snap = handshake(ws)
        (pending,) = [p for p in snap["pending_permissions"] if p["request_id"] == ev["request_id"]]
        assert pending["questions"] == ev["questions"]
        ws.send_json({"type": "permission_decision", "request_id": ev["request_id"], "allow": False})
        recv_until(ws, "permission_resolved")
    result(client, task)


# ---------------------------------------------------------------- réponses


def test_reponses_transmises_via_websocket(client):
    answers = {Q1: "Postgres", Q2: "unit, e2e"}
    _, res = ask(client, "AskUserQuestion", ASK, {"allow": True, "answers": answers})
    assert isinstance(res, PermissionResultAllow), res
    assert res.updated_input["answers"] == answers
    assert res.updated_input["questions"] == ASK["questions"]


def test_question_inconnue_ignoree(client):
    _, res = ask(client, "AskUserQuestion", ASK,
                 {"allow": True, "answers": {Q1: "SQLite", "Question inventée ?": "x"}})
    assert isinstance(res, PermissionResultAllow), res
    assert res.updated_input["answers"] == {Q1: "SQLite"}


def test_reponse_non_chaine_rejetee(client):
    _, res = ask(client, "AskUserQuestion", ASK,
                 {"allow": True, "answers": {Q1: "Postgres", Q2: ["unit", "e2e"]}})
    assert isinstance(res, PermissionResultAllow), res
    assert res.updated_input["answers"].get(Q1) == "Postgres"
    assert Q2 not in res.updated_input["answers"]


def test_reponse_trop_longue_tronquee(client):
    long = "x" * 100_000
    _, res = ask(client, "AskUserQuestion", ASK, {"allow": True, "answers": {Q1: long, Q2: "unit"}})
    assert isinstance(res, PermissionResultAllow), res
    stored = res.updated_input["answers"][Q1]
    assert isinstance(stored, str) and 0 < len(stored) < len(long)


def test_ignorer_refuse_en_disant_que_le_manager_n_a_pas_repondu(client):
    _, res = ask(client, "AskUserQuestion", ASK, {"allow": False})
    assert isinstance(res, PermissionResultDeny), res
    assert "manager" in res.message.lower()
    assert res.message != BASH_DENY


# ---------------------------------------------------------------- non-régression (autres outils)


def test_bash_autorise_input_inchange(client):
    inp = {"command": "ls -la"}
    ev, res = ask(client, "Bash", inp, {"allow": True})
    assert "questions" not in ev
    assert isinstance(res, PermissionResultAllow), res
    assert res.updated_input == inp


def test_bash_autorise_ignore_des_answers_parasites(client):
    inp = {"command": "ls -la"}
    _, res = ask(client, "Bash", inp, {"allow": True, "answers": {"x": "y"}})
    assert isinstance(res, PermissionResultAllow), res
    assert res.updated_input == inp


def test_bash_refuse_message_inchange(client):
    _, res = ask(client, "Bash", {"command": "rm -rf /tmp/x"}, {"allow": False})
    assert isinstance(res, PermissionResultDeny), res
    assert res.message == BASH_DENY
