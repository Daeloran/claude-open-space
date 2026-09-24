"""Tests d'intention pour l'issue #5 : usage du plan (fenêtre 5 h et semaine).

Écrits en boîte noire depuis l'issue, sans lire l'implémentation.

Contrat public supposé — module `backend/plan_usage.py` :

- `read_token(config_dir: Path) -> str | None`
    Lit `config_dir/.credentials.json`, clé `claudeAiOauth.accessToken`.
    None si fichier absent / illisible / clé manquante.
- `parse_usage(data: dict) -> dict`
    Depuis la réponse API `{"five_hour": {"utilization": .., "resets_at": ..}, "seven_day": {..}, ...}`
    renvoie `{"type": "plan_usage", "five_hour": {"utilization", "resets_at"} | None,
    "seven_day": {...} | None}`. Format inattendu -> fenêtre à None, jamais d'exception.
- `PlanUsage(config_dir: Path, fetch=<async (token) -> dict>, ttl: float = 180, clock=<() -> float>)`
    `async def get() -> dict` renvoie l'événement `plan_usage`.
    Pas de token -> fenêtres None, `fetch` jamais appelé.
    `fetch` qui lève -> fenêtres None, pas d'exception.
    Deux `get()` dans le TTL -> un seul appel à `fetch`.
    L'événement ne contient jamais le token.
- Le `fetch` par défaut (non testé ici, aucun appel réseau) appellera
  `GET https://api.anthropic.com/api/oauth/usage` avec `Authorization: Bearer <token>`
  et `anthropic-beta: oauth-2025-04-20`.

Aucun test ne lit le vrai `~/.claude/.credentials.json` : tout passe par `tmp_path`.
"""

import json

import pytest

from backend.plan_usage import PlanUsage, parse_usage, read_token

TOKEN = "tok-test-123"
RESET_5H = "2026-09-24T15:00:00+00:00"
RESET_7D = "2026-09-28T08:00:00+00:00"
API_OK = {
    "five_hour": {"utilization": 42.0, "resets_at": RESET_5H},
    "seven_day": {"utilization": 17.0, "resets_at": RESET_7D},
    "seven_day_opus": None,
}


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": TOKEN}})
    )
    return tmp_path


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeFetch:
    def __init__(self, result=None, exc=None):
        self.result = API_OK if result is None else result
        self.exc = exc
        self.tokens = []

    async def __call__(self, token):
        self.tokens.append(token)
        if self.exc:
            raise self.exc
        return self.result


def assert_unavailable(event):
    assert event["type"] == "plan_usage"
    assert event["five_hour"] is None
    assert event["seven_day"] is None


# --- read_token ---------------------------------------------------------------


def test_read_token_reads_access_token(config_dir):
    assert read_token(config_dir) == TOKEN


def test_read_token_missing_file_returns_none(tmp_path):
    assert read_token(tmp_path) is None


@pytest.mark.parametrize(
    "content",
    ["not json {", json.dumps({}), json.dumps({"claudeAiOauth": {}}), json.dumps([1, 2])],
)
def test_read_token_unreadable_or_missing_key_returns_none(tmp_path, content):
    (tmp_path / ".credentials.json").write_text(content)
    assert read_token(tmp_path) is None


# --- parse_usage --------------------------------------------------------------


def test_parse_usage_nominal():
    event = parse_usage(API_OK)
    assert event["type"] == "plan_usage"
    assert event["five_hour"]["utilization"] == 42
    assert event["five_hour"]["resets_at"] == RESET_5H
    assert event["seven_day"]["utilization"] == 17
    assert event["seven_day"]["resets_at"] == RESET_7D


def test_parse_usage_ignores_out_of_scope_windows():
    event = parse_usage(API_OK)
    assert "seven_day_opus" not in event


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"five_hour": None, "seven_day": None},
        {"five_hour": "garbage", "seven_day": 12},
        {"five_hour": {"resets_at": RESET_5H}, "seven_day": {}},
        {"unexpected": True},
    ],
)
def test_parse_usage_unexpected_format_gives_none_windows(data):
    assert_unavailable(parse_usage(data))


def test_parse_usage_partial_keeps_valid_window():
    event = parse_usage({"five_hour": {"utilization": 42.0, "resets_at": RESET_5H}})
    assert event["five_hour"]["utilization"] == 42
    assert event["seven_day"] is None


# --- PlanUsage ----------------------------------------------------------------


async def test_get_returns_plan_usage_event(config_dir):
    fetch = FakeFetch()
    event = await PlanUsage(config_dir, fetch=fetch, clock=FakeClock()).get()
    assert event["type"] == "plan_usage"
    assert event["five_hour"]["utilization"] == 42
    assert event["five_hour"]["resets_at"] == RESET_5H
    assert event["seven_day"]["utilization"] == 17
    assert event["seven_day"]["resets_at"] == RESET_7D
    assert fetch.tokens == [TOKEN]


async def test_no_credentials_no_fetch(tmp_path):
    fetch = FakeFetch()
    event = await PlanUsage(tmp_path, fetch=fetch, clock=FakeClock()).get()
    assert_unavailable(event)
    assert fetch.tokens == []


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("HTTP 401 Unauthorized"),
        RuntimeError("HTTP 500 Internal Server Error"),
        json.JSONDecodeError("Expecting value", "<html>", 0),
        ValueError("bad payload"),
    ],
)
async def test_fetch_error_gives_unavailable(config_dir, exc):
    event = await PlanUsage(config_dir, fetch=FakeFetch(exc=exc), clock=FakeClock()).get()
    assert_unavailable(event)


async def test_unexpected_api_payload_gives_unavailable(config_dir):
    fetch = FakeFetch(result={"error": {"type": "authentication_error"}})
    assert_unavailable(await PlanUsage(config_dir, fetch=fetch, clock=FakeClock()).get())


async def test_two_gets_within_ttl_single_fetch(config_dir):
    fetch, clock = FakeFetch(), FakeClock()
    usage = PlanUsage(config_dir, fetch=fetch, ttl=180, clock=clock)
    first = await usage.get()
    clock.now += 60
    second = await usage.get()
    assert len(fetch.tokens) == 1
    assert first == second


async def test_default_ttl_is_180s(config_dir):
    fetch, clock = FakeFetch(), FakeClock()
    usage = PlanUsage(config_dir, fetch=fetch, clock=clock)
    await usage.get()
    clock.now += 179
    await usage.get()
    assert len(fetch.tokens) == 1


async def test_refetch_after_ttl(config_dir):
    fetch, clock = FakeFetch(), FakeClock()
    usage = PlanUsage(config_dir, fetch=fetch, ttl=180, clock=clock)
    await usage.get()
    clock.now += 181
    await usage.get()
    assert len(fetch.tokens) == 2


async def test_token_never_in_event(config_dir):
    # Même si l'API renvoyait le token, il ne doit pas fuiter dans l'événement.
    payload = dict(API_OK, echo=TOKEN)
    for fetch in (FakeFetch(result=payload), FakeFetch(exc=RuntimeError(f"401 for {TOKEN}"))):
        event = await PlanUsage(config_dir, fetch=fetch, clock=FakeClock()).get()
        assert TOKEN not in json.dumps(event)
