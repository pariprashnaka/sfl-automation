"""Multi-provider AI router: ai(task, prompt, private=False).

Order (cloud first): groq, gemini, openrouter, then local Ollama last. Groq model size per task, circuit breaker and
24h cache for 'relevant' calls are configured in CONFIG below.
private=True -> local Ollama only (the user's own notes/questions never leave the machine).
Keys come from os.environ, or from a getter registered with set_key_getter (leads_app passes env_val).
Prompts, answers and keys are never logged.
"""
import os, re, time, sqlite3, threading, datetime
import requests

# All model names live here.
CONFIG = {
    "order": ["groq", "gemini", "openrouter", "ollama"],
    "groq": {"url": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY",
             "models": ["openai/gpt-oss-120b"],
             "sizes": {"small": "openai/gpt-oss-20b", "large": "openai/gpt-oss-120b"}},
    "openrouter": {"url": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY",
                   "models": ["google/gemma-4-31b-it:free"]},
    "gemini": {"key": "GEMINI_API_KEY", "models": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]},
    "ollama": {"url": "http://127.0.0.1:11434/api/generate", "model": "qwen2.5:3b"},
    "timeout": 60,
    "cool_first": 600, "cool_max": 7200,
    # Which Groq model size each task type uses (edit here). 'small' = higher-limit 8B for simple yes/no and extraction;
    # 'large' = drafts and anything ambiguous. Unlisted tasks use "default". Small falls back to large if it fails.
    "task_size": {"relevant": "small", "extract": "small", "profile": "small", "marketing_caption": "small", "default": "large"},
    "cache_tasks": ["relevant", "marketing_caption"],         # identical prompts for these tasks are answered from SQLite for cache_ttl seconds
    "cache_ttl": 86400,
    # Circuit breaker: quota/429 -> provider off until its daily reset (if the error says daily) or cool_quota seconds;
    # fail_limit failures in a row -> off for cool_quota seconds.
    "fail_limit": 5, "cool_quota": 1800,
    "reset_utc_offset_hours": {"groq": 0, "openrouter": 0, "gemini": -8},   # when each provider's day rolls over
}
DB = "D:/vault/automation/leads/leads.db"

_LOCK = threading.Lock()
_COOL = {}       # name -> {"until": ts, "step": seconds}; name is "provider" or "provider:model"
_DISABLED = {}   # "provider:model" -> day it was disabled (retired model)
_FAILS = {}      # provider -> consecutive failures
_QUOTA = {}      # provider -> True while tripped for quota (shown on the System page)
_KEYGET = [None]
_OVERRIDE = {}   # in-memory key overrides (tests only)


class AIError(Exception):
    pass


def set_key_getter(fn):
    _KEYGET[0] = fn


def _key(provider):
    name = CONFIG[provider]["key"]
    if name in _OVERRIDE:
        return _OVERRIDE[name]
    v = os.environ.get(name, "")
    if not v and _KEYGET[0]:
        try:
            v = _KEYGET[0](name) or ""
        except Exception:
            v = ""
    return v.strip()


def _today():
    return time.strftime("%Y-%m-%d")


def _cooling(name):
    with _LOCK:
        c = _COOL.get(name)
        return bool(c and c["until"] > time.time())


def _cool(name):
    with _LOCK:
        c = _COOL.get(name)
        step = CONFIG["cool_first"] if not c else min(c["step"] * 2, CONFIG["cool_max"])
        _COOL[name] = {"until": time.time() + step, "step": step}


def _ok(name):
    with _LOCK:
        _COOL.pop(name, None)
        _FAILS.pop(name.split(":", 1)[0], None)
        _QUOTA.pop(name.split(":", 1)[0], None)


