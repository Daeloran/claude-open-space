"""Tests d'intention pour l'issue #29 : employés terminal en attente (`waiting`) et inactifs (`idle`).

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seuls les critères testables côté backend sont couverts ; le front est vérifié à la main (`?demo`).

Contrat public supposé (en plus de #13 / #25) :
- `observed_status` et `observed_joined` (dans `agent`) portent `waiting_for` = `waitingFor` du
  fichier de session quand `status == "waiting"` ; la clé est absente sinon.
- Aucun autre champ du fichier de session (socket de messagerie, `.key`…) n'apparaît dans les événements.
- `chat_entries` : une entrée issue d'un tool_use `AskUserQuestion` expose chaque texte de question et
  chaque libellé d'option (structure libre : vérifié sur `json.dumps(entry)`).
"""
import json
import uuid

import pytest

from tests.spec.test_issue13_observer import (  # noqa: F401 (fixtures)
    Alive, aid, append, assistant, cfg, harness, new_sid, of, proj, prompt, transcript_path,
)

SOCKET = "/run/secret-messaging-29.sock"
KEY_SECRET = "KEY-SECRET-29-fedcba9876543210"


def observer_mod():
    import backend.observer as m

    return m


def write_session(cfg, pid, sid, cwd, status="busy", waiting_for=None):
    d = cfg / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"pid": pid, "sessionId": sid, "cwd": str(cwd), "name": f"eter-{pid}", "status": status,
           "entrypoint": "cli", "messagingSocketPath": SOCKET, "updatedAt": 1790000000000}
    if waiting_for is not None:
        rec["waitingFor"] = waiting_for
    (d / f"{pid}.json").write_text(json.dumps(rec))
    (d / f"{pid}.{uuid.uuid4().hex[:12]}.key").write_text(KEY_SECRET)


def start(cfg, proj, harness, **kw):
    sid = new_sid()
    append(transcript_path(cfg, sid, proj), prompt())
    write_session(cfg, 101, sid, proj, **kw)
    h = harness(cfg, Alive(101))
    return sid, h


# ---------------------------------------------------------------- observed_status


def test_status_waiting_porte_waiting_for(cfg, proj, harness):
    sid, h = start(cfg, proj, harness)
    h.poll()
    write_session(cfg, 101, sid, proj, status="waiting", waiting_for="input needed")
    evs = of(h.poll(), "observed_status", aid(sid))
    assert evs == [{"type": "observed_status", "agent_id": aid(sid),
                    "status": "waiting", "waiting_for": "input needed"}]


def test_retour_a_busy_sans_waiting_for(cfg, proj, harness):
    sid, h = start(cfg, proj, harness)
    h.poll()
    write_session(cfg, 101, sid, proj, status="waiting", waiting_for="dialog open")
    h.poll()
    write_session(cfg, 101, sid, proj, status="busy")
    evs = of(h.poll(), "observed_status", aid(sid))
    assert evs == [{"type": "observed_status", "agent_id": aid(sid), "status": "busy"}]


def test_idle_sans_waiting_for(cfg, proj, harness):
    sid, h = start(cfg, proj, harness)
    h.poll()
    write_session(cfg, 101, sid, proj, status="idle")
    evs = of(h.poll(), "observed_status", aid(sid))
    assert evs == [{"type": "observed_status", "agent_id": aid(sid), "status": "idle"}]


# ---------------------------------------------------------------- observed_joined


def test_joined_en_attente_porte_waiting_for(cfg, proj, harness):
    sid, h = start(cfg, proj, harness, status="waiting", waiting_for="input needed")
    (ev,) = of(h.poll(), "observed_joined", aid(sid))
    assert ev["agent"]["status"] == "waiting"
    assert ev["agent"]["waiting_for"] == "input needed"


def test_joined_busy_sans_waiting_for(cfg, proj, harness):
    sid, h = start(cfg, proj, harness, status="busy")
    (ev,) = of(h.poll(), "observed_joined", aid(sid))
    assert ev["agent"]["status"] == "busy"
    assert "waiting_for" not in ev["agent"]


# ---------------------------------------------------------------- pas de fuite


def test_aucun_autre_champ_du_fichier_de_session(cfg, proj, harness):
    sid, h = start(cfg, proj, harness, status="waiting", waiting_for="input needed")
    h.poll()
    write_session(cfg, 101, sid, proj, status="busy")
    h.poll()
    write_session(cfg, 101, sid, proj, status="waiting", waiting_for="dialog open")
    h.poll()
    assert any(e.get("waiting_for") == "dialog open" for e in h.events), "waiting_for attendu"
    blob = json.dumps(h.events)
    for f in ("messagingSocketPath", SOCKET, KEY_SECRET, "updatedAt", "waitingFor", "entrypoint"):
        assert f not in blob, f


# ---------------------------------------------------------------- chat : AskUserQuestion


ASK_INPUT = {"questions": [
    {"question": "QUESTION-UNE : quelle base ?", "header": "Base", "multiSelect": False,
     "options": [{"label": "OPT-POSTGRES", "description": "relationnelle"},
                 {"label": "OPT-SQLITE", "description": "fichier"}]},
    {"question": "QUESTION-DEUX : quel port ?", "header": "Port", "multiSelect": True,
     "options": [{"label": "OPT-8080", "description": "d"}, {"label": "OPT-9000", "description": "d"}]},
]}


def test_ask_user_question_expose_questions_et_options():
    rec = assistant([{"type": "tool_use", "id": "toolu_ask", "name": "AskUserQuestion", "input": ASK_INPUT}])
    entries = observer_mod().chat_entries([rec])
    assert len(entries) == 1, entries
    blob = json.dumps(entries[0], ensure_ascii=False)
    for q in ASK_INPUT["questions"]:
        assert q["question"] in blob, q["question"]
        for o in q["options"]:
            assert o["label"] in blob, o["label"]


def test_ask_user_question_input_malforme_sans_crash():
    for inp in ({}, {"questions": "x"}, {"questions": [{"options": None}]}, {"questions": [None]}):
        rec = assistant([{"type": "tool_use", "id": "t", "name": "AskUserQuestion", "input": inp}])
        observer_mod().chat_entries([rec])
