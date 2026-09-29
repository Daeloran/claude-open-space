"""Tests d'intention pour l'issue #76 : liste de l'équipe dans le panneau de discussion.

Boîte noire : la page servie par `/` est exécutée dans jsdom (Node).
- Mode live : `window.WebSocket` est remplacé par un faux qui enregistre les messages
  envoyés (`send`) et laisse le test pousser les messages serveur (`hello`, `agent_hired`,
  `observed_joined`, `agent_done`, `agent_left`).
- Mode démo (`?demo`) : transport factice de la page, on n'observe que le DOM.

Contrat supposé (issue #76 et tests existants) :
- la liste d'équipe `#team` a un `li` par employé, prénom = premier texte de `.name` (#72) ;
  cliquer un de ces `li` ouvre la discussion (`#chat`, nom dans `#chatName`) (#25) ;
- `#chat` contient `#chatTeam`, un `li` par employé présent, dans l'ordre de `#team` ;
  l'entrée de la discussion ouverte porte `aria-current="true"`, elle seule ;
- clic (ou Entrée) sur une autre entrée : `close_chat` (ancien) puis `open_chat` (nouveau) ;
- un employé qui part (`agent_left`, éventuellement après avoir marché jusqu'à la porte)
  disparaît de `#chatTeam` ;
- pas de barre de fatigue ni de bouton « Congédier » dans `#chatTeam` ;
- un employé fini (`agent_done`, non lu) est signalé : son entrée change.

jsdom : cherché via `OPENSPACE_JSDOM_PATH` (dossier `node_modules` ou préfixe npm
contenant `node_modules/jsdom`), sinon via la résolution Node par défaut.
Skip si node ou jsdom sont introuvables.
"""
import functools
import json
import subprocess

from fastapi.testclient import TestClient

from backend.app import app
from tests.spec.test_issue18_clock import _node_env

A, B, C, D = "Arthas-76", "Brann-76", "Cairne-76", "Durotan-76"

