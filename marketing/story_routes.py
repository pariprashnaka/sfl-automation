"""Cartoon story endpoints. Script writing and rendering run as background jobs (the page polls; nothing blocks or times out).
Two-step rule: a script is created by the server first (it gets a token); rendering only accepts a script carrying that token."""
import json, re, threading, time, uuid, pathlib
from flask import request, jsonify
from . import db as mdb, images, cartoon, hashtags, imagegen, shotslides, zones
from . import shots as shotlib
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
    return j.get("language") if j.get("language") in cartoon.LANGS else "Hindi"


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
    script = cartoon.clean_script(d, n, cartoon.product_facts(p), _lang(j), j.get("cta") or "", product_name=p["name"] if p else "")
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
        if not s["setting"]:                                   # keep the place of the slide being replaced
            old = script["slides"][i]
            s["setting_key"], s["setting"] = old.get("setting_key", ""), old.get("setting", "")
        note = cartoon.claim_flags(s, cartoon.product_facts(p))
        s["claim_note"] = "claim not in product details - confirm before posting" if note else ""
        return {"index": i, "slide": s}
    return jsonify(ok=True, job=start_job(work))


def _cast(b):
    c = db()
    rows = [dict(r) for r in c.execute(
        "SELECT m.role, COALESCE(NULLIF(ch.name,''), m.name) AS name, COALESCE(NULLIF(ch.description,''), m.description) AS description, m.seed, ch.master AS master "
        "FROM cast_members m LEFT JOIN characters ch ON ch.id=m.char_id WHERE m.brand_id=?", (b["id"],))]
    c.close()
    for r in rows:                                              # master image of the locked character, if it exists
        f = (mdb.ASSETS / r["master"]) if r.get("master") else None
        r["master"] = str(f) if f and f.is_file() else None
    return rows or [dict(role=r, name=n, description=d, seed=sd) for r, n, d, sd in imagegen.DEFAULT_CAST]


def _notes(slides):
    """Screenshot id -> note for every screenshot used in these slides."""
    ids = [x["id"] for s in slides for x in s.get("shots", [])]
    c = db()
    got = shotlib.get_many(c, ids)
    c.close()
    return got


def _gen_slide(slide, cast, j, ref=None):
    """One picture through the provider chain: the scene illustration, or (device slides with AI background) an EMPTY shop background.
    Returns the imagegen result dict (file None = use the SVG parts / brand gradient)."""
    M = routes.M
    off = j.get("imgmode") == "svg"
    if slide.get("type") == "shot" or (slide.get("type") == "device" and slide.get("bg") != "ai"):
        return {"file": None, "provider": "svg", "reason": ""}
    if slide.get("type") == "device":
        return imagegen.generate_chain(imagegen.empty_bg_prompt("shop"), imagegen.seed_for(5151, slide.get("img_v")), bool(j.get("hq")), M.env_val, M.conn, M.qset, off=off)
    return imagegen.scene_chain(slide, cast, cartoon.SCENES, bool(j.get("hq")), M.env_val, M.conn, M.qset, off=off)


def _ref_bytes(script, cast, j):
    """First slide's picture, used by Gemini as the character reference for later slides."""
    if not j.get("hq") or not script["slides"]:
        return None
    s0 = script["slides"][0]
    prompt, seed = cartoon.slide_prompt_for(s0, cast)
    f = imagegen.cache_file(imagegen.cache_key(prompt, imagegen.seed_for(seed, s0.get("img_v")), "gemini"))
    return f.read_bytes() if f else None


def _img_fields(slide, r):
    slide["image"] = r["file"].name if r["file"] else ""
    slide["provider"] = r["provider"] if (r["file"] or slide.get("type", "scene") == "scene") else ""
    slide["img_note"] = (r.get("reason") or r.get("note") or "")[:300]
    return slide


@bp.route("/api/marketing/story/image", methods=["POST"])
def api_story_image():
    """Regenerate the picture of one slide (new seed variant). Runs as a background job."""
    j = request.get_json(force=True, silent=True) or {}
    b, p = _ctx(j)
    script = j.get("script") or {}
    i = int(j.get("index", -1))
    if not b or script.get("token") not in TOKENS or not 0 <= i < len(script.get("slides") or []):
        return jsonify(ok=False, error="Write the script first.")
    script["slides"] = [cartoon.clean_slide(s) for s in script["slides"]]
    cast = _cast(b)

    def work(st):
        st("drawing picture for slide %d" % (i + 1))
        s = script["slides"][i]
        s["img_v"] = (s.get("img_v") or 0) + 1
        r = _gen_slide(s, cast, j, ref=_ref_bytes(script, cast, j) if i else None)
        return {"index": i, "slide": _img_fields(s, r)}
    return jsonify(ok=True, job=start_job(work))


