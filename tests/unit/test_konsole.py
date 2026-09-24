"""backend/konsole.py : échappement GVariant, parsing du pid, construction de la commande, timeout.

Aucun sous-processus réel : create_subprocess_exec est remplacé par un faux processus.
"""
import asyncio
import json

import pytest

import backend.konsole as k


@pytest.mark.parametrize("s", ["simple", "'x'", '"q"', "@s 'y'", "b'z'", "a\\b\\u0041", "\r", "-h",
                               "\x1b[200~l1\nl2 🚀 é\t\x1b[201~", "\x7f\x9b\x00", ""])
def test_gvariant_str_aller_retour(s):
    q = k.gvariant_str(s)
    assert q.startswith('"') and q.endswith('"')
    # Échappements utilisés (\\ \" \uXXXX) = sous-ensemble commun GVariant/JSON : JSON sert d'oracle
    assert json.loads(q) == s


def test_gvariant_str_controles_en_unicode():
    assert k.gvariant_str('\x1b"\\\r') == '"\\u001b\\"\\\\\\u000d"'


@pytest.mark.parametrize("out,pid", [("(13192,)", 13192), ("(13192,)\n", 13192), ("i 13192", 13192),
                                     ("", None), ("(,)", None), ("(-1,)", None), ("(12, 3)", None),
                                     ("n'importe quoi", None)])
def test_parse_pid(out, pid):
    assert k.parse_pid(out) == pid


class FakeProc:
    def __init__(self, delay=0.0, rc=0, out=b"(1,)\n", err=b""):
        self.delay, self.returncode, self.out, self.err = delay, rc, out, err
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(self.delay)
        return self.out, self.err

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


@pytest.fixture
def spawn(monkeypatch):
    calls = []

    def install(proc, which=lambda n: f"/usr/bin/{n}"):
        async def fake_exec(*cmd, **kw):
            calls.append((cmd, kw))
            return proc
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(k.shutil, "which", which)
        return calls
    return install


def test_commande_gdbus_arguments_echappes(spawn):
    calls = spawn(FakeProc(out=b"()\n"))
    asyncio.run(k.dbus_call(":1.5", "/Sessions/2", k.SEND, "a'b\n"))
    cmd, kw = calls[0]
    assert list(cmd) == ["/usr/bin/gdbus", "call", "--session", "--dest", ":1.5", "--object-path", "/Sessions/2",
                         "--method", k.SEND, '"a\'b\\u000a"']
    assert "shell" not in kw


def test_commande_busctl_en_secours(spawn):
    calls = spawn(FakeProc(out=b"i 42\n"), which=lambda n: "/usr/bin/busctl" if n == "busctl" else None)
    assert asyncio.run(k.dbus_call(":1.5", "/Sessions/2", k.SEND, "--help")) == "i 42"
    assert list(calls[0][0]) == ["/usr/bin/busctl", "--user", "call", "--", ":1.5", "/Sessions/2",
                                 "org.kde.konsole.Session", "sendText", "s", "--help"]


def test_sans_outil_d_bus(spawn):
    spawn(FakeProc(), which=lambda n: None)
    with pytest.raises(FileNotFoundError):
        asyncio.run(k.dbus_call(":1.5", "/Sessions/2", k.FG))


def test_timeout_tue_le_processus(spawn, monkeypatch):
    proc = FakeProc(delay=5)
    spawn(proc)
    monkeypatch.setattr(k, "TIMEOUT", 0.05)
    with pytest.raises(TimeoutError):
        asyncio.run(k.dbus_call(":1.5", "/Sessions/2", k.FG))
    assert proc.killed


def test_code_retour_non_nul_leve(spawn):
    spawn(FakeProc(rc=1, err=b"GDBus.Error:ServiceUnknown"))
    with pytest.raises(RuntimeError, match="ServiceUnknown"):
        asyncio.run(k.dbus_call(":1.5", "/Sessions/2", k.FG))


def test_timeout_devient_une_raison(tmp_path, spawn, monkeypatch):
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "environ").write_bytes(b"KONSOLE_DBUS_SERVICE=:1.5\0KONSOLE_DBUS_SESSION=/Sessions/2\0")
    monkeypatch.setattr(k, "PROC_ROOT", tmp_path)
    monkeypatch.setattr(k, "TIMEOUT", 0.05)
    spawn(FakeProc(delay=5))
    assert "TimeoutError" in asyncio.run(k.send_prompt(7, "salut"))


@pytest.mark.parametrize("service,path", [("-x", "/Sessions/2"), (":1.5", "--help"), (":1.5", "/a b")])
def test_locate_refuse_les_valeurs_hors_format(tmp_path, monkeypatch, service, path):
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "environ").write_bytes(
        f"KONSOLE_DBUS_SERVICE={service}\0KONSOLE_DBUS_SESSION={path}\0".encode())
    monkeypatch.setattr(k, "PROC_ROOT", tmp_path)
    assert k.locate(7) is None


def test_api_sensible_desactivee_donne_une_raison_explicite(monkeypatch):
    monkeypatch.setattr(k, "locate", lambda pid: (":1.5", "/Sessions/1"))
    sent = []

    async def fake(service, path, method, *args):
        if method.endswith("foregroundProcessId"):
            return "(42,)"
        sent.append(args)
        raise RuntimeError("GDBus.Error:org.freedesktop.DBus.Error.Failed: Security sensitive DBus API is disabled in the settings.")

    monkeypatch.setattr(k, "dbus_call", fake)
    reason = asyncio.run(k.send_prompt(42, "salut"))
    assert "security sensitive" in reason.lower() and "Configurer Konsole" in reason
    assert len(sent) == 1  # rien d'autre tenté après le refus