NODE_SCRIPT = r"""
const { JSDOM, VirtualConsole } = require("jsdom");
const html = require("fs").readFileSync(0, "utf8");
const mode = process.argv[1];
const vc = new VirtualConsole();
vc.on("jsdomError", e => process.stderr.write("jsdomError: " + (e.stack || e) + "\n"));
const sent = [];
let sock = null;
const dom = new JSDOM(html, {
  url: "http://127.0.0.1:8000/" + (mode === "demo" ? "?demo" : ""),
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(win) {
    const noop = new Proxy(function () {}, {
      get: (t, k) => (k === Symbol.toPrimitive ? () => 0 : k === "then" ? undefined : noop),
      apply: () => noop,
      set: () => true,
    });
    win.HTMLCanvasElement.prototype.getContext = () => noop;
    const D = win.HTMLDialogElement && win.HTMLDialogElement.prototype;
    if (D) {
      if (!D.showModal) D.showModal = function () { this.setAttribute("open", ""); };
      if (!D.show) D.show = function () { this.setAttribute("open", ""); };
      if (!D.close) D.close = function () { this.removeAttribute("open"); this.dispatchEvent(new win.Event("close")); };
    }
    class FakeWS {
      constructor(url) {
        this.url = url; this.readyState = 0; this.ls = {};
        sock = this;
        setTimeout(() => { this.readyState = 1; this._fire("open", new win.Event("open")); }, 10);
      }
      addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); }
      removeEventListener(t, f) { this.ls[t] = (this.ls[t] || []).filter(g => g !== f); }
      _fire(t, ev) { if (this["on" + t]) this["on" + t](ev); (this.ls[t] || []).forEach(f => f(ev)); }
      send(d) { try { sent.push(JSON.parse(d)); } catch (e) { sent.push({ raw: String(d) }); } }
      close() { this.readyState = 3; }
    }
    FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
    win.WebSocket = FakeWS;
  },
});
const w = dom.window, d = w.document;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const push = m => sock && sock._fire("message", new w.MessageEvent("message", { data: JSON.stringify(m) }));
async function until(f, ms) { const t = Date.now() + ms; while (!f() && Date.now() < t) await sleep(50); return f(); }

const teamLis = () => [...d.querySelectorAll("#team li")].filter(li => li.querySelector(".name"));
const teamName = li => { const n = li.querySelector(".name"); return (n.firstChild ? n.firstChild.textContent : n.textContent).trim(); };
const teamNames = () => teamLis().map(teamName);
const chatName = () => { const e = d.querySelector("#chatName"); return e ? e.textContent.trim() : null; };
const chatTeam = () => d.querySelector("#chat #chatTeam");
const entries = () => { const l = chatTeam(); return l ? [...l.querySelectorAll("li")] : []; };
let known = [];
const entryName = li => known.filter(n => li.textContent.includes(n)).sort((a, b) => b.length - a.length)[0] || null;
const entry = name => entries().find(li => entryName(li) === name) || null;
function snap() {
  known = [...new Set([...known, ...teamNames()])];
  const l = chatTeam();
  return {
    chatTeamExists: !!l,
    chatTeamInChat: !!(l && l.closest("#chat")),
    team: teamNames(),
    chatName: chatName(),
    entries: entries().map(li => ({
      name: entryName(li),
      current: li.getAttribute("aria-current"),
      text: li.textContent,
      html: li.outerHTML,
    })),
  };
}
const hasName = n => () => (chatName() || "").includes(n);

(async () => {
  await sleep(300);
  const out = { steps: {} };
  if (mode !== "demo") {
    push({ type: "hello", projects: [{ name: "proj76", cwd: "/tmp/proj76" }], team: [] });
    await sleep(100);
    [["a-76", "Arthas-76"], ["b-76", "Brann-76"], ["c-76", "Cairne-76"]].forEach(([id, name]) =>
      push({ type: "agent_hired", agent: { id, name, cwd: "/tmp/proj76", project: "proj76" } }));
    push({ type: "observed_joined", agent: { id: "o-d-76", name: "Durotan-76", cwd: "/tmp/proj76",
      project: "proj76", status: "idle", observed: true } });
  }
  await sleep(mode === "demo" ? 1500 : 500);
  known = teamNames();
  out.steps.initial = snap();
  const first = known[0], second = known[1];
  const firstLi = teamLis().find(li => teamName(li) === first);
  if (firstLi) firstLi.click();
  await until(hasName(first), 5000);
  await sleep(200);
  out.steps.opened = snap();

  if (mode !== "demo") {
    // signal « a fini » sur un employé dont la discussion n'est pas ouverte
    const c = () => entry("Cairne-76");
    out.doneBefore = c() ? c().outerHTML : null;
    push({ type: "agent_done", agent_id: "c-76" });
    await sleep(400);
    out.doneAfter = c() ? c().outerHTML : null;
  }

  // clic sur une autre entrée
  const mark = sent.length;
  const e2 = entry(second);
  if (e2) e2.click();
  await until(hasName(second), 5000);
  await sleep(200);
  out.sentAfterClick = sent.slice(mark);
  out.steps.switched = snap();

  if (mode !== "demo") {
    // touche Entrée sur une autre entrée
    const e3 = entry("Durotan-76");
    let target = null;
    if (e3) target = e3.matches("[tabindex],[role]") ? e3 : (e3.querySelector("button,a[href],[tabindex]") || e3);
    out.enterTargetIsNativeButton = !!(target && target.tagName === "BUTTON");
    const mark2 = sent.length;
    if (target) {
      target.focus && target.focus();
      target.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true, cancelable: true }));
      target.dispatchEvent(new w.KeyboardEvent("keyup", { key: "Enter", code: "Enter", bubbles: true, cancelable: true }));
    }
    await until(hasName("Durotan-76"), 3000);
    await sleep(200);
    out.sentAfterEnter = sent.slice(mark2);
    out.steps.enter = snap();

    // départ d'un employé (animation possible jusqu'à la porte)
    push({ type: "agent_left", agent_id: "c-76" });
    await until(() => !teamNames().includes("Cairne-76") && !entry("Cairne-76"), 15000);
    out.steps.left = snap();
  }
  const res = "@@RES@@" + JSON.stringify(out) + "\n";
  w.close();
  process.stdout.write(res, () => process.exit(0));
})().catch(e => { process.stderr.write(String(e.stack || e)); process.exit(1); });
"""


@functools.lru_cache(maxsize=None)
def _run(mode):
    node, env = _node_env()
    r = TestClient(app).get("/")  # pas de `with` : le lifespan ne démarre pas
    assert r.status_code == 200
    out = subprocess.run([node, "-e", NODE_SCRIPT, mode], input=r.text, env=env,
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.rsplit("@@RES@@", 1)[1])


