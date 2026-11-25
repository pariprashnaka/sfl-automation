"""Illustration providers for cartoon stories. The model NEVER draws text: every prompt forbids it, and Hindi dialogue is added by Chromium later.
Providers: "cloudflare" (free default) and "gemini" (paid, only with the High quality switch). If keys are missing, the daily cap is hit or a call
fails, generate() returns no image and the caller falls back to the SVG parts. Images are cached by prompt+seed+provider."""
import base64, hashlib, json, pathlib, re, time
import requests

CACHE_DIR = pathlib.Path(r"D:\vault\outputs\marketing\imagegen")
CF_MODEL = "@cf/black-forest-labs/flux-1-schnell"
CF_URL = "https://api.cloudflare.com/client/v4/accounts/%s/ai/run/" + CF_MODEL
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
DEFAULT_CAPS = {"cloudflare": 100, "gemini": 40}          # images per day; editable with settings mkt_img_cap_<provider>
TIMEOUT = 90
RETRIES = 3
SLEEP = time.sleep                                        # tests replace this

STYLE = "premium editorial caricature illustration, expressive faces, slightly exaggerated features, warm lighting, small Indian kirana shop, clean composition"
NO_TEXT = ("No text anywhere: blank signboards, no signs, no readable text, no letters, no numbers, no logos, no watermarks, no captions, no speech bubbles; "
           "any paper or screen stays blank.")
FRAMING = ("Medium shot, whole upper body visible. Characters fill at most 70 percent of the frame height, centered, with a clear empty margin on every side. "
           "Plain uncluttered background.")
SAFETY = ("Original fictional characters only, not resembling any real person, celebrity, brand mascot or known cartoon character. " + NO_TEXT + " " + FRAMING)
BG_STYLE = "premium editorial illustration, warm soft lighting, clean uncluttered composition, shallow depth, slightly blurred background"
BG_PLACES = {"shop": "an empty small Indian kirana shop interior with a wooden counter and shelves",
             "office": "an empty small Indian office with a plain desk and a window"}


def empty_bg_prompt(kind="shop"):
    """Background for device slides: a place with NOTHING on it that could compete with the screenshot."""
    return ("%s. %s, seen from the front, no people, no devices, no screens, no computers, no phones, no monitors, no pictures on the walls. %s"
            % (BG_STYLE, BG_PLACES.get(kind, BG_PLACES["shop"]), NO_TEXT))


DEFAULT_CAST = [
    ("shopkeeper", "Ramesh", "middle-aged Indian shopkeeper, round friendly face, thick black moustache, no glasses, short black hair with a little grey, "
     "mustard yellow kurta, white dhoti", 1101),
    ("shopkeeper2", "Sunil", "younger Indian shopkeeper, thin face, neat trimmed beard, round glasses, green half-sleeve shirt, black trousers, "
     "slightly curly hair", 2202),
    ("customer", "Meena", "Indian woman customer in her thirties, oval face, big expressive eyes, long black plait, purple saree with a small border, "
     "red bindi, cloth shopping bag", 3303),
]
LABELS = {"shopkeeper": "the first shopkeeper", "shopkeeper2": "the second shopkeeper", "customer": "the customer"}
ROLE_RE = re.compile(r"\b(shop\s*keeper\s*[_ -]?\s*2|shop\s*keeper\s*[_ -]?\s*two|shop\s*keeper|customer)\b", re.I)


class ImageError(Exception):
    pass


class Retryable(ImageError):
    pass


# ---------------- usage + cap ----------------
def today():
    return time.strftime("%Y-%m-%d")


def used_today(c, provider=None):
    if provider:
        r = c.execute("SELECT n FROM imagegen_usage WHERE day=? AND provider=?", (today(), provider)).fetchone()
        return r[0] if r else 0
    return {r[0]: r[1] for r in c.execute("SELECT provider, n FROM imagegen_usage WHERE day=?", (today(),))}


def count_use(c, provider, neurons=0):
    c.execute("INSERT INTO imagegen_usage(day,provider,n,neurons) VALUES(?,?,1,?) ON CONFLICT(day,provider) DO UPDATE SET n=n+1, neurons=neurons+?",
              (today(), provider, neurons, neurons))
    c.commit()


# Cloudflare free tier is counted in neurons per day (10,000). Estimates: about 125 for a 1024x1024 image with 4 references.
NEURON_CAP = 10000
NEURON_SCHNELL = 10
NEURON_IMAGE_4REF = 125


def neurons_for(kind, refs=0):
    return NEURON_SCHNELL if kind == "schnell" else 45 + 20 * min(4, max(0, refs))


