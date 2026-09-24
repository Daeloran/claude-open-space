"""Projets proposés au recrutement : dossiers de travail récents des sessions Claude Code."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from itertools import chain, islice
from pathlib import Path

MAX_LINES = 50  # lignes lues au plus par transcript pour y trouver un `cwd`


def first_cwd(path: Path, max_lines: int = MAX_LINES) -> str | None:
    """Premier champ `cwd` des `max_lines` premières lignes d'un transcript (sans le lire en entier)."""
    # ponytail: une première ligne géante (collage de plusieurs Mo) serait lue en entier ; rare en pratique
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in islice(f, max_lines):
                try:
                    cwd = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def recent_projects(config_dir: Path, extra: str | None = None, limit: int = 20) -> list[dict]:
    """`extra` puis les `cwd` des transcripts `config_dir/projects/*/*.jsonl`, plus récents d'abord,
    sans doublon, dossiers existants seulement, `limit` au plus."""
    files = sorted(Path(config_dir).glob("projects/*/*.jsonl"), key=_mtime, reverse=True)
    candidates = chain([os.path.abspath(extra)] if extra else [], (first_cwd(f) for f in files))
    seen: set[str] = set()
    out: list[dict] = []
    for cwd in candidates:  # générateur : on s'arrête de lire dès `limit` projets trouvés
        if not cwd:
            continue
        real = os.path.realpath(cwd)  # /home -> /var/home : même dossier, un seul projet
        if real in seen:
            continue
        seen.add(real)
        if os.path.isdir(cwd):
            out.append({"cwd": cwd, "name": Path(cwd).name})
            if len(out) >= limit:
                break
    return out[:limit]


def _prompt_text(rec: dict) -> str:
    """Texte d'un prompt utilisateur (hors messages internes et balises de commande), sinon ''."""
    msg = rec.get("message") if rec.get("type") == "user" and not rec.get("isMeta") else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        content = next((b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"), None)
    text = " ".join(content.split()) if isinstance(content, str) else ""
    return "" if text.startswith("<") else text


def resumable_sessions(config_dir: Path, live: set[str], limit: int = 30) -> list[dict]:
    """Sessions reprenables, plus récentes d'abord : id, dossier, titre (premier prompt), date ; `live` si un
    processus CLI la tient encore."""
    out = []
    for f in sorted(Path(config_dir).glob("projects/*/*.jsonl"), key=_mtime, reverse=True):
        if not re.fullmatch(r"[\w-]+", f.stem):
            continue
        cwd, title = None, ""
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in islice(fh, MAX_LINES):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    cwd = cwd or (rec.get("cwd") if isinstance(rec.get("cwd"), str) else None)
                    title = title or _prompt_text(rec)
                    if cwd and title:
                        break
        except OSError:
            continue
        if not cwd:
            continue
        out.append({"session_id": f.stem, "cwd": cwd, "project": Path(cwd).name, "title": title[:80],
                    "updated": datetime.fromtimestamp(_mtime(f), timezone.utc).isoformat(timespec="seconds"),
                    **({"live": True} if f.stem in live else {})})
        if len(out) >= limit:
            break
    return out
