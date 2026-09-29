"""Tests d'intention pour l'issue #77 : titre de conversation (ai-title / custom-title) à la place du premier message.

Boîte noire, écrits depuis l'issue sans lire `frontend/index.html` ni le corps des fonctions de `backend/`.

Backend — contrat public supposé :
`backend.projects.resumable_sessions(config_dir, live, limit=30)` parcourt `config_dir/projects/*/*.jsonl` et
renvoie des dicts `session_id, cwd, project, title, updated[, live]`. `title` = `customTitle` du dernier
enregistrement `custom-title`, sinon `aiTitle` du dernier `ai-title`, sinon le premier prompt (≤ 80 caractères),
même quand l'enregistrement de titre est au-delà des 50 premières lignes.

Front — la page servie par `/` est exécutée dans jsdom avec un faux WebSocket (même harnais que #73) :
un employé est recruté par reprise (`agent_hired` avec `resumed`), ce qui ouvre son panneau de discussion ;
le test pousse ensuite `chat_history` (avec ou sans `title`) puis `chat_title` et lit `#chatProj`
(`projet · titre`, titre complet en attribut `title` ; projet seul sans titre ; `chat_title` d'un autre
employé ignoré).

jsdom : cherché via `OPENSPACE_JSDOM_PATH`, skip si node ou jsdom sont introuvables.
L'émission backend de `chat_title` en fin de réponse n'est pas couverte ici.
"""
import functools
import json
import subprocess
import uuid

from fastapi.testclient import TestClient

from backend.app import app
from backend.projects import resumable_sessions
from tests.spec.test_issue18_clock import _node_env

TS = "2026-09-29T10:00:00Z"

# ---------------------------------------------------------------- backend : resumable_sessions


def _prompt(text, cwd, sid):
    return {"type": "user", "isSidechain": False, "timestamp": TS, "cwd": str(cwd), "sessionId": sid,
            "message": {"role": "user", "content": text}}


def _answer(text, cwd, sid):
    return {"type": "assistant", "isSidechain": False, "timestamp": TS, "cwd": str(cwd), "sessionId": sid,
            "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 1, "output_tokens": 1}, "stop_reason": "end_turn"}}


def _ai(title, sid):
    return {"type": "ai-title", "aiTitle": title, "sessionId": sid}


def _custom(title, sid):
    return {"type": "custom-title", "customTitle": title, "sessionId": sid}


def _session(tmp_path, build):
    """Écrit un transcript ; `build(sid, cwd)` renvoie la liste des enregistrements. Renvoie (cfg, sid)."""
    cfg = tmp_path / "claude-config"
    sid = str(uuid.uuid4())
    cwd = tmp_path / f"proj-{uuid.uuid4().hex[:8]}"
    cwd.mkdir()
    d = cfg / "projects" / str(cwd).replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in build(sid, cwd)))
    return cfg, sid


def _title(cfg, sid):
    (s,) = [s for s in resumable_sessions(cfg, set()) if s["session_id"] == sid]
    return s["title"]


def test_dernier_ai_title_gagne(tmp_path):
    cfg, sid = _session(tmp_path, lambda sid, cwd: [
        _prompt("PREMIER-PROMPT-77", cwd, sid), _ai("TITRE-IA-ANCIEN-77", sid),
        _answer("ok", cwd, sid), _ai("TITRE-IA-ANCIEN-77", sid),
        _prompt("suite", cwd, sid), _ai("TITRE-IA-RECENT-77", sid), _answer("ok", cwd, sid)])
    assert _title(cfg, sid) == "TITRE-IA-RECENT-77"


def test_custom_title_prioritaire_sur_ai_title(tmp_path):
    cfg, sid = _session(tmp_path, lambda sid, cwd: [
        _prompt("PREMIER-PROMPT-77", cwd, sid), _ai("TITRE-IA-77", sid),
        _custom("NOM-RENOMME-77", sid), _answer("ok", cwd, sid), _ai("TITRE-IA-APRES-77", sid)])
    assert _title(cfg, sid) == "NOM-RENOMME-77"


def test_sans_titre_repli_sur_premier_prompt(tmp_path):
    long_prompt = "PREMIER-PROMPT-77 " + "x" * 200
    cfg, sid = _session(tmp_path, lambda sid, cwd: [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": sid},
        _prompt(long_prompt, cwd, sid), _answer("ok", cwd, sid)])
    t = _title(cfg, sid)
    assert t.startswith("PREMIER-PROMPT-77"), t
    assert len(t) <= 80, t