def neurons_today(c):
    r = c.execute("SELECT COALESCE(SUM(neurons),0) FROM imagegen_usage WHERE day=? AND provider='cloudflare'", (today(),)).fetchone()
    return r[0] or 0


def neuron_cap(setting):
    try:
        return int(setting("mkt_neuron_cap", NEURON_CAP))
    except Exception:
        return NEURON_CAP


def images_left(c, setting):
    """How many more reference images fit in today's free budget (worst case 125 neurons each)."""
    return max(0, (neuron_cap(setting) - neurons_today(c)) // NEURON_IMAGE_4REF)


def queued_count(c):
    return c.execute("SELECT COUNT(*) FROM img_queue WHERE status='pending'").fetchone()[0]


def cap(provider, setting):
    try:
        return int(setting("mkt_img_cap_" + provider, DEFAULT_CAPS[provider]))
    except Exception:
        return DEFAULT_CAPS[provider]


# ---------------- config ----------------
def status(env):
    """What is configured, as text for the UI. Never returns a key value."""
    cf = bool(env("CF_ACCOUNT_ID") and env("CF_API_TOKEN"))
    gem_key, gem_model = bool(env("GEMINI_API_KEY")), bool(env("GEMINI_IMAGE_MODEL"))
    msg = ""
    if not gem_model:
        msg = "High quality needs GEMINI_IMAGE_MODEL in .env (not set)."
    elif not gem_key:
        msg = "High quality needs GEMINI_API_KEY in .env (not set)."
    return {"cloudflare": cf, "gemini": gem_key and gem_model, "cloudflare_msg": "" if cf else "Add CF_ACCOUNT_ID and CF_API_TOKEN to .env for AI illustrations.",
            "gemini_msg": msg}


# ---------------- HTTP (single seam for tests) ----------------
def http_post(url, headers, payload):
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    except requests.RequestException as ex:
        raise Retryable("network error: %s" % type(ex).__name__)
    if r.status_code == 429 or r.status_code >= 500:
        raise Retryable("HTTP %d" % r.status_code)
    if r.status_code >= 400:
        raise ImageError("HTTP %d: %s" % (r.status_code, r.text[:160].replace("\n", " ")))
    try:
        return r.json()
    except ValueError:
        raise Retryable("answer was not JSON")


def _b64(s):
    try:
        b = base64.b64decode(s)
    except Exception:
        raise Retryable("image data could not be decoded")
    if b[:3] != b"\xff\xd8\xff" and b[:4] != b"\x89PNG":
        raise Retryable("answer was not an image")
    return b


def cloudflare(prompt, seed, env, ref=None):
    acc, tok = env("CF_ACCOUNT_ID"), env("CF_API_TOKEN")
    if not (acc and tok):
        raise ImageError("CF_ACCOUNT_ID / CF_API_TOKEN not set")
    hdr = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    body = {"prompt": prompt, "steps": 6, "seed": int(seed) % 4294967295}
    try:
        j = http_post(CF_URL % acc, hdr, body)
    except ImageError as ex:
        if "HTTP 400" not in str(ex):
            raise
        body.pop("seed")                                  # model rejected the seed field: ask without it
        j = http_post(CF_URL % acc, hdr, body)
    img = (j.get("result") or {}).get("image")
    if not img:
        raise Retryable("no image in answer")
    return _b64(img)


def gemini(prompt, seed, env, ref=None):
    key, model = env("GEMINI_API_KEY"), env("GEMINI_IMAGE_MODEL")
    if not model:
        raise ImageError("GEMINI_IMAGE_MODEL not set")
    if not key:
        raise ImageError("GEMINI_API_KEY not set")
    parts = [{"text": prompt}]
    if ref:
        parts.insert(0, {"inline_data": {"mime_type": "image/png" if ref[:4] == b"\x89PNG" else "image/jpeg", "data": base64.b64encode(ref).decode()}})
        parts[1]["text"] = "Keep the same characters, faces, clothes and art style as the reference image. " + prompt
    body = {"contents": [{"parts": parts}], "generationConfig": {"responseModalities": ["IMAGE"], "seed": int(seed) % 2147483647}}
    j = http_post(GEMINI_URL % model, {"x-goog-api-key": key, "Content-Type": "application/json"}, body)
    for cand in j.get("candidates") or []:
        for p in (cand.get("content") or {}).get("parts") or []:
            d = p.get("inlineData") or p.get("inline_data")
            if d and d.get("data"):
                return _b64(d["data"])
    raise Retryable("no image in answer")


PROVIDERS = {"cloudflare": cloudflare, "gemini": gemini}


# ---------------- cache + generate ----------------
def cache_key(prompt, seed, provider):
    return hashlib.sha256(json.dumps([prompt, int(seed), provider], ensure_ascii=False).encode("utf-8")).hexdigest()[:32]


def cache_file(key):
    for ext in (".jpg", ".png"):
        p = CACHE_DIR / (key + ext)
        if p.exists() and p.stat().st_size > 1000:
            return p
    return None


def safe_name(name):
    """Only names this module produced are accepted from the browser."""
    return name if re.fullmatch(r"[0-9a-f]{32}\.(jpg|png)", name or "") else None


def call_with_retry(fn, prompt, seed, env, ref):
    last = None
    for i in range(RETRIES):
        try:
            return fn(prompt, seed, env, ref)
        except Retryable as ex:
            last = ex
            if i < RETRIES - 1:
                SLEEP(2 ** i)
    raise ImageError("failed after %d tries (%s)" % (RETRIES, last))


def generate(prompt, seed, provider, env, conn, setting, ref=None, fresh=False):
    """Returns {"file": Path, "provider": name, "cached": bool} or {"file": None, "provider": "svg", "reason": text}."""
    fn = PROVIDERS.get(provider)
    if not fn:
        return {"file": None, "provider": "svg", "reason": "AI illustrations are off"}
    key = cache_key(prompt, seed, provider)
    hit = None if fresh else cache_file(key)
    if hit:
        return {"file": hit, "provider": provider, "cached": True}
    st = status(env)
    if not st[provider]:
        return {"file": None, "provider": "svg", "reason": st[provider + "_msg"] or (provider + " is not configured")}
    c = conn()
    try:
        cost = NEURON_SCHNELL if provider == "cloudflare" else 0
        if provider == "cloudflare":
            if neurons_today(c) + cost > neuron_cap(setting):
                return {"file": None, "provider": "svg", "reason": "cloudflare daily limit reached (%d neurons)" % neuron_cap(setting)}
        elif used_today(c, provider) >= cap(provider, setting):
            return {"file": None, "provider": "svg", "reason": "%s daily limit reached (%d images)" % (provider, cap(provider, setting))}
        try:
            data = call_with_retry(fn, prompt, seed, env, ref)
        except ImageError as ex:
            return {"file": None, "provider": "svg", "reason": "%s: %s" % (provider, str(ex)[:160])}
        count_use(c, provider, cost)
    finally:
        c.close()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / (key + (".png" if data[:4] == b"\x89PNG" else ".jpg"))
    p.write_bytes(data)
    return {"file": p, "provider": provider, "cached": False}


def generate_chain(prompt, seed, hq, env, conn, setting, ref=None, fresh=False, off=False):
    """High quality: gemini, then cloudflare, then SVG. Normal: cloudflare, then SVG. off=True: SVG only."""
    if off:
        return {"file": None, "provider": "svg", "reason": "AI illustrations switched off"}
    reasons = []
    for prov in (["gemini", "cloudflare"] if hq else ["cloudflare"]):
        r = generate(prompt, seed, prov, env, conn, setting, ref=ref if prov == "gemini" else None, fresh=fresh)
        if r["file"]:
            if reasons:
                r["note"] = "; ".join(reasons)
            return r
        reasons.append(r["reason"])
    return {"file": None, "provider": "svg", "reason": "; ".join(reasons)}


# ---------------- prompts ----------------
def scrub(text, names=None):
    """Remove internal role labels (Shopkeeper2 etc.) from text meant for people or for the model. names: role -> display name."""
    names = names or {}
    t = re.sub(r"(?m)^\s*(shop\s*keeper\s*[_ -]?\s*(2|two)?|customer)\s*[:\-]\s*", "", str(text or ""), flags=re.I)

    def sub(m):
        k = re.sub(r"[\s_-]+", "", m.group(0).lower())
        role = "shopkeeper2" if k in ("shopkeeper2", "shopkeepertwo") else ("customer" if k == "customer" else "shopkeeper")
        return names.get(role) or LABELS[role]
    return ROLE_RE.sub(sub, t).strip()


def scene_prompt(slide, cast, scene_names):
    """style + cast + the scene from the script. Dialogue is never included."""
    by = {c["role"]: c for c in cast}
    names = {r: by[r]["name"] for r in by if by[r]["name"]}
    people = []
    left_right = []
    for ch in slide.get("characters", [])[:2]:
        c = by.get(ch.get("role"))
        if not c:
            continue
        people.append("%s: %s, expression %s" % (c["name"], c["description"], ch.get("expression") or "neutral"))
        left_right.append("%s on the %s" % (c["name"], ch.get("position") or "center"))
    desc = re.sub(r"[\"\u201c\u201d'][^\"\u201c\u201d']{0,200}[\"\u201c\u201d']", "", scrub(slide.get("description"), names))
    props = ", ".join(p for p in slide.get("props", []))
    scene = scene_names.get(slide.get("scene"), "shop")
    seed = by.get((slide.get("characters") or [{}])[0].get("role"), {}).get("seed") or 1
    prompt = "%s. Setting: %s. Characters: %s. Positions: %s. Scene: %s.%s %s" % (
        STYLE, scene, " | ".join(people) or "none", ", ".join(left_right) or "centered", desc or "the characters talk",
        (" Objects shown: %s." % props) if props else "", SAFETY)
    return prompt, int(seed)


def seed_for(base, variant):
    return int(base) + 7919 * int(variant or 0)


# ---------------- reference images: Cloudflare flux-2-klein-4b (free, no Gemini needed) ----------------
KLEIN = "@cf/black-forest-labs/flux-2-klein-4b"
KLEIN_URL = "https://api.cloudflare.com/client/v4/accounts/%s/ai/run/" + KLEIN
SCENE_W, SCENE_H = 1024, 720                                 # picture box 1080x760 is 1.42:1; sizes are multiples of 16
SAME = "Same person as in the reference image, same face, hair, clothes. "
CAND_STYLE = ("premium editorial caricature illustration, upper body portrait, plain light background, centered, looking at the viewer. "
              "Expressive face, slightly exaggerated features, warm lighting. " + NO_TEXT)
QUOTED = re.compile("[\"“”'][^\"“”']{0,200}[\"“”']")


def http_post_multipart(url, headers, fields, files):
    """multipart/form-data POST. fields: {name: str}, files: {name: bytes}. Single seam for tests."""
    try:
        r = requests.post(url, headers=headers, data=fields, files={k: (k + ".png", v, "image/png") for k, v in files.items()}, timeout=TIMEOUT * 2)
    except requests.RequestException as ex:
        raise Retryable("network error: %s" % type(ex).__name__)
    if r.status_code == 429 or r.status_code >= 500:
        raise Retryable("HTTP %d" % r.status_code)
    if r.status_code >= 400:
        raise ImageError("HTTP %d: %s" % (r.status_code, r.text[:160].replace("\n", " ")))
    try:
        return r.json()
    except ValueError:
        raise Retryable("answer was not JSON")


def klein(prompt, refs, w, h, env):
    """refs: list of image bytes (input_image_0..3). Returns image bytes."""
    acc, tok = env("CF_ACCOUNT_ID"), env("CF_API_TOKEN")
    if not (acc and tok):
        raise ImageError("CF_ACCOUNT_ID / CF_API_TOKEN not set")
    fields = {"prompt": prompt, "width": str(int(w)), "height": str(int(h))}
    files = {"input_image_%d" % k: b for k, b in enumerate(refs[:4])}
    j = http_post_multipart(KLEIN_URL % acc, {"Authorization": "Bearer " + tok}, fields, files)
    img = (j.get("result") or {}).get("image")
    if not img:
        raise Retryable("no image in answer")
    return _b64(img)


def ref_key(prompt, refs, w, h):
    hs = [hashlib.sha256(b).hexdigest()[:16] for b in refs]
    return hashlib.sha256(json.dumps(["klein", prompt, hs, w, h], ensure_ascii=False).encode("utf-8")).hexdigest()[:32]


def _enqueue(c, key, spec):
    c.execute("INSERT OR IGNORE INTO img_queue(key,spec,created_at) VALUES(?,?,?)", (key, json.dumps(spec, ensure_ascii=False), time.strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()


def generate_refs(prompt, ref_files, env, conn, setting, w=SCENE_W, h=SCENE_H, fresh=False, queue=True):
    """One klein image with the characters' locked masters as references. At the daily cap the call is queued for tomorrow (not failed)."""
    refs = [pathlib.Path(f).read_bytes() for f in ref_files][:4]
    if not refs:
        return {"file": None, "provider": "svg", "reason": "no character masters yet"}
    key = ref_key(prompt, refs, w, h)
    hit = None if fresh else cache_file(key)
    if hit:
        return {"file": hit, "provider": "cloudflare-ref", "cached": True}
    st = status(env)
    if not st["cloudflare"]:
        return {"file": None, "provider": "svg", "reason": st["cloudflare_msg"]}
    c = conn()
    try:
        cost = neurons_for("klein", len(refs))
        if neurons_today(c) + cost > neuron_cap(setting):
            if queue:
                _enqueue(c, key, {"prompt": prompt, "refs": [str(f) for f in ref_files][:4], "w": w, "h": h})
            return {"file": None, "provider": "svg", "queued": True, "reason": "daily free limit reached: queued for tomorrow"}
        try:
            data = call_with_retry(lambda p, s_, e, r: klein(p, refs, w, h, e), prompt, 0, env, None)
        except ImageError as ex:
            return {"file": None, "provider": "svg", "reason": "cloudflare-ref: %s" % str(ex)[:160]}
        count_use(c, "cloudflare", cost)
    finally:
        c.close()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / (key + (".png" if data[:4] == b"\x89PNG" else ".jpg"))
    p.write_bytes(data)
    return {"file": p, "provider": "cloudflare-ref", "cached": False}


def drain_queue(env, conn, setting, limit=20):
    """Make the queued images when budget is available (called from the scheduler tick). The cached results make the next render free."""
    c = conn()
    try:
        rows = c.execute("SELECT id,spec FROM img_queue WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
    finally:
        c.close()
    done = 0
    for rid, spec in rows:
        sp = json.loads(spec)
        files = [f for f in sp["refs"] if pathlib.Path(f).is_file()]
        r = generate_refs(sp["prompt"], files, env, conn, setting, sp["w"], sp["h"], queue=False) if files else {"file": None, "reason": "reference image missing"}
        c = conn()
        try:
            if r.get("file"):
                c.execute("UPDATE img_queue SET status='done', done_at=? WHERE id=?", (time.strftime("%Y-%m-%d %H:%M:%S"), rid))
                done += 1
            elif "limit" in (r.get("reason") or ""):
                break                                                     # still no budget: try again on the next tick
            else:
                c.execute("UPDATE img_queue SET status='failed', error=? WHERE id=?", ((r.get("reason") or "")[:200], rid))
            c.commit()
        finally:
            c.close()
    return done


def ref_scene_prompt(slide, cast, scene_names):
    """Prompt for klein: starts with the 'same person' line, then the scene. cast items need role/name/master. Returns (prompt, [master files])."""
    by = {c["role"]: c for c in cast}
    chars = [by[ch["role"]] for ch in slide.get("characters", [])[:2] if by.get(ch.get("role")) and by[ch["role"]].get("master")]
    names = {r: by[r]["name"] for r in by if by[r]["name"]}
    desc = QUOTED.sub("", scrub(slide.get("description"), names))
    two = len(chars) == 2
    head = ("The first person is the same as in reference image 1, the second person is the same as in reference image 2: same faces, hair, clothes. " if two else SAME)
    pos = ("Positions: " + ", ".join("person %d on the %s" % (k + 1, ch.get("position") or "center") for k, ch in enumerate(slide["characters"][:2])) + ". ") if two else ""
    prompt = "%s%s. Setting: %s. %sScene: %s. Expressions: %s. %s" % (
        head, STYLE, scene_names.get(slide.get("scene"), "shop"), pos, desc or "the characters talk",
        ", ".join(ch.get("expression") or "neutral" for ch in slide.get("characters", [])[:2]), SAFETY)
    return prompt, [c["master"] for c in chars]


def scene_chain(slide, cast, scene_names, hq, env, conn, setting, off=False):
    """Scene picture: klein with locked masters, then (High quality) Gemini, then schnell from text, then SVG. Returns the usual result dict."""
    if off:
        return {"file": None, "provider": "svg", "reason": "AI illustrations switched off"}
    reasons = []
    rp, masters = ref_scene_prompt(slide, cast, scene_names)
    if masters and len(masters) == len(slide.get("characters", [])[:2]):
        r = generate_refs(rp, masters, env, conn, setting)
        if r["file"] or r.get("queued"):
            return r
        reasons.append(r["reason"])
    else:
        reasons.append("no locked master image for this character yet")
    prompt, seed = scene_prompt(slide, cast, scene_names)
    r = generate_chain(prompt, seed_for(seed, slide.get("img_v")), hq, env, conn, setting)
    if r["file"]:
        r["note"] = "; ".join(reasons)
        return r
    r["reason"] = "; ".join(reasons + [r.get("reason", "")])
    return r


def candidate_prompt(description, k):
    """Prompt for one of 3 master-image candidates (flux-1-schnell). Each differs a little so a missing seed field still gives three different people-poses."""
    pose = ("slight smile", "friendly laugh", "confident calm look")[k % 3]
    return "%s Character: %s. Expression: %s." % (CAND_STYLE, scrub(description), pose)
