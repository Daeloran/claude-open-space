"""Tests d'intention pour l'issue #72 : skin féminin (cheveux longs) des employées.

Boîte noire : la page servie par `/` est exécutée dans jsdom (Node) en mode démo.
Le canvas est remplacé par un faux contexte 2D qui enregistre chaque `fillRect`
(avec le `fillStyle` courant). On lit les portraits de la liste d'équipe
(`li` > `canvas` 16x20, prénom = premier texte de `.name`).

Pour comparer une employée et un employé à la même place (même palette), on
renomme l'employé de démo « Hugo » (littéral `"Hugo"` de la page) avant exécution.

Contrat supposé :
- Jaina, Modera, Chloé, Jade, Zoé sont féminins, « Jaina 2 » aussi ;
- un portrait féminin = le portrait masculin + des rects de cheveux (couleur du
  haut du crâne) de part et d'autre de la tête, sur plusieurs pixels de haut ;
- un portrait masculin est identique à avant (géométrie de référence ci-dessous,
  relevée sur `main` avant le changement).

jsdom : cherché via `OPENSPACE_JSDOM_PATH` (dossier `node_modules` ou préfixe npm
contenant `node_modules/jsdom`), sinon via la résolution Node par défaut.
Skip si node ou jsdom sont introuvables.
"""
import functools
import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.app import app

NODE_SCRIPT = r"""
const { JSDOM, VirtualConsole } = require("jsdom");
const html = require("fs").readFileSync(0, "utf8");
const vc = new VirtualConsole();
vc.on("jsdomError", e => process.stderr.write("jsdomError: " + (e.stack || e) + "\n"));
const dom = new JSDOM(html, {
  url: "http://127.0.0.1:8000/?demo",
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(win) {
    win.HTMLCanvasElement.prototype.getContext = function () {
      if (this.__ctx) return this.__ctx;
      const rects = (this.__rects = []);
      const noop = () => {};
      const img = (w, h) => ({ width: w || 0, height: h || 0,
                               data: new Uint8ClampedArray((w || 0) * (h || 0) * 4) });
      const base = {
        fillStyle: "", canvas: this,
        fillRect(x, y, w, h) { rects.push([String(this.fillStyle), x, y, w, h]); },
        measureText: () => ({ width: 0 }),
        getImageData: (x, y, w, h) => img(w, h),
        createImageData: (w, h) => img(w, h),
        drawImage: noop,
      };
      return (this.__ctx = new Proxy(base, {
        get: (t, k) => (k in t ? t[k] : noop),
        set: (t, k, v) => ((t[k] = v), true),
      }));
    };
  },
});
setTimeout(() => {
  const out = {};
  for (const li of dom.window.document.querySelectorAll("li")) {
    const c = li.querySelector("canvas"), n = li.querySelector(".name");
    if (!c || !n || !n.firstChild) continue;
    out[n.firstChild.textContent.trim()] = c.__rects || [];
  }
  console.log(JSON.stringify(out));
  dom.window.close();
  process.exit(0);
}, 1500);
"""

# Géométrie (x, y, w, h) du portrait masculin avant #72.
MALE_BEFORE = {
    (4, 10, 1, 3), (4, 13, 1, 1), (4, 17, 8, 2), (5, 3, 6, 2), (5, 4, 6, 5),
    (5, 5, 1, 2), (5, 9, 6, 5), (5, 14, 2, 4), (5, 17, 2, 1), (6, 6, 1, 1),
    (9, 6, 1, 1), (9, 14, 2, 3), (9, 16, 2, 1), (10, 5, 1, 2), (11, 10, 1, 3),
    (11, 13, 1, 1),
}
FEMALE = ["Jaina", "Modera", "Chloé", "Jade", "Zoé"]
MALE = ["Hugo", "Khadgar", "Rhonin", "Tom", "Malik"]


def _node_env():
    node = shutil.which("node")
    if not node:
        pytest.skip("node introuvable")
    env = dict(os.environ)
    p = os.environ.get("OPENSPACE_JSDOM_PATH")
    if p:
        env["NODE_PATH"] = os.pathsep.join([p, os.path.join(p, "node_modules"), env.get("NODE_PATH", "")])
    ok = subprocess.run([node, "-e", "require.resolve('jsdom')"], env=env, capture_output=True)
    if ok.returncode != 0:
        pytest.skip("jsdom introuvable (définir OPENSPACE_JSDOM_PATH)")
    return node, env


@functools.lru_cache(maxsize=None)
def _portrait(name):
    """Rects [(style, x, y, w, h)] du portrait d'équipe de l'employé de démo « Hugo » renommé `name`."""
    node, env = _node_env()
    html = TestClient(app).get("/").text  # pas de `with` : le lifespan ne démarre pas
    assert html.count('"Hugo"') == 1, "employé de démo « Hugo » introuvable"
    html = html.replace('"Hugo"', json.dumps(name, ensure_ascii=False))
    out = subprocess.run([node, "-e", NODE_SCRIPT], input=html, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    team = json.loads(out.stdout.strip().splitlines()[-1])
    assert name in team, f"{name!r} absent de la liste d'équipe : {list(team)}"
    rects = [tuple(r) for r in team[name]]
    assert rects, f"aucun fillRect pour le portrait de {name!r}"
    return rects


def _geom(rects):
    return {r[1:] for r in rects}


@pytest.mark.parametrize("name", FEMALE)
def test_female_portrait_adds_long_hair_on_both_sides(name):
    male, fem = _geom(_portrait("Hugo")), _geom(_portrait(name))
    assert male < fem, f"{name} devrait être le sprite masculin + des mèches"
    extra = fem - male
    left = [r for r in extra if r[0] + r[2] <= 8]
    right = [r for r in extra if r[0] >= 8]
    assert left and right, f"mèches attendues des deux côtés de la tête : {sorted(extra)}"
    for side in (left, right):
        rows = {y for (_, y, _, h) in side for y in range(y, y + h)}
        assert len(rows) >= 3, f"mèches trop courtes (jusqu'aux épaules attendu) : {sorted(side)}"
    # couleur des mèches = couleur du haut du crâne
    rects = _portrait(name)
    top = min(r[2] for r in rects)
    hair = {r[0] for r in rects if r[2] == top}
    assert {r[0] for r in rects if r[1:] in extra} <= hair


def test_suffixed_name_stays_female():
    assert _geom(_portrait("Jaina 2")) == _geom(_portrait("Jaina"))
    assert _geom(_portrait("Jaina 2")) != _geom(_portrait("Hugo"))


@pytest.mark.parametrize("name", MALE)
def test_male_portrait_unchanged(name):
    assert _geom(_portrait(name)) == MALE_BEFORE
