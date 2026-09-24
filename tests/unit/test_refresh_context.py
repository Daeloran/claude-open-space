"""Tests de logique de Employee.refresh_context."""

import pytest

from backend import app


class Client:
    def __init__(self, resp):
        self.resp = resp

    async def get_context_usage(self):
        return self.resp


@pytest.fixture
def events(monkeypatch):
    captured: list[dict] = []

    async def fake_emit(event: dict) -> None:
        captured.append(event)

    monkeypatch.setattr(app.hub, "emit", fake_emit)
    return captured


async def ratios(resp, events):
    await app.Employee(0, "Léa").refresh_context(Client(resp))
    return [e["ratio"] for e in events if e["type"] == "context"]


async def test_ratio_above_one_is_capped(events):
    assert await ratios({"totalTokens": 300_000, "maxTokens": 200_000}, events) == [1.0]


async def test_negative_ratio_is_floored(events):
    assert await ratios({"totalTokens": -5, "maxTokens": 200_000}, events) == [0.0]


async def test_zero_max_tokens_falls_back_on_context_window(events, monkeypatch):
    monkeypatch.setattr(app, "CONTEXT_WINDOW", 100_000)
    assert await ratios({"totalTokens": 50_000, "maxTokens": 0}, events) == [pytest.approx(0.5)]


async def test_uses_max_tokens_not_raw(events):
    resp = {"totalTokens": 50_000, "maxTokens": 100_000, "rawMaxTokens": 200_000}
    assert await ratios(resp, events) == [pytest.approx(0.5)]


async def test_missing_total_tokens_emits_nothing(events):
    assert await ratios({"maxTokens": 200_000}, events) == []
