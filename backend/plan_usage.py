"""Usage du plan Claude (fenêtre de 5 h et semaine), comme la barre d'état de Claude Code.

Source : endpoint OAuth non documenté `GET /api/oauth/usage` (même appel que ccstatusline).
Réponse : `{"five_hour": {"utilization": 42.0, "resets_at": "2026-09-24T15:00:00+00:00"}, "seven_day": {...}, ...}`
avec `utilization` en pourcentage (0-100). Tout échec donne des fenêtres à None (« — » côté front).
Le token ne sort jamais de ce module, sauf vers api.anthropic.com.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
WINDOWS = ("five_hour", "seven_day")


def read_token(config_dir: Path) -> str | None:
    try:
        token = json.loads((Path(config_dir) / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return token if isinstance(token, str) and token else None


def _window(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pct = raw.get("utilization")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    reset = raw.get("resets_at")
    return {"utilization": pct, "resets_at": reset if isinstance(reset, str) else None}


def parse_usage(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    return {"type": "plan_usage", **{w: _window(data.get(w)) for w in WINDOWS}}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib recopierait l'en-tête Authorization vers la cible d'une redirection
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


async def fetch_usage(token: str) -> dict:
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
    })

    def call() -> dict:
        with _opener.open(req, timeout=5) as resp:
            return json.load(resp)

    return await asyncio.to_thread(call)


class PlanUsage:
    """Dernier événement `plan_usage`, rafraîchi au plus une fois par `ttl` secondes."""

    def __init__(self, config_dir: Path, fetch=fetch_usage, ttl: float = 180, clock=time.monotonic) -> None:
        self.config_dir = Path(config_dir)
        self.fetch, self.ttl, self.clock = fetch, ttl, clock
        self.event = parse_usage({})
        self._fetched_at: float | None = None

    async def get(self) -> dict:
        now = self.clock()
        if self._fetched_at is None or now - self._fetched_at >= self.ttl:
            self._fetched_at = now  # un échec est aussi mis en cache : pas de rafale de requêtes
            self.event = await self._load()
        return self.event

    async def _load(self) -> dict:
        token = read_token(self.config_dir)
        if not token:
            return parse_usage({})
        try:
            return parse_usage(await self.fetch(token))
        except Exception:  # réseau, HTTP 4xx/5xx, JSON invalide : jamais loggé (le message peut contenir le token)
            return parse_usage({})

    def apply_rate_limit(self, info) -> bool:
        """Fusionne un `RateLimitInfo` du SDK (utilization 0-1, resets_at en timestamp Unix). True si modifié."""
        kind, ratio = getattr(info, "rate_limit_type", None), getattr(info, "utilization", None)
        if kind not in WINDOWS or not isinstance(ratio, (int, float)):
            return False
        old = self.event.get(kind) or {}
        ts = getattr(info, "resets_at", None)
        reset = datetime.fromtimestamp(ts, timezone.utc).isoformat() if isinstance(ts, (int, float)) else old.get("resets_at")
        self.event = {**self.event, kind: {"utilization": round(ratio * 100, 1), "resets_at": reset}}
        return True
