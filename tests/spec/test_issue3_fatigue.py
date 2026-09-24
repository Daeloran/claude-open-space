"""Tests d'intention pour l'issue #3 : la fatigue suit le contexte réel de chaque employé.

Écrits depuis l'issue uniquement (boîte noire). Contrat public supposé :

- `backend.app.Employee(idx, name)` construit un employé ; `backend.app.hub.emit(event)`
  est l'unique point d'émission des événements (monkeypatché ici pour les capturer).
- `await Employee.refresh_context(client)` interroge `client.get_context_usage()`
  (réponse au format `claude_agent_sdk.ContextUsageResponse`) et émet
  `{"type": "context", "agent_id": <id>, "ratio": totalTokens / maxTokens}`.
  Si la taille max manque dans la réponse, repli sur `OPENSPACE_CONTEXT`
  (constante `backend.app.CONTEXT_WINDOW`). Si `get_context_usage` lève :
  aucune exception propagée, aucun événement `context` émis.
- `await Employee.translate(msg)` sur un `ResultMessage` n'émet plus d'événement
  `context` (le `cost` reste émis), quel que soit le `usage` cumulé.
- `await Employee.translate(SystemMessage("compact_boundary", ...))` émet `compaction` ;
  la nouvelle mesure vient ensuite de `refresh_context`.

Aucun vrai Claude : les clients sont des faux avec `async def get_context_usage`.
"""

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

from backend import app


@pytest.fixture
def events(monkeypatch):
    captured: list[dict] = []

    async def fake_emit(event: dict) -> None:
        captured.append(event)

    monkeypatch.setattr(app.hub, "emit", fake_emit)
    return captured


@pytest.fixture
def employee():
    return app.Employee(0, "Léa")


def usage(total, max_tokens=None):
    resp = {"categories": [], "totalTokens": total, "percentage": 0.0, "model": "claude"}
    if max_tokens is not None:
        # maxTokens (effectif) et rawMaxTokens (brut) identiques : le test ne tranche pas entre les deux.
        resp["maxTokens"] = max_tokens
        resp["rawMaxTokens"] = max_tokens
        resp["percentage"] = 100.0 * total / max_tokens
    return resp


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    async def get_context_usage(self):
        self.calls += 1
        return self.responses.pop(0)


class BrokenClient:
    async def get_context_usage(self):
        raise RuntimeError("mesure impossible")


def contexts(events):
    return [e for e in events if e.get("type") == "context"]


def result_message(huge_input_tokens):
    return ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=12,
        session_id="s1",
        total_cost_usd=0.42,
        usage={
            "input_tokens": huge_input_tokens,
            "cache_read_input_tokens": huge_input_tokens,
            "cache_creation_input_tokens": huge_input_tokens,
            "output_tokens": 5000,
        },
        result="ok",
    )


async def test_ratio_equals_session_context_usage(events, employee):
    client = FakeClient(usage(50_000, 200_000))
    await employee.refresh_context(client)

    ctx = contexts(events)
    assert client.calls == 1
    assert len(ctx) == 1
    assert "agent_id" in ctx[0]
    assert ctx[0]["ratio"] == pytest.approx(0.25)


async def test_window_size_comes_from_session_not_env(events, employee, monkeypatch):
    monkeypatch.setenv("OPENSPACE_CONTEXT", "1000000")
    monkeypatch.setattr(app, "CONTEXT_WINDOW", 1_000_000)
    await employee.refresh_context(FakeClient(usage(50_000, 100_000)))

    assert contexts(events)[-1]["ratio"] == pytest.approx(0.5)


async def test_window_size_falls_back_on_env_when_missing(events, employee, monkeypatch):
    monkeypatch.setenv("OPENSPACE_CONTEXT", "100000")
    monkeypatch.setattr(app, "CONTEXT_WINDOW", 100_000)
    await employee.refresh_context(FakeClient(usage(25_000)))

    assert contexts(events)[-1]["ratio"] == pytest.approx(0.25)


async def test_multi_turn_result_usage_does_not_drive_fatigue(events, employee):
    await employee.translate(result_message(huge_input_tokens=5_000_000))

    assert contexts(events) == []
    assert any(e.get("type") == "cost" for e in events)

    await employee.refresh_context(FakeClient(usage(30_000, 200_000)))
    ctx = contexts(events)
    assert len(ctx) == 1
    assert ctx[0]["ratio"] == pytest.approx(0.15)


async def test_measure_failure_is_silent_and_emits_no_context(events, employee):
    await employee.refresh_context(FakeClient(usage(80_000, 200_000)))
    await employee.refresh_context(BrokenClient())  # ne doit pas lever

    ctx = contexts(events)
    assert len(ctx) == 1
    assert ctx[0]["ratio"] == pytest.approx(0.4)


async def test_compaction_signalled_then_ratio_drops(events, employee):
    client = FakeClient(usage(160_000, 200_000), usage(20_000, 200_000))
    await employee.refresh_context(client)

    await employee.translate(SystemMessage(subtype="compact_boundary", data={"compact_metadata": {"trigger": "auto"}}))
    assert any(e.get("type") == "compaction" for e in events)

    await employee.refresh_context(client)
    ratios = [e["ratio"] for e in contexts(events)]
    assert ratios == pytest.approx([0.8, 0.1])
    assert ratios[-1] < ratios[0]