def _names(step):
    return [e["name"] for e in step["entries"]]


def _current(step):
    return [e["name"] for e in step["entries"] if e["current"] == "true"]


def _assert_lists_team(step):
    assert step["chatTeamExists"] and step["chatTeamInChat"], "#chatTeam absent de #chat"
    assert len(step["team"]) >= 2, step["team"]
    assert _names(step) == step["team"], (_names(step), step["team"])


def _index(msgs, type_, agent_id):
    return next((i for i, m in enumerate(msgs) if m.get("type") == type_ and m.get("agent_id") == agent_id), None)


# ---------------------------------------------------------------- mode démo


def test_demo_chat_team_liste_tous_les_employes_dans_l_ordre():
    step = _run("demo")["steps"]["opened"]
    _assert_lists_team(step)


def test_demo_entree_active_seule_aria_current():
    step = _run("demo")["steps"]["opened"]
    assert step["team"] and step["team"][0] in (step["chatName"] or ""), step
    assert _current(step) == [step["team"][0]], step["entries"]


def test_demo_clic_autre_entree_change_de_discussion():
    res = _run("demo")
    before, step = res["steps"]["opened"], res["steps"]["switched"]
    second = before["team"][1] if len(before["team"]) > 1 else None
    assert second and second in (step["chatName"] or ""), (second, step["chatName"])
    assert _current(step) == [second], step["entries"]


def test_demo_pas_de_fatigue_ni_de_conge():
    _assert_no_fatigue_nor_dismiss(_run("demo")["steps"]["opened"])


# ---------------------------------------------------------------- mode live (faux WebSocket)


def test_live_chat_team_liste_tous_les_employes_dans_l_ordre():
    step = _run("live")["steps"]["opened"]
    _assert_lists_team(step)
    assert set(step["team"]) >= {A, B, C, D}, step["team"]


def test_live_entree_active_seule_aria_current():
    step = _run("live")["steps"]["opened"]
    assert A in (step["chatName"] or ""), step["chatName"]
    assert _current(step) == [A], step["entries"]


def test_live_clic_envoie_close_chat_puis_open_chat():
    res = _run("live")
    step, msgs = res["steps"]["switched"], res["sentAfterClick"]
    assert B in (step["chatName"] or ""), step["chatName"]
    assert _current(step) == [B], step["entries"]
    i_close, i_open = _index(msgs, "close_chat", "a-76"), _index(msgs, "open_chat", "b-76")
    assert i_close is not None and i_open is not None and i_close < i_open, msgs


def test_live_touche_entree_change_de_discussion():
    res = _run("live")
    step, msgs = res["steps"]["enter"], res["sentAfterEnter"]
    if res["enterTargetIsNativeButton"]:
        # un vrai <button> est activé nativement par Entrée (jsdom ne le simule pas)
        assert step["chatTeamExists"], "#chatTeam absent de #chat"
        return
    assert D in (step["chatName"] or ""), step["chatName"]
    assert _current(step) == [D], step["entries"]
    i_close, i_open = _index(msgs, "close_chat", "b-76"), _index(msgs, "open_chat", "o-d-76")
    assert i_close is not None and i_open is not None and i_close < i_open, msgs


def test_live_employe_qui_part_disparait_de_la_liste():
    res = _run("live")
    before, after = res["steps"]["enter"], res["steps"]["left"]
    assert C in _names(before), _names(before)
    assert C not in after["team"], "l'employé n'a jamais quitté #team"
    assert C not in _names(after), _names(after)
    assert _names(after) == after["team"], (_names(after), after["team"])


def test_live_employe_fini_signale():
    res = _run("live")
    assert res["doneBefore"] is not None, "entrée de Cairne-76 absente de #chatTeam"
    assert res["doneAfter"] is not None
    assert res["doneAfter"] != res["doneBefore"], "agent_done ne change pas l'entrée"


def test_live_pas_de_fatigue_ni_de_conge():
    _assert_no_fatigue_nor_dismiss(_run("live")["steps"]["opened"])


def _assert_no_fatigue_nor_dismiss(step):
    assert step["chatTeamExists"] and step["entries"], "#chatTeam vide ou absent"
    for e in step["entries"]:
        low = e["html"].lower()
        assert "fatigue" not in low, e["html"]
        assert "congé" not in low and "conge" not in low, e["html"]
        assert "<progress" not in low and "<meter" not in low, e["html"]