def _secs_to_reset(provider):
    off = CONFIG["reset_utc_offset_hours"].get(provider, 0)
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=off)
    nxt = (now + datetime.timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return max(60, int((nxt - now).total_seconds()))


def _trip(provider, secs, quota=False):
    with _LOCK:
        _COOL[provider] = {"until": time.time() + secs, "step": secs}
        _FAILS.pop(provider, None)
        if quota:
            _QUOTA[provider] = True


def _is_quota(f):
    return f.status == 429 or bool(re.search(r"quota|rate.?limit|resource.?exhausted|too many requests", str(f), re.I))


def _is_daily(f):
    return bool(re.search(r"per day|daily|\bRPD\b|\bTPD\b|exceeded your current quota|free.models?.per.day", str(f), re.I))


def _note_fail(provider, f):
    """Circuit breaker. Returns True when the provider is now switched off."""
    if f.gone:
        return False
    if _is_quota(f):
        ra = f.retry_after
        short = bool(re.search(r"per minute|RPM|TPM|per second", str(f), re.I)) or (ra is not None and ra < 300)
        if _is_daily(f) and not (ra is not None and ra < 300):
            _trip(provider, _secs_to_reset(provider), quota=True)
        elif short:
            _trip(provider, max(ra or 0, 60), quota=True)
        else:
            _trip(provider, CONFIG["cool_quota"], quota=True)
        return True
    if f.status in (401, 403):
        _trip(provider, CONFIG["cool_quota"])
        return True
    with _LOCK:
        _FAILS[provider] = _FAILS.get(provider, 0) + 1
        n = _FAILS[provider]
    if n >= CONFIG["fail_limit"]:
        _trip(provider, CONFIG["cool_quota"])
        return True
    return False


_COLS_OK = [False]
_CACHE_OK = [False]


def _cache_key(task, prompt):
    import hashlib
    return hashlib.sha256((str(task) + "\n" + prompt).encode("utf-8", "ignore")).hexdigest()


def _cache_get(task, prompt):
    try:
        c = sqlite3.connect(DB, timeout=30)
        c.execute("CREATE TABLE IF NOT EXISTS ai_cache(h TEXT PRIMARY KEY, answer TEXT, ts REAL)")
        r = c.execute("SELECT answer FROM ai_cache WHERE h=? AND ts>?", (_cache_key(task, prompt), time.time() - CONFIG["cache_ttl"])).fetchone()
        c.close()
        return r[0] if r else None
    except Exception:
        return None


def _cache_put(task, prompt, answer):
    try:
        c = sqlite3.connect(DB, timeout=30)
        c.execute("CREATE TABLE IF NOT EXISTS ai_cache(h TEXT PRIMARY KEY, answer TEXT, ts REAL)")
        c.execute("DELETE FROM ai_cache WHERE ts<?", (time.time() - CONFIG["cache_ttl"],))
        c.execute("INSERT OR REPLACE INTO ai_cache(h,answer,ts) VALUES(?,?,?)", (_cache_key(task, prompt), answer, time.time()))
        c.commit()
        c.close()
    except Exception:
        pass


def _disabled(name):
    with _LOCK:
        return _DISABLED.get(name) == _today()


def _log(provider, model, ok, usage=None, error="", kind="call", task="", ms=0):
    try:
        u = usage or {}
        c = sqlite3.connect(DB, timeout=30)
        c.execute("CREATE TABLE IF NOT EXISTS gemini_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, kind TEXT, model TEXT, ok INTEGER, prompt_tokens INTEGER, output_tokens INTEGER, thought_tokens INTEGER, total_tokens INTEGER, error TEXT)")
        if not _COLS_OK[0]:
            cols = [r[1] for r in c.execute("PRAGMA table_info(gemini_calls)")]
            for col, typ in (("provider", "TEXT"), ("task", "TEXT"), ("latency_ms", "INTEGER")):
                if col not in cols:
                    try:
                        c.execute("ALTER TABLE gemini_calls ADD COLUMN %s %s" % (col, typ))
                    except Exception:
                        pass
            _COLS_OK[0] = True
        c.execute("INSERT INTO gemini_calls(ts,day,kind,model,ok,prompt_tokens,output_tokens,thought_tokens,total_tokens,error,provider,task,latency_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (time.strftime("%Y-%m-%d %H:%M:%S"), _today(), kind, model, 1 if ok else 0,
                   int(u.get("p") or 0), int(u.get("o") or 0), int(u.get("t") or 0), int(u.get("total") or 0),
                   str(error)[:200], provider, str(task)[:40], int(ms or 0)))
        c.commit()
        c.close()
    except Exception:
        pass


