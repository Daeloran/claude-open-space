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
    if name == "AskUserQuestion" and isinstance(qs := data.get("questions"), list) and qs and isinstance(qs[0], dict):
        return _short(qs[0].get("question"), 80)
    return name


def ask_questions(data: dict | None) -> list[dict]:
    """Questions d'un AskUserQuestion : texte, titre, choix multiple, options (libellé, description) ; malformé → ignoré."""
    qs = (data or {}).get("questions")
    return [{"question": str(q.get("question") or ""), "header": str(q.get("header") or ""),
             "multi": bool(q.get("multiSelect")),
             "options": [{"label": str(o.get("label") or ""), "description": str(o.get("description") or "")}
                         for o in (q.get("options") or []) if isinstance(o, dict)]}
            for q in qs if isinstance(q, dict)] if isinstance(qs, list) else []


def todo_items(data: dict | None) -> list[dict]:
    """Liste d'un TodoWrite : `content` et `status` des éléments valides seulement."""
    todos = (data or {}).get("todos")
    return [{"content": t["content"], "status": t["status"]} for t in (todos if isinstance(todos, list) else [])
            if isinstance(t, dict) and isinstance(t.get("content"), str) and isinstance(t.get("status"), str)]


def deliverable_for(name: str, data: dict | None) -> dict | None:
    data = data or {}
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = data.get("file_path") or data.get("notebook_path")
        if path:
            return {"path": _short_path(path), "kind": "write" if name == "Write" else "edit"}
    if name == "Bash" and "git commit" in str(data.get("command", "")):
        return {"path": _short(data["command"], 70), "kind": "commit"}
    return None
