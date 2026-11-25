"""Cartoon story endpoints. Script writing and rendering run as background jobs (the page polls; nothing blocks or times out).
Two-step rule: a script is created by the server first (it gets a token); rendering only accepts a script carrying that token."""
import json, re, threading, time, uuid, pathlib
from flask import request, jsonify
from . import db as mdb, images, cartoon, hashtags
from . import routes
from .routes import bp, db
from .create import parse_json, ai, AIError

JOBS = {}
TOKENS = {}                         # script token -> time issued
JLOCK = threading.Lock()


def _gc():
    now = time.time()
    for k in [k for k, j in JOBS.items() if now - j["t"] > 7200]:
        JOBS.pop(k, None)
    for k in [k for k, t in TOKENS.items() if now - t > 86400]:
        TOKENS.pop(k, None)


def start_job(fn):
    """fn(set_stage) -> result dict. Returns the job id."""
    _gc()
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "state": "running", "stage": "starting", "result": None, "error": "", "t": time.time()}
    JOBS[jid] = job

    def run():
        try:
            job["result"] = fn(lambda s: job.__setitem__("stage", s))
            job["state"] = "done"
        except (images.RenderError, AIError, ValueError) as ex:
            job["state"], job["error"] = "error", str(ex)[:300]
        except Exception as ex:
            job["state"], job["error"] = "error", "Unexpected error: %s" % str(ex)[:200]
    threading.Thread(target=run, daemon=True).start()
    return jid


def _ctx(j):
    c = db()
    b = c.execute("SELECT * FROM brands WHERE id=?", (j.get("brand_id"),)).fetchone()
    p = c.execute("SELECT * FROM products WHERE id=?", (j.get("product_id"),)).fetchone()
    c.close()
    return b, p


def _lang(j):
    return j.get("language") if j.get("language") in cartoon.LANGS else "Hinglish"


def write_script(b, p, j, set_stage):
    n = max(cartoon.MIN_SLIDES, min(cartoon.MAX_SLIDES, int(j.get("slides") or 5)))
    set_stage("writing script")
    prompt = cartoon.script_prompt(j.get("brief", ""), p, b["name"], _lang(j), n, j.get("tone"), j.get("cta"))
    last = None
    for attempt in range(2):                                   # one retry if the answer is not valid JSON
        raw = ai("marketing_story", prompt + ("" if attempt == 0 else "\nReturn ONLY the JSON object, nothing else."), big=True)
        try:
            d = parse_json(raw)
            break
        except Exception as ex:
            last = ex
    else:
        raise ValueError("The AI answer could not be read. Press Write script again.")
    script = cartoon.clean_script(d, n, cartoon.product_facts(p), _lang(j))
    tags = hashtags.build(script.pop("hashtags_raw"))
    script["hashtags"] = hashtags.as_text(tags)
    script["tiers"] = hashtags.tier_map(tags)
    token = uuid.uuid4().hex[:12]
    TOKENS[token] = time.time()
    script["token"] = token
    return script


@bp.route("/api/marketing/story/script", methods=["POST"])
def api_story_script():
    j = request.get_json(force=True, silent=True) or {}
    b, p = _ctx(j)
    if not b:
        return jsonify(ok=False, error="Choose a brand first.")
    if not (j.get("brief") or "").strip():
        return jsonify(ok=False, error="Describe the story first.")
    return jsonify(ok=True, job=start_job(lambda st: write_script(b, p, j, st)))


@bp.route("/api/marketing/story/slide", methods=["POST"])
def api_story_slide():
    j = request.get_json(force=True, silent=True) or {}
    b, p = _ctx(j)
    script = j.get("script") or {}
    i = int(j.get("index", -1))
    if not b or script.get("token") not in TOKENS or not 0 <= i < len(script.get("slides") or []):
        return jsonify(ok=False, error="Write the script first.")
    script["slides"] = [cartoon.clean_slide(s) for s in script["slides"]]

    def work(st):
        st("rewriting slide %d" % (i + 1))
        raw = ai("marketing_story", cartoon.slide_prompt(script, i, j.get("brief", ""), p, _lang(j), j.get("tone"), j.get("instruction", "")), big=True)
        try:
            d = parse_json(raw)
        except Exception:
            raise ValueError("The AI answer could not be read. Press Regenerate again.")
        s = cartoon.clean_slide(d)
        note = cartoon.claim_flags(s, cartoon.product_facts(p))
        s["claim_note"] = "claim not in product details - confirm before posting" if note else ""
        return {"index": i, "slide": s}
    return jsonify(ok=True, job=start_job(work))


@bp.route("/api/marketing/story/render", methods=["POST"])
def api_story_render():
    j = request.get_json(force=True, silent=True) or {}
    b, p = _ctx(j)
    script = j.get("script") or {}
    if not b:
        return jsonify(ok=False, error="Choose a brand first.")
    if script.get("token") not in TOKENS:
        return jsonify(ok=False, error="Write the script first. Rendering only uses a script you have seen.")
    slides = [cartoon.clean_slide(s) for s in (script.get("slides") or [])][:cartoon.MAX_SLIDES]
    if not slides or not any(s["dialogue"] or s["narration"] for s in slides):
        return jsonify(ok=False, error="The script has no dialogue to render.")
    for s in slides:                                           # keep the owner's wording; only refresh the yellow notes
        old = s["claim_note"]
        note = cartoon.claim_flags(dict(s, claim_note=""), cartoon.product_facts(p))
        s["claim_note"] = "claim not in product details - confirm before posting" if (note or old) else ""
    script = dict(script, slides=slides)
    tok = uuid.uuid4().hex[:10]

    def work(st):
        files = cartoon.render_script(script, b, p, mdb.OUT_ROOT / ("p_" + tok), cta=j.get("cta", ""), progress=st)
        return {"token": tok, "images": ["/marketing/out/p_%s/%s" % (tok, pathlib.Path(f).name) for f in files]}
    return jsonify(ok=True, job=start_job(work))


@bp.route("/api/marketing/story/job/<jid>")
def api_story_job(jid):
    job = JOBS.get(re.sub(r"[^a-z0-9]", "", jid))
    if not job:
        return jsonify(ok=False, error="Job not found (the app may have restarted). Start again.")
    return jsonify(ok=True, state=job["state"], stage=job["stage"], error=job["error"], result=job["result"] if job["state"] == "done" else None)
