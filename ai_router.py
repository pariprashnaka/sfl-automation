"""Multi-provider AI router: ai(task, prompt, private=False).

Order (cloud first): groq, gemini, openrouter, then local Ollama last.
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
             "models": ["openai/gpt-oss-120b"]},
    "openrouter": {"url": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY",
                   "models": ["google/gemma-4-31b-it:free"]},
    "gemini": {"key": "GEMINI_API_KEY", "models": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]},
    "ollama": {"url": "http://127.0.0.1:11434/api/generate", "model": "qwen2.5:3b"},
    "timeout": 60,
    "cool_first": 600, "cool_max": 7200,
}
DB = "D:/vault/automation/leads/leads.db"

_LOCK = threading.Lock()
_COOL = {}       # name -> {"until": ts, "step": seconds}; name is "provider" or "provider:model"
_DISABLED = {}   # "provider:model" -> day it was disabled (retired model)
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


def _disabled(name):
    with _LOCK:
        return _DISABLED.get(name) == _today()


def _log(provider, model, ok, usage=None, error="", kind="call"):
    try:
        u = usage or {}
        c = sqlite3.connect(DB, timeout=30)
        c.execute("CREATE TABLE IF NOT EXISTS gemini_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, kind TEXT, model TEXT, ok INTEGER, prompt_tokens INTEGER, output_tokens INTEGER, thought_tokens INTEGER, total_tokens INTEGER, error TEXT)")
        cols = [r[1] for r in c.execute("PRAGMA table_info(gemini_calls)")]
        if "provider" not in cols:
            try:
                c.execute("ALTER TABLE gemini_calls ADD COLUMN provider TEXT")
            except Exception:
                pass
        c.execute("INSERT INTO gemini_calls(ts,day,kind,model,ok,prompt_tokens,output_tokens,thought_tokens,total_tokens,error,provider) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (time.strftime("%Y-%m-%d %H:%M:%S"), _today(), kind, model, 1 if ok else 0,
                   int(u.get("p") or 0), int(u.get("o") or 0), int(u.get("t") or 0), int(u.get("total") or 0),
                   str(error)[:200], provider))
        c.commit()
        c.close()
    except Exception:
        pass


class _Fail(Exception):
    def __init__(self, msg, status=0, gone=False):
        Exception.__init__(self, msg)
        self.status = status
        self.gone = gone


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
        raise _Fail("HTTP " + str(r.status_code) + " " + msg, r.status_code, gone and r.status_code in (400, 404))
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
        raise _Fail("HTTP " + str(r.status_code) + " " + msg, r.status_code, gone)
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


def _models_for(provider):
    ms = list(CONFIG[provider]["models"])
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


def ai(task, prompt, private=False, ctx=2048, order=None, only=None):
    """Return the model's answer text. Raises AIError if every allowed provider failed.
    task: short label, 'digest...' tasks are logged as kind 'digest'.
    only: restrict to one provider name (used for comparison tests)."""
    kind = "digest" if str(task).startswith("digest") else "call"
    chain = ["ollama"] if private else list(order or CONFIG["order"])
    if only:
        chain = [only]
    tried_cloud = False
    last = ""
    for provider in chain:
        if provider == "ollama":
            try:
                text, usage = _call_ollama(prompt, ctx)
                _log("ollama", CONFIG["ollama"]["model"], True, usage, kind=kind)
                if tried_cloud:
                    _log("ollama", CONFIG["ollama"]["model"], False, error="cloud providers failed; used local model", kind="fallback")
                return text
            except _Fail as f:
                _log("ollama", CONFIG["ollama"]["model"], False, error=str(f), kind=kind)
                last = str(f)
                continue
        if not _key(provider) or _cooling(provider):
            continue
        tried_cloud = True
        models = [m for m in _models_for(provider) if not _disabled(provider + ":" + m) and not _cooling(provider + ":" + m)]
        for m in models:
            try:
                if provider == "gemini":
                    text, usage = _call_gemini(m, prompt)
                else:
                    text, usage = _call_openai(provider, m, prompt)
                if not text:
                    raise _Fail("empty answer")
                _ok(provider + ":" + m)
                _log(provider, m, True, usage, kind=kind)
                return text
            except _Fail as f:
                last = provider + ": " + str(f)
                _log(provider, m, False, error=str(f), kind=kind)
                if provider != "gemini" and f.status in (401, 403):
                    _cool(provider)        # bad key: whole provider rests
                    break
                _handle_fail(provider, m, f)
                if provider != "gemini":   # single-model providers: provider-level rest too
                    if not f.gone:
                        _cool(provider)
    raise AIError("all AI providers failed" + (": " + last if last else ""))


def status():
    """Per-provider state for the System page."""
    now = time.time()
    out = {}
    for p in ("groq", "gemini", "openrouter", "ollama"):
        info = {"key": p == "ollama" or bool(_key(p)), "cool": [], "disabled": []}
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
