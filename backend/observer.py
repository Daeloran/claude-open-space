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

from .events import INTERN_NAMES, ask_questions, is_pr_command, summarize_tool, todo_items

TAIL_BYTES = 256 * 1024  # fin du transcript lue pour la fatigue initiale
BIG_WINDOW = 1_000_000
CHAT_LIMIT = 200          # entrées renvoyées à l'ouverture du panneau de discussion
CHAT_MAX_BYTES = 4 << 20  # fin de transcript lue au plus pour cet historique
TOOL_OUTPUT_MAX = 2000    # caractères d'une sortie d'outil transmis au panneau
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


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
                    "status": str(rec.get("status") or "idle"), "project": project,
                    # Seulement en attente : ce que la session attend de toi (question, permission, dialogue)
                    **({"waiting_for": str(rec.get("waitingFor") or "")} if rec.get("status") == "waiting" else {})})
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


def _tool_output(content) -> str:
    if isinstance(content, list):
        content = "\n".join((b.get("text") or "") if b.get("type") == "text" else f"[{b.get('type')}]"
                            for b in content if isinstance(b, dict))
    text = content if isinstance(content, str) else ""
    if len(text) > TOOL_OUTPUT_MAX:
        text = text[:TOOL_OUTPUT_MAX] + f"\n… (sortie tronquée, {len(text)} caractères au total)"
    return text


