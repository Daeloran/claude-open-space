"""Tests d'intention pour l'issue #73 : reprendre une conversation dès sa sélection, sans ticket.

Boîte noire : la page servie par `/` est exécutée dans jsdom (Node).
- Mode live : `window.WebSocket` est remplacé par un faux qui enregistre les messages
  envoyés (`send`) et laisse le test pousser les messages serveur (`hello`, `sessions`,
  `agent_hired` / `resume_rejected`).
- Mode démo (`?demo`) : transport factice de la page, on n'observe que le DOM.

Scénario : ouvrir `#ticketDlg` via `#newTicketBtn`, saisir un brouillon dans `#ticketTitle`,
choisir `#dest` = « resume », recevoir la liste des sessions, choisir une session dans
`#session` (événements `change`), sans jamais cliquer sur `#send` ni soumettre `#newTicket`.

jsdom : cherché via `OPENSPACE_JSDOM_PATH` (dossier `node_modules` ou préfixe npm
contenant `node_modules/jsdom`), sinon via la résolution Node par défaut.
Skip si node ou jsdom sont introuvables.
"""
import functools
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from tests.spec.test_issue18_clock import _node_env

SID = "sess-dead-73"
LIVE_SID = "sess-live-73"
NAME = "Jaina-Reprise-73"
DRAFT = "BROUILLON-73-NON-ENVOYE"

NODE_SCRIPT = r"""
const { JSDOM, VirtualConsole } = require("jsdom");
const html = require("fs").readFileSync(0, "utf8");
const [mode, reply] = process.argv.slice(1);
const SID = "sess-dead-73", LIVE_SID = "sess-live-73", NAME = "Jaina-Reprise-73";
const DRAFT = "BROUILLON-73-NON-ENVOYE";
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
const chatEl = () => d.querySelector(".chat");
function chatState() {
  const c = chatEl();
  if (!c) return { exists: false, shown: false, text: "" };
  const hidden = c.hidden || !!c.closest("[hidden]") || w.getComputedStyle(c).display === "none";
  return { exists: true, shown: c.classList.contains("open") || !hidden, text: c.textContent };
}
const change = el => el && el.dispatchEvent(new w.Event("change", { bubbles: true }));
(async () => {
  await sleep(300);
  if (mode !== "demo") {
    push({ type: "hello", projects: [{ name: "proj73", cwd: "/tmp/proj73" }], team: [] });
    await sleep(100);
  }
  const chatBefore = chatState();
  d.querySelector("#newTicketBtn").click();
  await sleep(100);
  const title = d.querySelector("#ticketTitle");
  title.value = DRAFT;
  title.dispatchEvent(new w.Event("input", { bubbles: true }));
  const dest = d.querySelector("#dest");
  dest.value = "resume";
  change(dest);
  await sleep(200);
  if (mode !== "demo") {
    push({ type: "sessions", sessions: [
      { session_id: LIVE_SID, title: "TITRE-LIVE-73", project: "proj73", cwd: "/tmp/proj73",
        updated: "2026-09-28T10:00:00Z", live: true },
      { session_id: SID, title: "TITRE-REPRISE-73", project: "proj73", cwd: "/tmp/proj73",
        updated: "2026-09-27T10:00:00Z", live: false },
    ] });
  }
  await sleep(500);
  const sel = d.querySelector("#session");
  const options = [...sel.options].map(o => ({ value: o.value, text: o.textContent, disabled: o.disabled }));
  const sentBeforeSelect = sent.length;
  let chosen = SID;
  if (mode === "demo") {
    const o = [...sel.options].find(o => o.value && !o.disabled);
    chosen = o ? o.value : null;
  }
  if (chosen) { sel.value = chosen; change(sel); }
  await sleep(300);
  const sentAfterSelect = sent.slice(sentBeforeSelect);
  if (mode !== "demo" && reply === "hired") {
    push({ type: "agent_hired", agent: { id: "a-73", name: NAME, cwd: "/tmp/proj73", project: "proj73", resumed: SID } });
  } else if (mode !== "demo" && reply === "rejected") {
    push({ type: "resume_rejected", session_id: SID, reason: "RAISON-REFUS-73" });
  }
  await sleep(mode === "demo" ? 2000 : 500);
  const dlg = d.querySelector("#ticketDlg");
  const res = "@@RES@@" + JSON.stringify({
    sent, sentAfterSelect, options, chosen,
    dlgOpen: !!(dlg && (dlg.open || dlg.hasAttribute("open"))),
    draft: d.querySelector("#ticketTitle").value,
    chatBefore, chatAfter: chatState(),
    bodyText: d.body.textContent,
  }) + "\n";
  w.close();
  process.stdout.write(res, () => process.exit(0));
})().catch(e => { process.stderr.write(String(e.stack || e)); process.exit(1); });
"""


@functools.lru_cache(maxsize=None)
def _run(mode, reply=""):
    node, env = _node_env()
    r = TestClient(app).get("/")  # pas de `with` : le lifespan ne démarre pas
    assert r.status_code == 200
    out = subprocess.run([node, "-e", NODE_SCRIPT, mode, reply], input=r.text, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.rsplit("@@RES@@", 1)[1])  # bodyText peut contenir U+2028


def _types(msgs, t):
    return [m for m in msgs if m.get("type") == t]


def _resume_sent(res):
    return [m for m in _types(res["sentAfterSelect"], "resume") if m.get("session_id") == SID]


# ---------------------------------------------------------------- mode live (faux WebSocket)


def test_choisir_une_session_envoie_resume_sans_cliquer_envoyer():
    res = _run("live", "hired")
    assert _resume_sent(res), res["sent"]


def test_agent_hired_ferme_le_ticket_ouvre_la_discussion_sans_ticket():
    res = _run("live", "hired")
    assert _resume_sent(res), res["sent"]
    assert not _types(res["sent"], "new_ticket"), res["sent"]
    assert not res["dlgOpen"], "la fenêtre de ticket devrait être fermée"
    chat = res["chatAfter"]
    assert chat["exists"] and chat["shown"], chat
    assert NAME in chat["text"], chat


def test_le_brouillon_est_conserve_et_non_envoye():
    res = _run("live", "hired")
    assert _resume_sent(res), res["sent"]
    assert res["draft"] == DRAFT
    assert DRAFT not in json.dumps(res["sent"]), res["sent"]


def test_session_live_non_selectionnable():
    res = _run("live", "hired")
    live = [o for o in res["options"] if o["value"] == LIVE_SID or "TITRE-LIVE-73" in o["text"]]
    assert live, res["options"]
    assert all(o["disabled"] for o in live), live


def test_refus_sans_ticket_envoye():
    res = _run("live", "rejected")
    assert _resume_sent(res), res["sent"]
    assert not _types(res["sent"], "new_ticket"), res["sent"]


# ---------------------------------------------------------------- mode démo


def test_mode_demo_reprise_a_la_selection_sans_ticket():
    res = _run("demo")
    assert res["chosen"], f"aucune session sélectionnable en démo : {res['options']}"
    assert not res["dlgOpen"], "la fenêtre de ticket devrait être fermée"
    assert res["draft"] == DRAFT
    # un ticket créé ferait apparaître le brouillon dans la page (liste, journal, discussion)
    assert DRAFT not in res["bodyText"]
    assert not res["chatBefore"]["shown"] and res["chatAfter"]["shown"], (res["chatBefore"], res["chatAfter"])
