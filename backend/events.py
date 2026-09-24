"""Traduction des appels d'outils Claude Code en libellés lisibles pour le jeu."""
from __future__ import annotations

from pathlib import PurePath
from typing import Any

FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"}


def _short(text: Any, limit: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_path(path: str) -> str:
    parts = PurePath(path).parts
    return "/".join(parts[-2:]) if len(parts) > 2 else path


INTERN_NAMES = ["Tom", "Chloé", "Malik", "Jade", "Noé", "Zoé"]


def summarize_tool(name: str, data: dict | None) -> str:
    data = data or {}
    if name in FILE_TOOLS:
        return _short(_short_path(data.get("file_path") or data.get("notebook_path") or "un fichier"))
    if name == "Bash":
        return _short(data.get("command"), 80)
    if name in ("Grep", "Glob"):
        return _short(data.get("pattern"))
    if name == "LS":
        return _short(data.get("path", "."))
    if name == "WebSearch":
        return _short(data.get("query"))
    if name == "WebFetch":
        return _short(data.get("url"))
    if name == "TodoWrite":
        return f"{len(data.get('todos') or [])} tâche(s)"
    if name in ("Task", "Agent"):
        return _short(data.get("description") or data.get("prompt"))
    return name


def deliverable_for(name: str, data: dict | None) -> dict | None:
    data = data or {}
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = data.get("file_path") or data.get("notebook_path")
        if path:
            return {"path": _short_path(path), "kind": "write" if name == "Write" else "edit"}
    if name == "Bash" and "git commit" in str(data.get("command", "")):
        return {"path": _short(data["command"], 70), "kind": "commit"}
    return None
