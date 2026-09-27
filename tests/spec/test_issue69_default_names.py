"""Tests d'intention pour l'issue #69 : nouveaux prénoms par défaut des recrues.

Contrat supposé :
- sans OPENSPACE_TEAM, `backend.app.TEAM` vaut
  Jaina, Khadgar, Rhonin, Modera, Antonidas, Aethas, Kalec, Ansirem (dans cet ordre) ;
- `backend.app.recruit_name(n)` donne le n-ième prénom (n à partir de 0), puis,
  une fois la liste épuisée, le prénom suffixé ("Jaina 2") ;
- OPENSPACE_TEAM (liste séparée par des virgules) remplace toujours la liste.
"""

import importlib

import pytest

import backend.app

DEFAULT = ["Jaina", "Khadgar", "Rhonin", "Modera", "Antonidas", "Aethas", "Kalec", "Ansirem"]


@pytest.fixture
def reload_app(monkeypatch):
    # reload remplace hub, app, employees… : on restaure les objets d'origine,
    # que d'autres tests ont importés par référence
    saved = dict(vars(backend.app))

    def _reload(team=None):
        if team is None:
            monkeypatch.delenv("OPENSPACE_TEAM", raising=False)
        else:
            monkeypatch.setenv("OPENSPACE_TEAM", team)
        importlib.reload(backend.app)
        return backend.app

    yield _reload
    vars(backend.app).update(saved)


def test_default_list_is_warcraft_mages_in_order(reload_app):
    app = reload_app()
    assert [app.recruit_name(n) for n in range(len(DEFAULT))] == DEFAULT


def test_default_first_recruits_are_jaina_khadgar_rhonin(reload_app):
    app = reload_app()
    assert [app.recruit_name(n) for n in range(3)] == ["Jaina", "Khadgar", "Rhonin"]


def test_suffix_once_default_list_exhausted(reload_app):
    app = reload_app()
    n = len(DEFAULT)
    assert app.recruit_name(n) == "Jaina 2"
    assert app.recruit_name(n + 1) == "Khadgar 2"


def test_openspace_team_still_overrides(reload_app):
    app = reload_app("Alice,Bob")
    assert [app.recruit_name(n) for n in range(3)] == ["Alice", "Bob", "Alice 2"]