@bp.route("/api/marketing/story/imgstatus")
def api_story_imgstatus():
    c = db()
    used = imagegen.used_today(c)
    c.close()
    st = imagegen.status(routes.M.env_val)
    return jsonify(ok=True, used=used, **st)


@bp.route("/marketing/img/<name>")
def img_file(name):
    n = imagegen.safe_name(name)
    p = imagegen.CACHE_DIR / n if n else None
    if not p or not p.is_file():
        return "", 404
    from flask import send_file
    return send_file(str(p), max_age=3600)


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
    if not slides or not any(s["dialogue"] or s["narration"] or s["shots"] for s in slides):
        return jsonify(ok=False, error="The script has no dialogue or screenshots to render.")
    shots = _notes(slides)
    notes = {k: v["note"] for k, v in shots.items()}
    for s in slides:                                           # keep the owner's wording; only refresh the yellow notes
        old = s["claim_note"]
        note = cartoon.claim_flags(dict(s, claim_note=""), cartoon.slide_facts(s, cartoon.product_facts(p), notes))
        s["claim_note"] = "claim not in product details - confirm before posting" if (note or old) else ""
    added = cartoon.ensure_cta(slides, j.get("cta", ""))
    cartoon.drop_cta_text(slides, j.get("cta", ""))
    script = dict(script, slides=slides)
    tok = uuid.uuid4().hex[:10]
    cast = _cast(b)
    provs = []

    def get_image(i):
        s = script["slides"][i]
        ref = _ref_bytes(script, cast, j) if i else None
        r = _gen_slide(s, cast, j, ref=ref)
        _img_fields(s, r)
        provs.append(r["provider"])
        return str(r["file"]) if r["file"] else None

    def work(st):
        names = {c["role"]: c["name"] for c in cast if c["name"]}
        files = cartoon.render_script(script, b, p, mdb.OUT_ROOT / ("p_" + tok), cta=j.get("cta", ""), progress=st, get_image=get_image,
                                      lookup=shots.get, names=names)
        for s in script["slides"]:
            s.pop("_file", None)
        return {"token": tok, "images": ["/marketing/out/p_%s/%s" % (tok, pathlib.Path(f).name) for f in files], "providers": provs,
                "slides": script["slides"], "added_cta": added}
    return jsonify(ok=True, job=start_job(work))


def _lang_of(script, j):
    return script.get("language") if script.get("language") in cartoon.LANGS else _lang(j)


def _check_notes(slides, cta):
    return [zones.too_long(s, cta) for s in slides]


@bp.route("/api/marketing/story/check", methods=["POST"])
def api_story_check():
    """Editor warnings: which slides have text that will not fit ('text too long for this slide')."""
    j = request.get_json(force=True, silent=True) or {}
    slides = [cartoon.clean_slide(s) for s in ((j.get("script") or {}).get("slides") or [])][:cartoon.MAX_SLIDES]
    return jsonify(ok=True, notes=_check_notes(slides, (j.get("cta") or "").strip()))


@bp.route("/api/marketing/story/blank", methods=["POST"])
def api_story_blank():
    """Carousel mode: start a script with one empty screenshot slide (no AI story). Gets a token like a written script."""
    j = request.get_json(force=True, silent=True) or {}
    b, p = _ctx(j)
    if not b:
        return jsonify(ok=False, error="Choose a brand first.")
    s = cartoon.clean_slide(dict(cartoon.blank_slide(), type="shot"))
    token = uuid.uuid4().hex[:12]
    TOKENS[token] = time.time()
    return jsonify(ok=True, script={"title": "", "caption": "", "alt": "", "hashtags": "", "tiers": {}, "slides": [s], "language": _lang(j), "token": token})


