"""Tests d'intention pour l'issue #30 : sous-agents des sessions terminal affichés comme stagiaires.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Réutilise les faux ~/.claude et le harnais de l'Observer de l'issue #13.

Contrat supposé (même forme que pour les employés pilotés) :
- `Agent`/`Task` tool_use non sidechain → `{"type": "subagent_spawned", "parent_id": <id observé>,
  "agent": {"id", "name"}, "task": <résumé>}` ; id de stagiaire unique par tool_use.
- tool_result avec le même `tool_use_id` → `{"type": "subagent_done", "agent_id": <id stagiaire>}`.
- Session qui part avec des stagiaires actifs → `subagent_done` pour chacun.
"""
import pytest

from tests.spec.test_issue13_observer import (  # noqa: F401  (fixtures réutilisées)
    Alive, aid, append, assistant, cfg, harness, new_sid, of, prompt, proj, remove_session,
    tool_result, tool_use, transcript_path, write_session,
)


def started(cfg, proj, harness, alive=None):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt())
    write_session(cfg, 101, sid, proj)
    alive = alive or Alive(101)
    h = harness(cfg, alive)
    h.poll()
    return h, t, aid(sid)


def agent_use(tid, name="Agent", desc="explore the repo", sidechain=False):
    return tool_use(tid, name=name, inp={"description": desc, "prompt": "find the observer code",
                                         "subagent_type": "Explore"}, sidechain=sidechain)


def check_spawn(ev, parent):
    assert ev["parent_id"] == parent
    assert isinstance(ev["agent"]["id"], str) and ev["agent"]["id"]
    assert isinstance(ev["agent"]["name"], str) and ev["agent"]["name"]
    assert isinstance(ev["task"], str) and ev["task"].strip()


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_tool_use_agent_fait_venir_un_stagiaire(cfg, proj, harness, tool):
    h, t, parent = started(cfg, proj, harness)
    append(t, agent_use("toolu_a1", name=tool))
    spawned = of(h.poll(), "subagent_spawned")
    assert len(spawned) == 1
    check_spawn(spawned[0], parent)
    assert of(h.poll(), "subagent_spawned") == [], "une ligne n'est traitée qu'une fois"


def test_tool_result_correspondant_fait_partir_le_stagiaire(cfg, proj, harness):
    h, t, parent = started(cfg, proj, harness)
    append(t, agent_use("toolu_a1"))
    (sp,) = of(h.poll(), "subagent_spawned")
    append(t, tool_result("toolu_a1"))
    assert of(h.poll(), "subagent_done") == [{"type": "subagent_done", "agent_id": sp["agent"]["id"]}]


def test_deux_agents_dans_un_message_donnent_deux_stagiaires(cfg, proj, harness):
    h, t, parent = started(cfg, proj, harness)
    inp = {"description": "d", "prompt": "p", "subagent_type": "Explore"}
    append(t, assistant([
        {"type": "tool_use", "id": "toolu_a1", "name": "Agent", "input": dict(inp, description="one")},
        {"type": "tool_use", "id": "toolu_a2", "name": "Agent", "input": dict(inp, description="two")},
    ]))
    spawned = of(h.poll(), "subagent_spawned")
    assert len(spawned) == 2
    for ev in spawned:
        check_spawn(ev, parent)
    ids = [ev["agent"]["id"] for ev in spawned]
    assert len(set(ids)) == 2
    append(t, tool_result("toolu_a2"))
    assert [e["agent_id"] for e in of(h.poll(), "subagent_done")] == [ids[1]]


def test_sidechain_ne_fait_pas_venir_de_stagiaire(cfg, proj, harness):
    h, t, _ = started(cfg, proj, harness)
    append(t, agent_use("toolu_s1", sidechain=True))
    assert of(h.poll(), "subagent_spawned") == []


@pytest.mark.parametrize("how", ["session_retiree", "pid_mort"])
def test_depart_de_la_session_renvoie_les_stagiaires(cfg, proj, harness, how):
    alive = Alive(101)
    h, t, parent = started(cfg, proj, harness, alive)
    append(t, agent_use("toolu_a1"))
    (sp,) = of(h.poll(), "subagent_spawned")
    if how == "session_retiree":
        remove_session(cfg, 101)
    else:
        alive.pids.clear()
    evs = h.poll()
    assert of(evs, "subagent_done") == [{"type": "subagent_done", "agent_id": sp["agent"]["id"]}]
    assert of(h.poll(), "subagent_done") == [], "une seule fois"
