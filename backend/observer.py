"""Sessions Claude Code du terminal, affichées en lecture seule comme employés « observés ».

Registre : `$CLAUDE_CONFIG_DIR/sessions/<pid>.json` (format non documenté, parsing défensif). Seuls
les champs non secrets sont lus ; les fichiers `*.key` ne sont jamais ouverts (glob strict `*.json`).
Activité : fin du transcript `projects/*/<sessionId>.jsonl`, lue au fil de l'eau par offset.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from .events import summarize_tool

TAIL_BYTES = 256 * 1024  # fin du transcript lue pour la fatigue initiale
BIG_WINDOW = 1_000_000


def pid_alive(pid: int) -> bool:
    if pid <= 0:  # 0 / négatif : groupe de processus, pas une session
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # existe, mais appartient à un autre utilisateur
        return True
    except (OSError, OverflowError):
        return False
    return True


def live_sessions(config_dir: Path, pid_alive=pid_alive) -> list[dict]:
    """Sessions terminal (`entrypoint == "cli"`) dont le pid vit, champs non secrets seulement."""
    out = []
    try:
        files = sorted((Path(config_dir) / "sessions").glob("*.json"))
    except OSError:
        return []
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("entrypoint") != "cli":
            continue
        pid, sid, cwd = rec.get("pid"), rec.get("sessionId"), rec.get("cwd")
        if (type(pid) is not int or not isinstance(cwd, str) or not cwd
                or not isinstance(sid, str) or not re.fullmatch(r"[\w-]+", sid)):  # sid sert dans un glob
            continue
        if not pid_alive(pid):
            continue
        project = Path(cwd).name
        out.append({"session_id": sid, "pid": pid, "cwd": cwd, "name": str(rec.get("name") or project),
                    "status": str(rec.get("status") or "idle"), "project": project})
    return out


def context_window_for(cwd, config_dir: Path, default: int) -> int:
    """Fenêtre du modèle réglé pour `cwd` : premier `model` des réglages projet local, projet, utilisateur."""
    claude = Path(cwd) / ".claude"
    for f in (claude / "settings.local.json", claude / "settings.json", Path(config_dir) / "settings.json"):
        try:
            model = json.loads(f.read_text(encoding="utf-8")).get("model")
        except (OSError, ValueError, AttributeError):  # absent, illisible, invalide, pas un objet
            continue
        if model:
            return BIG_WINDOW if "[1m]" in str(model) else default
    return default


class TranscriptTail:
    """Lecture incrémentale d'un JSONL : seulement les lignes complètes ajoutées depuis le dernier appel."""

    def __init__(self, path: Path, from_end: bool = False) -> None:
        self.path = Path(path)
        self.offset: int | None = None if from_end else 0
        self.buf = b""

    def read_new(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                if self.offset is None:
                    self.offset = size
                    return []
                if size < self.offset:  # tronqué ou remplacé : on repart du début
                    self.offset, self.buf = 0, b""
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        self.offset += len(data)
        *lines, self.buf = (self.buf + data).split(b"\n")
        return [r for r in map(_parse, lines) if r is not None]


def _parse(line: bytes) -> dict | None:
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _last_assistant_usage(path: Path) -> dict | None:
    """Dernier message assistant (hors sidechain) avec `usage`, cherché dans la fin du fichier."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().split(b"\n")
    except OSError:
        return None
    if size > TAIL_BYTES:
        lines = lines[1:]  # première ligne coupée
    # ponytail: un dernier message plus loin que 256 Ko → pas de fatigue initiale, elle vient au suivant
    for rec in reversed([r for r in map(_parse, lines) if r is not None]):
        msg = rec.get("message")
        if rec.get("type") == "assistant" and not rec.get("isSidechain") and isinstance(msg, dict) \
                and isinstance(msg.get("usage"), dict):
            return msg
    return None


class Observer:
    """`poll()` = une passe : registre + transcripts → événements de jeu via `emit`."""

    def __init__(self, config_dir: Path, emit, pid_alive=pid_alive, context_window: int = 200000) -> None:
        self.config_dir = Path(config_dir)
        self.emit = emit
        self.pid_alive = pid_alive
        self.window = context_window
        self.watched: dict[str, dict] = {}  # agent_id -> {"status", "sid", "tail", "tools"}

    async def poll(self) -> None:
        for ev in await asyncio.to_thread(self._scan):
            await self.emit(ev)

    def _scan(self) -> list[dict]:
        live = {"o-" + s["session_id"][:8]: s for s in live_sessions(self.config_dir, self.pid_alive)}
        out: list[dict] = []
        for aid in [a for a in self.watched if a not in live and self._gone(self.watched[a])]:
            del self.watched[aid]
            out.append({"type": "observed_left", "agent_id": aid})
        for aid, s in live.items():
            w = self.watched.get(aid)
            if w is None:
                w = self.watched[aid] = {"status": s["status"], "sid": s["session_id"], "pid": s["pid"],
                                         "tail": None, "tools": {},
                                         # ponytail: réglages lus à l'arrivée seulement ; un changement en cours
                                         # de session compte au prochain démarrage de la session / de l'Open Space
                                         "window": context_window_for(s["cwd"], self.config_dir, self.window)}
                out.append({"type": "observed_joined", "agent": {
                    "id": aid, "name": s["name"], "cwd": s["cwd"], "project": s["project"],
                    "status": s["status"], "observed": True}})
                if path := self._transcript(s["session_id"]):
                    # Session déjà en cours : pas de rejeu de l'historique, seulement sa fatigue actuelle
                    w["tail"] = TranscriptTail(path, from_end=True)
                    w["tail"].read_new()  # fixe l'offset avant de lire la fin : rien ne se perd entre les deux
                    if (msg := _last_assistant_usage(path)) and (r := self._ratio(msg, w["window"])) is not None:
                        out.append({"type": "context", "agent_id": aid, "ratio": r})
                continue
            if s["status"] != w["status"]:
                w["status"] = s["status"]
                out.append({"type": "observed_status", "agent_id": aid, "status": s["status"]})
            if w["tail"] is None:
                if not (path := self._transcript(w["sid"])):
                    continue
                w["tail"] = TranscriptTail(path)  # transcript créé après l'arrivée : tout est nouveau
            for rec in w["tail"].read_new():
                out.extend(self._events(aid, w, rec))
        return out

    def _gone(self, w: dict) -> bool:
        """Absente du registre : partie, sauf si son fichier est en cours de réécriture (JSON tronqué)."""
        if not self.pid_alive(w["pid"]):
            return True
        try:  # le registre nomme les fichiers `<pid>.json`
            json.loads((self.config_dir / "sessions" / f"{w['pid']}.json").read_text(encoding="utf-8"))
        except ValueError:
            return False
        except OSError:
            return True
        return True  # JSON valide mais plus une session cli vivante

    def _transcript(self, sid: str) -> Path | None:
        # Trouvé une fois puis gardé par le TranscriptTail (le nom du dossier projet n'est pas interprété)
        return next((self.config_dir / "projects").glob(f"*/{sid}.jsonl"), None)

    def _ratio(self, msg: dict, window: int | None = None) -> float | None:
        u = msg.get("usage")
        if not isinstance(u, dict):
            return None
        try:
            tokens = sum(int(u.get(k) or 0) for k in
                         ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        except (TypeError, ValueError):
            return None
        if tokens <= 0:  # message synthétique (erreur API…) : pas une mesure
            return None
        window = window or self.window
        # ponytail: heuristique, la vraie taille de fenêtre n'est pas dans le transcript : `model` des
        # réglages (context_window_for), puis secours `[1m]` dans le modèle du transcript ou tokens au-delà
        # de la fenêtre → 1M. Un `/model` en cours de session reste invisible.
        if "[1m]" in str(msg.get("model") or "") or tokens > window:
            window = BIG_WINDOW
        return min(1.0, tokens / window)

    def _events(self, aid: str, w: dict, rec: dict) -> list[dict]:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return []
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        blocks = [b for b in content if isinstance(b, dict)]
        out = []
        if rec.get("type") == "assistant":
            for b in blocks:
                if b.get("type") == "tool_use" and isinstance(name := b.get("name"), str):
                    w["tools"][str(b.get("id"))] = name
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    out.append({"type": "tool_use", "agent_id": aid, "tool": name,
                                "summary": summarize_tool(name, inp) or name})
            if not rec.get("isSidechain") and (r := self._ratio(msg, w["window"])) is not None:
                out.append({"type": "context", "agent_id": aid, "ratio": r})
        elif rec.get("type") == "user":
            for b in blocks:
                if b.get("type") == "tool_result":
                    out.append({"type": "tool_result", "agent_id": aid,
                                "tool": w["tools"].pop(str(b.get("tool_use_id")), "?"),
                                "ok": not b.get("is_error")})
        return out
