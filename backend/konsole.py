"""Tape un prompt dans l'onglet Konsole d'une session Claude Code du terminal (D-Bus).

Onglet retrouvé par `KONSOLE_DBUS_SERVICE` / `KONSOLE_DBUS_SESSION` dans `/proc/<pid>/environ` (seules
ces deux clés sont gardées). Garde-fou : rien n'est envoyé si le processus au premier plan de l'onglet
n'est pas la session Claude elle-même (jamais de texte tapé dans un shell).

Appels D-Bus : `gdbus call` en sous-processus (liste d'arguments, jamais de shell), `busctl --user call`
à défaut. `gdbus` lit chaque argument comme du texte GVariant : une chaîne nue pourrait être réinterprétée
(`'x'` perd ses guillemets, `@s "x"`…), elle est donc toujours passée entre guillemets doubles, échappée
par `gvariant_str`. `busctl` prend les chaînes telles quelles (signature `s`), après `--`.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import unicodedata
from pathlib import Path

PROC_ROOT = Path("/proc")
TIMEOUT = 3.0
KEYS = {b"KONSOLE_DBUS_SERVICE", b"KONSOLE_DBUS_SESSION"}
FG = "org.kde.konsole.Session.foregroundProcessId"
SEND = "org.kde.konsole.Session.sendText"
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"


def locate(pid: int) -> tuple[str, str] | None:
    """(service, chemin de session) de l'onglet Konsole du processus, ou None. Aucune autre variable lue."""
    try:
        raw = (PROC_ROOT / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    env = {}
    for entry in raw.split(b"\0"):
        key, _, value = entry.partition(b"=")
        if key in KEYS:
            try:
                env[key] = value.decode()
            except ValueError:
                return None
    service, path = env.get(b"KONSOLE_DBUS_SERVICE"), env.get(b"KONSOLE_DBUS_SESSION")
    # Formats attendus (`:1.163`, `org.kde.konsole-1234` ; `/Sessions/2`) : jamais pris pour une option
    if not (service and path and re.fullmatch(r"[\w:.][\w:.-]*", service) and re.fullmatch(r"/[\w/]*", path)):
        return None
    return service, path


def sanitize(text: str) -> str:
    """Retire les caractères de contrôle (Unicode Cc : ESC, BEL, CR, DEL, C1…), sauf `\\n` et `\\t`."""
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")


def gvariant_str(s: str) -> str:
    """Chaîne GVariant littérale : guillemets doubles, `\\` et `"` échappés, contrôles en `\\uXXXX`."""
    out = []
    for c in s:
        if c in '\\"':
            out.append("\\" + c)
        elif unicodedata.category(c) == "Cc":
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def parse_pid(out: str) -> int | None:
    """Réponse de foregroundProcessId : `(13192,)` (gdbus) ou `i 13192` (busctl)."""
    m = re.fullmatch(r"\((\d+),\)|i (\d+)", out.strip())
    return int(m.group(1) or m.group(2)) if m else None


async def dbus_call(service: str, path: str, method: str, *args: str) -> str:
    """Un appel de méthode D-Bus (bus de session), arguments chaînes. Lève en cas d'échec ou de timeout."""
    if gdbus := shutil.which("gdbus"):
        cmd = [gdbus, "call", "--session", "--dest", service, "--object-path", path, "--method", method,
               *map(gvariant_str, args)]
    elif busctl := shutil.which("busctl"):
        iface, _, member = method.rpartition(".")
        cmd = [busctl, "--user", "call", "--", service, path, iface, member, *(["s" * len(args), *args] if args else [])]
    else:
        raise FileNotFoundError("ni gdbus ni busctl dans le PATH")
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(err.decode(errors="replace").strip()[:200])
    return out.decode(errors="replace").strip()


async def send_prompt(pid: int, text: str) -> str | None:
    """Tape `text` puis Entrée dans l'onglet Konsole de la session `pid`. None si envoyé, sinon la raison."""
    loc = locate(pid)
    if loc is None:
        return "Onglet Konsole introuvable (session lancée hors de Konsole ?)."
    try:
        if parse_pid(await dbus_call(*loc, FG)) != pid:
            return "Claude n'est pas au premier plan de son onglet Konsole : rien n'a été envoyé."
        # ponytail: pas de verrou entre la vérification et l'envoi (quelques ms) : fenêtre acceptée
        body = sanitize(text)
        if "\n" in body:  # bracketed paste : un seul message, pas une validation par ligne
            body = PASTE_START + body + PASTE_END
        await dbus_call(*loc, SEND, body)
        await dbus_call(*loc, SEND, "\r")
    except Exception as exc:  # gdbus absent, D-Bus en panne, timeout, onglet fermé…
        if "Security sensitive DBus API is disabled" in str(exc):
            return ("Konsole refuse l'envoi : active « Enable the security sensitive parts of the DBus API » "
                    "(Configurer Konsole → Général).")
        return f"Konsole injoignable par D-Bus ({type(exc).__name__})."
    return None