@bp.route("/api/marketing/story/addshots", methods=["POST"])
def api_story_addshots():
    """The AI inserts screenshot slides (1-6 chosen screenshots) at sensible places in the existing script. Everything stays editable."""
    j = request.get_json(force=True, silent=True) or {}
    b, p = _ctx(j)
    script = j.get("script") or {}
    ids = [int(x) for x in (j.get("shot_ids") or []) if str(x).isdigit()][:6]
    if not b or script.get("token") not in TOKENS:
        return jsonify(ok=False, error="Write the script first.")
    if not ids:
        return jsonify(ok=False, error="Choose 1 to 6 screenshots first.")
    slides = [cartoon.clean_slide(x) for x in (script.get("slides") or [])][:cartoon.MAX_SLIDES]
    if not slides:
        return jsonify(ok=False, error="Write the script first.")
    got = _notes(slides)
    c = db()
    chosen = shotlib.get_many(c, ids)
    c.close()
    if not chosen:
        return jsonify(ok=False, error="Those screenshots were not found.")
    script = dict(script, slides=slides)
    lang = _lang_of(script, j)

    def work(st):
        st("placing screenshots")
        facts = cartoon.product_facts(p)
        raw = ai("marketing_caption", shotslides.insert_prompt(script, chosen, facts, lang), big=True)
        try:
            d = parse_json(raw)
        except Exception:
            raise ValueError("The AI answer could not be read. Press Add screenshots again.")
        return {"slides": apply_inserts(slides, d, chosen, facts, (j.get("cta") or "").strip()), "added": len(chosen)}
    return jsonify(ok=True, job=start_job(work))


def apply_inserts(slides, d, chosen, facts, cta):
    """Insert validated screenshot slides into a copy of the slides; the call-to-action slide stays last; captions get the claim check."""
    slides = [dict(s) for s in slides]
    has_cta_last = bool(slides and slides[-1].get("is_cta"))
    aspects = {k: (v["w"] / v["h"]) for k, v in chosen.items() if v.get("w") and v.get("h")}
    plan = shotslides.plan_inserts(d, set(chosen), len(slides), has_cta_last, aspects, cartoon.MAX_SLIDES - len(slides))
    notes = {k: v["note"] for k, v in chosen.items()}
    out = list(slides)
    for k, ins in enumerate(plan):
        cap = ins["caption"] or " ".join(chosen[x["id"]]["note"] for x in ins["shots"])[:140]
        sl = cartoon.clean_slide({"type": ins["type"], "shots": ins["shots"], "layout": ins.get("layout"), "device": ins.get("device"), "narration": cap,
                                  "characters": [{"role": "shopkeeper", "position": "left"}]})
        flag = cartoon.claim_flags(sl, cartoon.slide_facts(sl, facts, notes))
        sl["claim_note"] = "claim not in product details - confirm before posting" if flag else ""
        out.insert(ins["after"] + k, sl)                # earlier inserts shift later positions by one each
    return out


@bp.route("/api/marketing/story/caption", methods=["POST"])
def api_story_caption():
    """Rewrite the caption of one screenshot slide from the screenshot notes and the product record only."""
    j = request.get_json(force=True, silent=True) or {}
    b, p = _ctx(j)
    script = j.get("script") or {}
    i = int(j.get("index", -1))
    slides = [cartoon.clean_slide(x) for x in (script.get("slides") or [])]
    if not b or script.get("token") not in TOKENS or not 0 <= i < len(slides) or not slides[i]["shots"]:
        return jsonify(ok=False, error="Choose a screenshot slide first.")
    sl = slides[i]
    got = _notes([sl])
    lang = _lang_of(script, j)

    def work(st):
        st("writing caption for slide %d" % (i + 1))
        facts = cartoon.product_facts(p)
        raw = ai("marketing_caption", shotslides.caption_prompt(sl, [got[x["id"]] for x in sl["shots"] if x["id"] in got], facts, lang, j.get("instruction", "")), big=True)
        try:
            cap = str(parse_json(raw).get("caption") or "").strip()
        except Exception:
            raise ValueError("The AI answer could not be read. Press Regenerate caption again.")
        if not cap:
            raise ValueError("The AI returned no caption. Press Regenerate caption again.")
        sl["narration"] = imagegen.scrub(cap)
        flag = cartoon.claim_flags(dict(sl, claim_note=""), cartoon.slide_facts(sl, facts, {k: v["note"] for k, v in got.items()}))
        sl["claim_note"] = "claim not in product details - confirm before posting" if flag else ""
        return {"index": i, "slide": sl}
    return jsonify(ok=True, job=start_job(work))


@bp.route("/api/marketing/story/job/<jid>")
def api_story_job(jid):
    job = JOBS.get(re.sub(r"[^a-z0-9]", "", jid))
    if not job:
        return jsonify(ok=False, error="Job not found (the app may have restarted). Start again.")
    return jsonify(ok=True, state=job["state"], stage=job["stage"], error=job["error"], result=job["result"] if job["state"] == "done" else None)
