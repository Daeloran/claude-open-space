"""Tests d'intention pour l'issue #51 : les stats de l'en-tête et de la Direction survivent à un rechargement.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; la restauration côté front (S.plan, S.deliv, S.coffee, S.perms,
S.denied) est vérifiée à la main.

Contrat public supposé — le `snapshot` envoyé à tout nouveau client /ws porte en plus :
- `plan_usage` : le dernier événement `plan_usage` émis (au moins `five_hour` et `seven_day`
  identiques à ceux émis), ou None si aucun n'a été émis.
- `deliverables` : liste de `{"path": str, "kind": str}` (clés en plus tolérées), dans l'ordre
  d'émission, bornée : au-delà de la borne, les plus anciens sont retirés, les plus récents gardés.
- `coffee` : int, nombre d'événements `compaction` émis (pauses café).
- `permissions` : `{"total": int, "denied": int}`, décisions de permission rendues via /ws
  (`permission_decision` allow true/false).

Hypothèses :
- L'état est alimenté par les événements passés par `hub.emit` (`plan_usage`, `deliverable`,
  `compaction`) et par le flux réel de permission (`Employee.can_use_tool` → `permission_request`
  → `permission_decision` → `permission_resolved`).
- `hub` est un singleton partagé par toute la session pytest : les compteurs sont vérifiés par
  différence (avant / après) ; la borne des livrables est inconnue mais < 500 et ≥ 4
  (le panneau affiche les 4 derniers).
- Deux clients connectés l'un après l'autre reçoivent les mêmes valeurs (état tenu par le backend).

Même harnais que test_issue33_ask_desk : TestClient sans lifespan, portail partagé.
"""
import uuid

from tests.spec.test_issue4_replay import client, connect, emit, handshake  # noqa: F401
from tests.spec.test_issue33_ask_desk import ask

PLAN = {
    "type": "plan_usage",
    "five_hour": {"utilization": 37.0, "resets_at": "2026-09-25T15:00:00+00:00"},
    "seven_day": {"utilization": 64.0, "resets_at": "2026-09-29T08:00:00+00:00"},
}


def snapshot(client):
    with connect(client) as ws:
        _, snap = handshake(ws)
    return snap


def observed_agent(client, tmp_path):
    agent = {"id": "o-" + uuid.uuid4().hex[:8], "name": "eter-51", "cwd": str(tmp_path),
             "project": tmp_path.name, "status": "busy", "observed": True}
    emit(client, {"type": "observed_joined", "agent": agent})
    return agent["id"]


# ---------------------------------------------------------------- plan usage


def test_snapshot_porte_le_dernier_plan_usage(client):
    emit(client, {**PLAN, "five_hour": {"utilization": 1.0, "resets_at": "2026-09-25T10:00:00+00:00"}})
    emit(client, PLAN)
    snap = snapshot(client)
    assert "plan_usage" in snap, snap.keys()
    got = snap["plan_usage"]
    assert isinstance(got, dict), got
    assert got["five_hour"] == PLAN["five_hour"]
    assert got["seven_day"] == PLAN["seven_day"]


# ---------------------------------------------------------------- livrables


def test_snapshot_porte_les_livrables(client, tmp_path):
    aid = observed_agent(client, tmp_path)
    tag = uuid.uuid4().hex[:8]
    sent = [{"path": f"src/{tag}-{i}.py", "kind": k} for i, k in enumerate(["write", "edit", "commit"])]
    try:
        for d in sent:
            emit(client, {"type": "deliverable", "agent_id": aid, **d})
        snap = snapshot(client)
    finally:
        emit(client, {"type": "observed_left", "agent_id": aid})
    assert isinstance(snap.get("deliverables"), list), snap.keys()
    tail = [{"path": d.get("path"), "kind": d.get("kind")} for d in snap["deliverables"][-3:]]
    assert tail == sent


def test_livrables_bornes_les_plus_recents_gardes(client, tmp_path):
    aid = observed_agent(client, tmp_path)
    tag = uuid.uuid4().hex[:8]
    n = 500
    try:
        for i in range(n):
            emit(client, {"type": "deliverable", "agent_id": aid, "path": f"f/{tag}-{i}.py", "kind": "edit"})
        snap = snapshot(client)
    finally:
        emit(client, {"type": "observed_left", "agent_id": aid})
    got = snap.get("deliverables")
    assert isinstance(got, list), snap.keys()
    assert 4 <= len(got) < n, f"liste non bornée : {len(got)} éléments"
    paths = [d.get("path") for d in got]
    # uniquement les plus récents, dans l'ordre, sans trou
    assert paths == [f"f/{tag}-{i}.py" for i in range(n - len(got), n)], paths[:5]


# ---------------------------------------------------------------- pauses café


def test_snapshot_porte_le_nombre_de_pauses_cafe(client, tmp_path):
    before = snapshot(client)
    assert isinstance(before.get("coffee"), int), before.keys()
    aid = observed_agent(client, tmp_path)
    try:
        for _ in range(3):
            emit(client, {"type": "compaction", "agent_id": aid})
        after = snapshot(client)
    finally:
        emit(client, {"type": "observed_left", "agent_id": aid})
    assert after["coffee"] - before["coffee"] == 3


# ---------------------------------------------------------------- validations


def test_snapshot_porte_les_validations_totales_et_refusees(client):
    before = snapshot(client)
    assert isinstance(before.get("permissions"), dict), before.keys()
    ask(client, "Bash", {"command": "ls"}, {"allow": True})
    ask(client, "Bash", {"command": "rm -rf /tmp/x"}, {"allow": False})
    after = snapshot(client)
    assert after["permissions"]["total"] - before["permissions"]["total"] == 2
    assert after["permissions"]["denied"] - before["permissions"]["denied"] == 1


def test_etat_tenu_par_le_backend_pas_par_la_connexion(client):
    """Un client qui se connecte après coup voit les mêmes compteurs qu'un autre."""
    emit(client, PLAN)
    a, b = snapshot(client), snapshot(client)
    for k in ("plan_usage", "deliverables", "coffee", "permissions"):
        assert k in a, a.keys()
        assert a[k] == b[k], k

