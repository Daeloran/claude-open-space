"""Tests de logique de backend/plan_usage.py."""
import json

from claude_agent_sdk.types import RateLimitInfo

from backend import plan_usage
from backend.plan_usage import PlanUsage, parse_usage


def usage(tmp_path, result):
    async def fetch(token):
        return result
    return PlanUsage(tmp_path, fetch=fetch)


def test_parse_usage_rejects_bool_and_keeps_none_reset():
    event = parse_usage({"five_hour": {"utilization": True}, "seven_day": {"utilization": 3, "resets_at": 12}})
    assert event["five_hour"] is None
    assert event["seven_day"] == {"utilization": 3, "resets_at": None}


def test_parse_usage_non_dict_payload():
    assert parse_usage([1, 2]) == {"type": "plan_usage", "five_hour": None, "seven_day": None}


def test_rate_limit_converts_ratio_and_timestamp(tmp_path):
    u = usage(tmp_path, {})
    assert u.apply_rate_limit(RateLimitInfo(status="allowed", rate_limit_type="five_hour", utilization=0.425, resets_at=0))
    assert u.event["five_hour"] == {"utilization": 42.5, "resets_at": "1970-01-01T00:00:00+00:00"}
    assert u.event["seven_day"] is None


async def test_rate_limit_keeps_previous_reset_and_other_window(tmp_path):
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
    u = usage(tmp_path, {"five_hour": {"utilization": 10, "resets_at": "R5"}, "seven_day": {"utilization": 20, "resets_at": "R7"}})
    await u.get()
    assert u.apply_rate_limit(RateLimitInfo(status="allowed_warning", rate_limit_type="seven_day", utilization=0.9))
    assert u.event["seven_day"] == {"utilization": 90.0, "resets_at": "R7"}
    assert u.event["five_hour"] == {"utilization": 10, "resets_at": "R5"}


def test_rate_limit_ignores_other_types_and_missing_utilization(tmp_path):
    u = usage(tmp_path, {})
    before = u.event
    assert not u.apply_rate_limit(RateLimitInfo(status="allowed", rate_limit_type="seven_day_opus", utilization=0.5))
    assert not u.apply_rate_limit(RateLimitInfo(status="allowed", rate_limit_type="five_hour"))
    assert u.event == before


def test_redirects_are_not_followed():
    # Le token ne doit pas suivre une redirection hors d'api.anthropic.com
    handler = next(h for h in plan_usage._opener.handlers if isinstance(h, plan_usage._NoRedirect))
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://ailleurs.example") is None