class _Fail(Exception):
    def __init__(self, msg, status=0, gone=False, retry_after=None):
        Exception.__init__(self, msg)
        self.status = status
        self.gone = gone
        self.retry_after = retry_after


def _retry_after(r):
    try:
        v = r.headers.get("retry-after")
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def _clean(t):
    return re.sub(r"<think>.*?</think>", "", t or "", flags=re.S).strip()


def _call_openai(provider, model, prompt):
    cfg = CONFIG[provider]
    try:
        r = requests.post(cfg["url"] + "/chat/completions",
                          headers={"Authorization": "Bearer " + _key(provider), "Content-Type": "application/json"},
                          json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
                          timeout=CONFIG["timeout"])
    except Exception as ex:
        raise _Fail("request failed: " + type(ex).__name__)
    try:
        j = r.json()
    except Exception:
        raise _Fail("unreadable reply HTTP " + str(r.status_code), r.status_code)
    if r.status_code != 200 or "error" in j:
        e = j.get("error") if isinstance(j, dict) else None
        msg = str((e.get("message") if isinstance(e, dict) else e) or j)[:150]
        gone = r.status_code == 404 or bool(re.search(r"no longer available|decommission|not found|does not exist|no endpoints", msg, re.I))
        raise _Fail("HTTP " + str(r.status_code) + " " + msg, r.status_code, gone and r.status_code in (400, 404), _retry_after(r))
    try:
        text = _clean(j["choices"][0]["message"]["content"])
    except Exception:
        raise _Fail("no answer in reply", r.status_code)
    if not text:
        raise _Fail("empty answer", r.status_code)
    u = j.get("usage") or {}
    return text, {"p": u.get("prompt_tokens"), "o": u.get("completion_tokens"), "total": u.get("total_tokens")}


