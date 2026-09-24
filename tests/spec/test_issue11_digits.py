"""Tests d'intention pour l'issue #11 : chiffres du bandeau en police monospace.

Boîte noire : on lit uniquement le HTML/CSS servi par `/`.
Bandeau : `.stats > .stat > span` (libellé) + `b` (valeur).
"""
import re

from fastapi.testclient import TestClient

from backend.app import app


def _page():
    r = TestClient(app).get("/")  # pas de `with` : le lifespan ne démarre pas
    assert r.status_code == 200
    return r.text


def _css(html):
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _rules(css):
    """(sélecteurs normalisés, déclarations {prop: valeur}) pour chaque règle feuille,
    y compris celles imbriquées dans une @media."""
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        sels = [re.sub(r"\s+", " ", re.sub(r"\s*>\s*", " ", s)).strip() for s in sel.split(",")]
        decls = {}
        for d in body.split(";"):
            if ":" in d:
                k, v = d.split(":", 1)
                decls[k.strip().lower()] = v.strip()
        out.append((sels, decls))
    return out


def _families(value):
    return [f.strip().strip("\"'").lower() for f in value.replace("!important", "").split(",")]


def _is_mono_first(value):
    fams = _families(value)
    return bool(fams) and fams[0] in ("ui-monospace", "monospace")


def _targets(sels, wanted):
    return any(s in wanted or any(s.endswith(" " + w) for w in wanted) for s in sels)


VALUE_SELS = {".stat b", ".stats b"}
LABEL_SELS = {".stat span", ".stats span"}


def _decls_for(wanted, prop):
    return [d[prop] for sels, d in _rules(_css(_page())) if _targets(sels, wanted) and prop in d]


def test_stat_values_use_monospace_stack():
    fams = _decls_for(VALUE_SELS, "font-family")
    assert fams, "aucune règle .stat b ne définit font-family"
    last = _families(fams[-1])
    assert last[0] == "ui-monospace", fams[-1]
    assert last[-1] == "monospace", fams[-1]
    assert "pixelify sans" not in last


def test_stat_values_use_tabular_nums():
    vals = _decls_for(VALUE_SELS, "font-variant-numeric")
    assert vals and "tabular-nums" in vals[-1]


def test_only_pixelify_is_loaded_from_network():
    html = _page()
    css = _css(html)
    assert "@font-face" not in css.lower()
    assert not re.search(r"\.woff2?\b", html, re.I)
    urls = re.findall(r"https?://fonts\.googleapis\.com/css2?\?[^\"'\s)]+", html)
    assert urls, "Pixelify Sans doit rester chargée"
    for u in urls:
        assert re.findall(r"family=([^:&;]+)", u) == ["Pixelify+Sans"], u
    assert not re.search(r"@import", css, re.I)


def test_stat_labels_stay_pixel_font():
    for v in _decls_for(LABEL_SELS, "font-family"):
        assert not _is_mono_first(v), v
    # sans règle de libellé explicite, le conteneur ne doit pas passer en monospace
    if not _decls_for(LABEL_SELS, "font-family"):
        for v in _decls_for({".stat", ".stats"}, "font-family"):
            assert not _is_mono_first(v), v


def test_title_stays_pixel_font():
    for v in _decls_for({".brand h1", ".brand", "h1"}, "font-family"):
        assert not _is_mono_first(v), v
