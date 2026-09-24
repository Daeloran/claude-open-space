"""Tests de logique de origin_allowed (#1)."""
import pytest

from backend.app import origin_allowed


@pytest.fixture(autouse=True)
def no_extra_origins(monkeypatch):
    monkeypatch.delenv("OPENSPACE_ALLOWED_ORIGINS", raising=False)


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost"])
def test_loopback_same_origin_allowed(host):
    assert origin_allowed(f"http://{host}", host)


def test_dns_rebinding_refused():
    # evil.com rebindé sur 127.0.0.1 : Origin et Host concordent mais le nom n'est pas loopback
    assert not origin_allowed("http://evil.com:8000", "evil.com:8000")


@pytest.mark.parametrize("origin, host", [
    (None, "127.0.0.1:8000"),
    ("", "127.0.0.1:8000"),
    ("null", "127.0.0.1:8000"),
    ("http://127.0.0.1:8000", None),
    ("https://127.0.0.1:8000", "127.0.0.1:8000"),   # schéma différent
    ("http://localhost:8000", "127.0.0.1:8000"),    # host différent
    ("http://127.0.0.1:8000.evil.com", "127.0.0.1:8000"),
])
def test_mismatch_refused(origin, host):
    assert not origin_allowed(origin, host)


def test_env_origins_parsed_with_spaces_and_empty_entries(monkeypatch):
    monkeypatch.setenv("OPENSPACE_ALLOWED_ORIGINS", " http://a.local:3000 ,, http://b.local ")
    assert origin_allowed("http://a.local:3000", "whatever")
    assert origin_allowed("http://b.local", None)
    assert not origin_allowed("", "whatever")
    assert not origin_allowed("http://c.local", "c.local")