def chat_entries(records: list[dict], sidechain: bool = False) -> list[dict]:
    """Enregistrements de transcript → entrées du panneau de discussion (conversation principale seulement,
    sauf `sidechain` : transcript d'un sous-agent, dont tout est « sidechain »)."""
    out = []
    for rec in records:
        msg = rec.get("message") if isinstance(rec, dict) else None
        if (not isinstance(msg, dict) or rec.get("type") not in ("user", "assistant")
                or rec.get("isMeta") or (rec.get("isSidechain") and not sidechain)):
            continue
        role, ts, content = rec["type"], rec.get("timestamp"), msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for b in blocks if isinstance(blocks, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                text = (REMINDER.sub("", b["text"]) if role == "user" else b["text"]).strip()
                if text:
                    out.append({"role": role, "kind": "text", "text": text, "ts": ts})
            elif b.get("type") == "tool_use" and role == "assistant" and isinstance(name := b.get("name"), str):
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                out.append({"role": role, "kind": "tool_use", "tool": name, "id": str(b.get("id")),
                            "summary": summarize_tool(name, inp) or name, "ts": ts,
                            **({"questions": ask_questions(inp)} if name == "AskUserQuestion" else {})})
            elif b.get("type") == "tool_result" and role == "user":
                out.append({"role": role, "kind": "tool_result", "id": str(b.get("tool_use_id")),
                            "text": _tool_output(b.get("content")), "ok": not b.get("is_error"), "ts": ts})
    return out


def _waiting(s: dict) -> dict:
    return {"waiting_for": s["waiting_for"]} if "waiting_for" in s else {}


def subagent_file(transcript: Path, tool_use_id: str) -> Path | None:
    """Transcript du sous-agent lancé par `tool_use_id` : `<session>/subagents/agent-*.jsonl`, repéré par son .meta.json."""
    for meta in (transcript.parent / transcript.stem / "subagents").glob("agent-*.meta.json"):
        try:
            if json.loads(meta.read_text(encoding="utf-8")).get("toolUseId") == tool_use_id:
                return meta.with_name(meta.name.removesuffix(".meta.json") + ".jsonl")
        except (OSError, ValueError, AttributeError):
            continue
    return None


def intern_updates(transcript: Path | None, interns: dict[str, str], tails: dict, tools: bool) -> list[dict]:
    """Activité des stagiaires (tool_use_id -> id) : entrées de panneau et, si `tools`, leurs outils (animation).
    Fichier du sous-agent lu depuis le début dès qu'il apparaît ; suivi abandonné quand le stagiaire part."""
    out: list[dict] = []
    for tuid, sid in interns.items():
        if sid not in tails:
            if not transcript or not (f := subagent_file(transcript, tuid)):
                continue
            tails[sid] = TranscriptTail(f)
        recs = tails[sid].read_new()
        out.extend({"type": "chat_entry", "agent_id": sid, "entry": e} for e in chat_entries(recs, sidechain=True))
        for rec in recs if tools else []:
            content = (rec.get("message") or {}).get("content") if rec.get("type") == "assistant" else None
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(name := b.get("name"), str):
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    out.append({"type": "tool_use", "agent_id": sid, "tool": name,
                                "summary": summarize_tool(name, inp) or name})
    for sid in set(tails) - set(interns.values()):
        del tails[sid]
    return out


def chat_history(path: Path, limit: int = CHAT_LIMIT, max_bytes: int = CHAT_MAX_BYTES,
                 chunk: int = 64 * 1024, sidechain: bool = False) -> list[dict]:
    """`limit` dernières entrées, lues depuis la fin (fenêtre doublée jusqu'à assez d'entrées). OSError propagée."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        n = min(chunk, size, max_bytes)
        while True:
            f.seek(size - n)
            lines = f.read(n).split(b"\n")
            if n < size:
                lines = lines[1:]  # première ligne coupée
            entries = chat_entries([r for r in map(_parse, lines) if r is not None], sidechain)
            # ponytail: plafond 4 Mo, une session aux énormes sorties d'outils peut montrer moins de 200 entrées
            if len(entries) >= limit or n >= size or n >= max_bytes:
                return entries[-limit:]
            n = min(n * 2, size, max_bytes)


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
            out.extend({"type": "subagent_done", "agent_id": sid} for sid in self.watched.pop(aid)["interns"].values())
            out.append({"type": "observed_left", "agent_id": aid})
        for aid, s in live.items():
            w = self.watched.get(aid)
            if w is None:
                w = self.watched[aid] = {"status": s["status"], "sid": s["session_id"], "pid": s["pid"],
                                         "waiting_for": s.get("waiting_for"), "tail": None, "tools": {}, "prs": set(), "interns": {}, "intern_seq": 0,
                                         # ponytail: réglages lus à l'arrivée seulement ; un changement en cours
                                         # de session compte au prochain démarrage de la session / de l'Open Space
                                         "window": context_window_for(s["cwd"], self.config_dir, self.window)}
                out.append({"type": "observed_joined", "agent": {
                    "id": aid, "name": s["name"], "cwd": s["cwd"], "project": s["project"],
                    "status": s["status"], **_waiting(s), "observed": True}})
                if path := self._transcript(s["session_id"]):
                    # Session déjà en cours : pas de rejeu de l'historique, seulement sa fatigue actuelle
                    w["tail"] = TranscriptTail(path, from_end=True)
                    w["tail"].read_new()  # fixe l'offset avant de lire la fin : rien ne se perd entre les deux
                    if (msg := _last_assistant_usage(path)) and (r := self._ratio(msg, w["window"])) is not None:
                        out.append({"type": "context", "agent_id": aid, "ratio": r})
                continue
            if (s["status"], s.get("waiting_for")) != (w["status"], w.get("waiting_for")):
                w["status"], w["waiting_for"] = s["status"], s.get("waiting_for")
                out.append({"type": "observed_status", "agent_id": aid, "status": s["status"], **_waiting(s)})
            if w["tail"] is None:
                if not (path := self._transcript(w["sid"])):
                    continue
                w["tail"] = TranscriptTail(path)  # transcript créé après l'arrivée : tout est nouveau
            for rec in w["tail"].read_new():
                out.extend(self._events(aid, w, rec))
                # Contenu de conversation : l'émetteur (Hub) ne le transmet qu'aux panneaux ouverts, jamais à tous
                out.extend({"type": "chat_entry", "agent_id": aid, "entry": e} for e in chat_entries([rec]))
            out.extend(intern_updates(w["tail"].path, w["interns"], w.setdefault("intern_tails", {}), tools=True))
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
                    if is_pr_command(name, b.get("input") if isinstance(b.get("input"), dict) else {}):
                        w["prs"].add(str(b.get("id")))
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    out.append({"type": "tool_use", "agent_id": aid, "tool": name,
                                "summary": summarize_tool(name, inp) or name})
                    if name == "TodoWrite":
                        out.append({"type": "todos", "agent_id": aid, "todos": todo_items(inp)})
                    if name in ("Task", "Agent") and not rec.get("isSidechain"):  # sous-agent : un stagiaire
                        w["intern_seq"] += 1
                        sid = w["interns"][str(b.get("id"))] = f"{aid}-s{w['intern_seq']}"
                        out.append({"type": "subagent_spawned", "parent_id": aid, "task": summarize_tool(name, inp),
                                    "agent": {"id": sid, "name": INTERN_NAMES[(w["intern_seq"] - 1) % len(INTERN_NAMES)]}})
            if not rec.get("isSidechain") and (r := self._ratio(msg, w["window"])) is not None:
                out.append({"type": "context", "agent_id": aid, "ratio": r})
            if not rec.get("isSidechain") and msg.get("stop_reason") == "end_turn":  # fin de réponse
                out.append({"type": "observed_turn_end", "agent_id": aid, "at": str(rec.get("timestamp") or "")})
        elif rec.get("type") == "user":
            for b in blocks:
                if b.get("type") == "tool_result":
                    out.append({"type": "tool_result", "agent_id": aid,
                                "tool": w["tools"].pop(str(b.get("tool_use_id")), "?"),
                                "ok": not b.get("is_error"),
                                **({"pr": True} if str(b.get("tool_use_id")) in w["prs"] else {})})
                    w["prs"].discard(str(b.get("tool_use_id")))
                    if sid := w["interns"].pop(str(b.get("tool_use_id")), None):
                        out.append({"type": "subagent_done", "agent_id": sid})
        return out