def test_titre_trouve_apres_60_lignes(tmp_path):
    def build(sid, cwd):
        recs = [_prompt("PREMIER-PROMPT-77", cwd, sid)]
        for i in range(35):
            recs += [_answer(f"reponse {i}", cwd, sid), _prompt(f"relance {i}", cwd, sid)]
        return recs + [_ai("TITRE-TARDIF-77", sid), _answer("fin", cwd, sid)]
    cfg, sid = _session(tmp_path, build)
    assert _title(cfg, sid) == "TITRE-TARDIF-77"


# ---------------------------------------------------------------- front : #chatProj (jsdom, faux WebSocket)

NODE_SCRIPT = r"""
const { JSDOM, VirtualConsole } = require("jsdom");
const html = require("fs").readFileSync(0, "utf8");
const SID = "sess-77", PROJ = "proj77";
const vc = new VirtualConsole();
vc.on("jsdomError", e => process.stderr.write("jsdomError: " + (e.stack || e) + "\n"));
const sent = [];
let sock = null;
const dom = new JSDOM(html, {
  url: "http://127.0.0.1:8000/",
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
const change = el => el && el.dispatchEvent(new w.Event("change", { bubbles: true }));
function proj() {
  const el = d.querySelector("#chatProj");
  return el ? { exists: true, text: el.textContent, title: el.getAttribute("title") || "" }
            : { exists: false, text: "", title: "" };
}
(async () => {
  await sleep(300);
  push({ type: "hello", projects: [{ name: PROJ, cwd: "/tmp/proj77" }], team: [] });
  await sleep(100);
  d.querySelector("#newTicketBtn").click();
  await sleep(100);
  const dest = d.querySelector("#dest");
  dest.value = "resume"; change(dest);
  await sleep(200);
  push({ type: "sessions", sessions: [{ session_id: SID, title: "PREMIER-PROMPT-77", project: PROJ,
    cwd: "/tmp/proj77", updated: TS_, live: false }] });
  await sleep(300);
  const sel = d.querySelector("#session");
  sel.value = SID; change(sel);
  await sleep(200);
  push({ type: "agent_hired", agent: { id: "a-77", name: "Jaina-77", cwd: "/tmp/proj77", project: PROJ, resumed: SID } });
  await sleep(400);
  const oc = sent.filter(m => m.type === "open_chat").pop();
  const aid = oc ? oc.agent_id : "a-77";
  const snap = {};
  push({ type: "chat_history", agent_id: aid, entries: [] });
  await sleep(200); snap.noTitle = proj();
  push({ type: "chat_history", agent_id: aid, entries: [], title: "TITRE-HISTO-77" });
  await sleep(200); snap.history = proj();
  push({ type: "chat_title", agent_id: aid, title: "TITRE-MAJ-77" });
  await sleep(200); snap.update = proj();
  push({ type: "chat_title", agent_id: "a-autre-77", title: "TITRE-AUTRE-77" });
  await sleep(200); snap.other = proj();
  const res = "@@RES@@" + JSON.stringify({ sent, aid, snap }) + "\n";
  w.close();
  process.stdout.write(res, () => process.exit(0));
})().catch(e => { process.stderr.write(String(e.stack || e)); process.exit(1); });
""".replace("TS_", json.dumps(TS))


@functools.lru_cache(maxsize=None)
def _run():
    node, env = _node_env()
    r = TestClient(app).get("/")  # pas de `with` : le lifespan ne démarre pas
    assert r.status_code == 200
    out = subprocess.run([node, "-e", NODE_SCRIPT], input=r.text, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.rsplit("@@RES@@", 1)[1])


def test_chat_history_sans_titre_affiche_le_projet_seul():
    p = _run()["snap"]["noTitle"]
    assert p["exists"], "#chatProj absent"
    assert "proj77" in p["text"], p
    assert "·" not in p["text"], p


def test_chat_history_avec_titre_affiche_projet_et_titre():
    p = _run()["snap"]["history"]
    assert "proj77" in p["text"] and "TITRE-HISTO-77" in p["text"], p
    assert "TITRE-HISTO-77" in p["title"], p


def test_chat_title_met_a_jour_le_panneau_ouvert():
    p = _run()["snap"]["update"]
    assert "proj77" in p["text"] and "TITRE-MAJ-77" in p["text"], p
    assert "TITRE-HISTO-77" not in p["text"], p
    assert "TITRE-MAJ-77" in p["title"], p


def test_chat_title_d_un_autre_employe_ignore():
    p = _run()["snap"]["other"]
    assert "TITRE-AUTRE-77" not in p["text"] and "TITRE-AUTRE-77" not in p["title"], p
    assert "TITRE-MAJ-77" in p["text"], p
