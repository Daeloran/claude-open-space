"""Tests d'intention pour l'issue #18 : horloge du bandeau à l'heure réelle.

Boîte noire : la page servie par `/` est exécutée dans jsdom (Node) en mode démo,
avec une date système simulée à 14:37 locale. On lit `#sClock` et le premier
`<time>` du journal.

jsdom : cherché via `OPENSPACE_JSDOM_PATH` (dossier `node_modules` ou préfixe npm
contenant `node_modules/jsdom`), sinon via la résolution Node par défaut.
Skip si node ou jsdom sont introuvables.
"""
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
    const Real = win.Date;
    const offset = new Real(2026, 0, 15, 14, 37, 0).getTime() - Real.now();
    class FakeDate extends Real {
      constructor(...a) { a.length ? super(...a) : super(Real.now() + offset); }
      static now() { return Real.now() + offset; }
    }
    win.Date = FakeDate;
    const noop = new Proxy(function () {}, {
      get: (t, k) => (k === Symbol.toPrimitive ? () => 0 : k === "then" ? undefined : noop),
      apply: () => noop,
      set: () => true,
    });
    win.HTMLCanvasElement.prototype.getContext = () => noop;
  },
});
setTimeout(() => {
  const d = dom.window.document;
  const c = d.querySelector("#sClock");
  const t = d.querySelector("time");
  console.log(JSON.stringify({ clock: c && c.textContent.trim(), log: t && t.textContent.trim() }));
  dom.window.close();
  process.exit(0);
}, 1500);
"""


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


def _run_page():
    node, env = _node_env()
    r = TestClient(app).get("/")  # pas de `with` : le lifespan ne démarre pas
    assert r.status_code == 200
    out = subprocess.run([node, "-e", NODE_SCRIPT], input=r.text, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


REAL = {"14:37", "14:38"}


def test_banner_clock_shows_real_local_time_in_demo():
    res = _run_page()
    assert res["clock"] in REAL, res


def test_log_timestamps_use_real_local_time():
    res = _run_page()
    if res["log"] is None:
        pytest.skip("aucune ligne de journal horodatée affichée")
    assert res["log"] in REAL, res