def _call_gemini(model, prompt):
    try:
        r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                          headers={"x-goog-api-key": _key("gemini"), "Content-Type": "application/json"},
                          json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}},
                          timeout=CONFIG["timeout"])
    except Exception as ex:
        raise _Fail("request failed: " + type(ex).__name__)
    try:
        j = r.json()
    except Exception:
        raise _Fail("unreadable reply HTTP " + str(r.status_code), r.status_code)
    if "error" in j:
        msg = str(j["error"].get("message", j["error"]))[:150]
        gone = r.status_code == 404 or "no longer available" in msg.lower()
        raise _Fail("HTTP " + str(r.status_code) + " " + msg, r.status_code, gone, _retry_after(r))
    um = j.get("usageMetadata") or {}
    usage = {"p": um.get("promptTokenCount"), "o": um.get("candidatesTokenCount"),
             "t": um.get("thoughtsTokenCount"), "total": um.get("totalTokenCount")}
    try:
        text = j["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        raise _Fail("no answer in reply", r.status_code)
    return text, usage


def _call_ollama(prompt, ctx):
    cfg = CONFIG["ollama"]
    try:
        r = requests.post(cfg["url"], json={"model": cfg["model"], "prompt": prompt, "stream": False,
                                            "options": {"num_ctx": ctx, "temperature": 0.2}}, timeout=900)
        j = r.json()
    except Exception as ex:
        raise _Fail("local AI unreachable: " + type(ex).__name__)
    if "response" not in j:
        raise _Fail("local AI error: " + str(j.get("error", j))[:120])
    return j["response"].strip(), {}


def _models_for(provider, size="large"):
    ms = list(CONFIG[provider]["models"])
    if provider == "groq":
        sz = CONFIG["groq"]["sizes"]
        ms = [sz["small"], sz["large"]] if size == "small" else [sz["large"]]
    if provider == "gemini":
        pref = os.environ.get("GEMINI_MODEL", "")
        if not pref and _KEYGET[0]:
            try:
                pref = _KEYGET[0]("GEMINI_MODEL") or ""
            except Exception:
                pref = ""
        if pref and pref not in ms:
            ms.insert(0, pref)
    return ms


def _handle_fail(provider, model, f):
    """Retired model -> off for the day; rate limit/overload/timeout -> cool-down; else cool the model briefly too."""
    name = provider + ":" + model
    if f.gone:
        with _LOCK:
            _DISABLED[name] = _today()
    else:
        _cool(name)


def ai(task, prompt, private=False, ctx=2048, order=None, only=None, big=False):
    """Return the model's answer text. Raises AIError if every allowed provider failed.
    task: short label, 'digest...' tasks are logged as kind 'digest'.
    only: restrict to one provider name (used for comparison tests)."""
    kind = "digest" if str(task).startswith("digest") else "call"
    chain = ["ollama"] if private else list(order or CONFIG["order"])
    if only:
        chain = [only]
    ts = CONFIG["task_size"]
    size = "large" if big else ts.get(str(task), ts.get("default", "large"))
    use_cache = (not private) and str(task) in CONFIG["cache_tasks"]
    if use_cache:
        hit = _cache_get(task, prompt)
        if hit is not None:
            _log("cache", "-", True, kind="cache", task=task)
            return hit
    tried_cloud = False
    last = ""
    for provider in chain:
        if provider == "ollama":
            t0 = time.time()
            try:
                text, usage = _call_ollama(prompt, ctx)
                _log("ollama", CONFIG["ollama"]["model"], True, usage, kind=kind, task=task, ms=(time.time() - t0) * 1000)
                if tried_cloud:
                    _log("ollama", CONFIG["ollama"]["model"], False, error="cloud providers failed; used local model", kind="fallback", task=task)
                if use_cache:
                    _cache_put(task, prompt, text)
                return text
            except _Fail as f:
                _log("ollama", CONFIG["ollama"]["model"], False, error=str(f), kind=kind, task=task, ms=(time.time() - t0) * 1000)
                last = str(f)
                continue
        if not _key(provider) or _cooling(provider):
            continue
        tried_cloud = True
        for m in _models_for(provider, size):
            if _disabled(provider + ":" + m) or _cooling(provider + ":" + m):
                continue
            if _cooling(provider):      # breaker tripped by an earlier model in this loop
                break
            t0 = time.time()
            try:
                if provider == "gemini":
                    text, usage = _call_gemini(m, prompt)
                else:
                    text, usage = _call_openai(provider, m, prompt)
                if not text:
                    raise _Fail("empty answer")
                _ok(provider + ":" + m)
                _log(provider, m, True, usage, kind=kind, task=task, ms=(time.time() - t0) * 1000)
                if use_cache:
                    _cache_put(task, prompt, text)
                return text
            except _Fail as f:
                last = provider + ": " + str(f)
                _log(provider, m, False, error=str(f), kind=kind, task=task, ms=(time.time() - t0) * 1000)
                if f.gone:
                    _handle_fail(provider, m, f)      # retired model: off for the day, try the next one
                    continue
                if _note_fail(provider, f):
                    break                             # provider switched off: go to the next provider at once
    raise AIError("all AI providers failed" + (": " + last if last else ""))


def status():
    """Per-provider state for the System page."""
    now = time.time()
    out = {}
    for p in ("groq", "gemini", "openrouter", "ollama"):
        info = {"key": p == "ollama" or bool(_key(p)), "cool": [], "disabled": [], "quota": bool(_QUOTA.get(p) and _cooling(p))}
        out[p] = info
    with _LOCK:
        for n, c in _COOL.items():
            if c["until"] > now:
                p = n.split(":", 1)[0]
                if p in out:
                    out[p]["cool"].append((n, int((c["until"] - now) / 60) + 1))
        for n, d in _DISABLED.items():
            if d == _today():
                p = n.split(":", 1)[0]
                if p in out:
                    out[p]["disabled"].append(n)
    return out
