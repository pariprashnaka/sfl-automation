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
SAFETY = ("Original fictional characters only, not resembling any real person, celebrity, brand mascot or known cartoon character. "
          "No text, no letters, no words, no numbers, no logos, no watermarks, no captions, no speech bubbles; any signboard or paper stays blank. "
          "Characters in the lower two thirds of the frame, calm plain background in the upper third.")

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


def count_use(c, provider):
    c.execute("INSERT INTO imagegen_usage(day,provider,n) VALUES(?,?,1) ON CONFLICT(day,provider) DO UPDATE SET n=n+1", (today(), provider))
    c.commit()


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
        if used_today(c, provider) >= cap(provider, setting):
            return {"file": None, "provider": "svg", "reason": "%s daily limit reached (%d images)" % (provider, cap(provider, setting))}
        try:
            data = call_with_retry(fn, prompt, seed, env, ref)
        except ImageError as ex:
            return {"file": None, "provider": "svg", "reason": "%s: %s" % (provider, str(ex)[:160])}
        count_use(c, provider)
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
