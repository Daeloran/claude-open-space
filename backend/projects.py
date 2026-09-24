"""Projets proposés au recrutement : dossiers de travail récents des sessions Claude Code."""
from __future__ import annotations

import json
import os
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
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        if os.path.isdir(cwd):
            out.append({"cwd": cwd, "name": Path(cwd).name})
            if len(out) >= limit:
                break
    return out[:limit]
