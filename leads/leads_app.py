import sqlite3, threading, re, time, csv, io, urllib.parse, pathlib, subprocess, shutil, datetime, json, html
import requests
from flask import Flask, request, redirect, render_template_string, Response, jsonify
from ddgs import DDGS

BASE = pathlib.Path(r"D:\vault\automation\leads")
BASE.mkdir(parents=True, exist_ok=True)
DB = str(BASE / "leads.db")
MODEL = "qwen2.5:3b"
OLLAMA = "http://127.0.0.1:11434/api/generate"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
SKIP = ("justdial.com", "indiamart.com", "sulekha.com", "tradeindia.com", "facebook.com", "instagram.com",
        "youtube.com", "linkedin.com", "wikipedia.org", "twitter.com", "x.com", "pinterest.com", "quora.com")
STATUSES = ["New", "Contacted", "Replied", "Interested", "Demo booked", "Won", "Not interested", "Wrong number"]
PHONE = re.compile(r"(?<!\d)(?:\+?91|0)?([6-9]\d{9})(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+\.[A-Za-z0-9.\-]+")

app = Flask(__name__)
LOGS, RUNNING = {}, set()
LOCK = threading.Lock()

def conn():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, queries TEXT, location TEXT, pitch TEXT, language TEXT, max_leads INTEGER, created TEXT);
    CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER, title TEXT, url TEXT, domain TEXT, snippet TEXT, phones TEXT, emails TEXT, status TEXT DEFAULT 'New', notes TEXT DEFAULT '', draft TEXT DEFAULT '', created TEXT);
    """)
    c.commit()
    c.close()
    ensure_businesses()

def log(cid, msg):
    with LOCK:
        LOGS.setdefault(cid, []).append(time.strftime("%H:%M:%S") + "  " + msg)
        LOGS[cid] = LOGS[cid][-300:]

def domain_of(url):
    d = urllib.parse.urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d

def grab(url):
    try:
        r = requests.get(url, headers=UA, timeout=12)
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""

# Only real Indian mobile layouts: 10 digits, or grouped 5-5, 3-3-4, 4-3-3 with one space or dash. Not part of a longer token (file names, ids, prices, coordinates).
PHONE_TXT = re.compile(r"(?<![\w.\-/,])(?:\+?91[\s\-]?|0)?[6-9](?:\d{9}|\d{4}[\s\-]\d{5}|\d{2}[\s\-]\d{3}[\s\-]\d{4}|\d{3}[\s\-]\d{3}[\s\-]\d{3})(?![\w])(?![\-.,/]\d)")
TAG = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>""")
JUNK_BLOCKS = re.compile(r"(?is)<!--.*?-->|<(script|style|svg|noscript|template|header|footer|nav)\b.*?</\1>")

TRUNK0 = set()   # 10-digit numbers seen written with a leading 0 on a source page

def visible_contacts(page):
    # Phones and emails only from visible text, tel:/mailto: links and JSON-LD of the page body.
    # Scripts, styles, SVG paths and the site's own header/footer/nav (its helpline) are ignored.
    ld = " ".join(re.findall(r"(?is)<script[^>]*ld\+json[^>]*>(.*?)</script>", page))
    body = JUNK_BLOCKS.sub(" ", page)
    text = TAG.sub(" ", body)
    links = " ".join(re.findall(r"""(?i)href=["'](?:tel|mailto):([^"']+)""", body))
    phones, emails = [], []
    for src in (links, text):
        for m in PHONE_TXT.finditer(src):
            d = p10(m.group(0))
            if d and re.sub(r"\D", "", m.group(0)).startswith("0") and d[0] != "9":
                TRUNK0.add(d)   # written with a leading 0 (STD code): the master list marks it as a landline
            if d and d not in phones:
                phones.append(d)
    for e in EMAIL.findall(links + " " + text + " " + ld):
        e = e.lower().rstrip(".")
        if e.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")) or "example" in e or "sentry" in e:
            continue
        if e not in emails:
            emails.append(e)
    return phones, emails

def scan(url):
    page = grab(url)
    extra = ""
    m = re.search(r'href=["\']([^"\']*contact[^"\']*)["\']', page, re.I)
    if m and len([x for x in urllib.parse.urlparse(url).path.split("/") if x]) <= 1:
        # only from a homepage-like URL: a contact page linked from a deep directory profile belongs to the directory
        cu = urllib.parse.urljoin(url, m.group(1))
        if domain_of(cu) == domain_of(url):
            extra = grab(cu)
    phones, emails = visible_contacts(page)
    p2, e2 = visible_contacts(extra)
    phones += [p for p in p2 if p not in phones]
    emails += [e for e in e2 if e not in emails]
    return phones[:3], emails[:2]

AGG_DOMAINS = ("exportersindia.com", "indianyellowpages.com", "aajjo.com", "lotwaala.com", "dial4trade.com", "datagemba.com")
CATEGORY = re.compile(r"\b(suppliers?|manufacturers?|dealers?|exporters?|wholesalers?|distributors?|directory|list of|top \d+|best|products?|companies)\b", re.I)

GENERIC_WORDS = {"store", "stores", "shop", "shops", "hardware", "bhandar", "traders", "trader", "center", "centre", "house", "and", "in", "varanasi"}

def core_words(name):
    return set(re.findall(r"[a-z0-9]+", name.lower())) - GENERIC_WORDS - NOISE

def find_phones(cid, name, aliases):
    # Looks for the shop's own listing on non-blocked web pages (title must contain the shop's name, page must mention the city).
    # Returns (phones, note). Phones are returned only when exactly one such listing has phones; otherwise the candidates go in the note.
    core = core_words(name)
    if not core or not aliases:
        return [], "", ""
    city_l = [a.lower() for a in aliases if a]
    found, seen = [], set()
    for q in (name + " " + aliases[0] + " contact number", name + " " + aliases[0]):
        for r in web(cid, q, "in-en"):
            url = r.get("href") or ""
            d = domain_of(url)
            title = r.get("title") or ""
            tw = set(re.findall(r"[a-z0-9]+", title.lower()))
            if not url or url in seen or blocked(d) or d in AGG_DOMAINS or len(core & tw) / len(core) < 0.6:
                continue
            seen.add(url)
            if len(seen) > 5:
                break
            text, html = fetch_page(url)
            if not any(c in (title + " " + text).lower() for c in city_l):
                continue
            phones, _ = visible_contacts(html)
            if phones:
                found.append((d, url, title.strip()[:70], phones[:2]))
        time.sleep(1)
        if len(seen) > 5:
            break
    if len(found) == 1:
        d, url, title, phones = found[0]
        return phones, " | phone from " + d + " listing '" + title + "' (" + url + "), check it is the same shop", url
    if len(found) > 1:
        return [], " | several listings with this name, no phone attached: " + "; ".join(
            "%s '%s' %s" % (d, t, ",".join(p)) for d, u, t, p in found[:3]), ""
    return [], "", ""

def category_page(title, d):
    # A directory category page is not one business: never save it as a lead (extraction from it is tried separately).
    return any(d == x or d.endswith("." + x) for x in AGG_DOMAINS) or bool(CATEGORY.search(title or ""))

def ask(prompt):
    try:
        r = requests.post(OLLAMA, json={"model": MODEL, "prompt": prompt, "stream": False,
                                        "options": {"num_ctx": 2048}}, timeout=300)
        return r.json()["response"].strip()
    except Exception as e:
        return "(Could not reach the local AI: " + str(e)[:80] + ")"

def draft(lead, camp):
    prompt = ("You write short first messages for a small Indian software company.\n"
              "What we sell: " + camp["pitch"] + "\n"
              "Lead: " + (lead["title"] or "") + "\n"
              "About them: " + (lead["snippet"] or "") + "\n"
              "Location: " + camp["location"] + "\n"
              "Language: " + camp["language"] + "\n\n"
              "Write a polite WhatsApp message under 70 words. Greet them, say in one line what we offer and one benefit, "
              "and ask if they would like a 10-minute demo. No emojis, no pricing, and do not invent facts about their business. "
              "End with: Reply STOP and we will not message again.")
    return ask(prompt)

def short(term):
    if len(term.split()) <= 5:
        return term
    out = ask("Rewrite as a short web search phrase of 2 to 4 words. Output only the phrase, no quotes.\n\n" + term)
    out = out.strip().strip('"').splitlines()[0].strip() if out.strip() else ""
    if 0 < len(out) < 60 and not out.startswith("("):
        return out
    return " ".join(term.split()[:4])

def find(cid, term, location):
    s = short(term)
    if s != term:
        log(cid, "  simplified your request to: " + s)
    parts = [x.strip() for x in location.split(",")]
    tries = [s + " " + location, s + " " + ", ".join(parts[:2]), s + " " + parts[0]]
    home = "in-en" if location.endswith("India") else "wt-wt"
    for q in dict.fromkeys(tries):
        for region in dict.fromkeys([home, "wt-wt"]):
            log(cid, "Searching the web for: " + q + " [" + region + "]")
            try:
                res = DDGS().text(q, region=region, max_results=30)
                if res:
                    return res
            except Exception as e:
                log(cid, "  no results: " + str(e)[:80])
    return []

BLOCK_EXTRA = ("justdial.com", "indiamart.com", "sulekha.com", "facebook.com", "instagram.com", "youtube.com",
               "twitter.com", "x.com", "linkedin.com", "pinterest.com", "wikipedia.org", "quora.com", "reddit.com")
LISTING = re.compile(r"\b(top|best|list|lists|directory|near me|nearby|address|contact|suppliers?|manufacturers?|dealers?|wholesalers?|exporters?|distributors?|stockists?|companies|shops|stores|\d{1,3}\+?\s+\w+)\b", re.I)
NOISE = {"the", "and", "of", "pvt", "ltd", "private", "limited", "co"}
ENTITY_TAGS = {"hospital": ["amenity=hospital", "amenity=clinic", "healthcare=hospital"],
               "clinic": ["amenity=clinic", "amenity=doctors", "healthcare=clinic"],
               "nursing": ["amenity=clinic", "amenity=hospital"],
               "pharmacy": ["amenity=pharmacy"], "medical": ["amenity=pharmacy"],
               "lab": ["healthcare=laboratory"], "diagnostic": ["healthcare=laboratory", "healthcare=centre"],
               "school": ["amenity=school"], "college": ["amenity=college"], "bank": ["amenity=bank"],
               "restaurant": ["amenity=restaurant"], "hotel": ["tourism=hotel"],
               "kirana": ["shop=convenience", "shop=supermarket"], "grocery": ["shop=convenience", "shop=supermarket"],
               "hardware": ["shop=hardware"], "jewel": ["shop=jewelry"], "garment": ["shop=clothes"]}
# Shops that OpenStreetMap contributors tag only by name (e.g. "Bharat Bartan Wale"): matched on name, shop-tagged places only.
ENTITY_NAMES = {"utensil": "utensil|bartan|bartaan|vessel|cookware|kitchenware|crockery",
                "kitchenware": "utensil|bartan|bartaan|vessel|cookware|kitchenware|crockery",
                "cookware": "utensil|bartan|bartaan|vessel|cookware|kitchenware|crockery",
                "bartan": "utensil|bartan|bartaan|vessel|cookware|kitchenware|crockery"}

def blocked(d):
    return any(d == x or d.endswith("." + x) for x in tuple(SKIP) + BLOCK_EXTRA)

def tok(name):
    return frozenset(w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in NOISE)

def p10(x):
    d = re.sub(r"\D", "", str(x))
    return d[-10:] if len(d) >= 10 else ""

def fetch_page(url, cap=30000):
    try:
        h = UA if isinstance(UA, dict) else {"User-Agent": UA}
        r = requests.get(url, headers=h, timeout=20)
        if r.status_code != 200:
            return "", ""
        t = ""
        try:
            import trafilatura
            t = trafilatura.extract(r.text, include_tables=True, favor_recall=True) or ""
        except Exception:
            t = ""
        if not t:
            t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
        return t[:cap], r.text
    except Exception:
        return "", ""

def next_link(html, base):
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html or "", re.I | re.S):
        attrs, label = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip().lower()
        if re.search(r"rel=[\"']next", attrs, re.I) or label in ("next", "next page", "next \u00bb", "next \u203a", "\u00bb", "\u203a", ">"):
            h = re.search(r"href=[\"']([^\"']+)", attrs, re.I)
            if h and not h.group(1).startswith(("#", "javascript")):
                u = urllib.parse.urljoin(base, h.group(1))
                if domain_of(u) == domain_of(base):
                    return u
    return ""

def parse_json(txt, kind):
    try:
        a = txt.index(kind[0])
        b = txt.rindex(kind[1])
        return json.loads(txt[a:b + 1])
    except Exception:
        return None

def _plan_leads(cid, camp, term):
    city = camp["location"].split(",")[0].strip()
    prompt = ("Plan a business lead search. Output ONLY JSON like "
              "{\"entity\":\"hospital\",\"terms\":[\"hospital\",\"nursing home\"],\"places\":[\"locality one\"],\"osm\":[\"amenity=hospital\"],\"aliases\":[\"spelling\"]}.\n"
              "entity: the kind of business wanted, 1 or 2 words.\n"
              "terms: up to 8 search phrases (synonyms and sub-types of exactly this business, 1 to 3 words each, no location; do not broaden to other product types).\n"
              "places: up to 10 well-known localities, areas or roads inside that city (not other cities).\n"
              "osm: up to 4 OpenStreetMap tags as key=value that match this kind of business.\n"
              "aliases: other common spellings of the city name, up to 3 (for example Arrah for Ara).\n\n"
              "Requirement: " + term + "\nLocation: " + camp["location"])
    plan = None
    try:
        plan = parse_json(ask_research(prompt, 2048), "{}")
    except Exception as ex:
        log(cid, "  planner unavailable: " + str(ex)[:80])
    if not isinstance(plan, dict):
        plan = {}
    entity = str(plan.get("entity") or short(term))[:40]
    terms = [str(x).strip() for x in (plan.get("terms") or []) if str(x).strip()][:8]
    if not terms:
        terms = [short(term)]
    if entity.lower() not in [t.lower() for t in terms]:
        terms.insert(0, entity)
    places = [str(x).strip() for x in (plan.get("places") or []) if str(x).strip()][:10]
    # map tags and name words: union of the cached AI profile, the planner's own tags and the hard-coded lists
    prof_tags, prof_names = entity_profile(cid, entity)
    blob = (entity + " " + " ".join(terms)).lower()
    raw = list(prof_tags) + [str(t).strip() for t in (plan.get("osm") or [])]
    for k, v in ENTITY_TAGS.items():
        if k in blob:
            raw += v
    tags = []
    for t in raw:
        if re.fullmatch(r"[a-z_:]+=[A-Za-z0-9_:]+", t) and tuple(t.split("=", 1)) not in tags:
            tags.append(tuple(t.split("=", 1)))
    aliases = [city] + [str(x).strip() for x in (plan.get("aliases") or []) if str(x).strip()][:3]
    nm = []
    for n in list(prof_names or []) + [w for k, v in ENTITY_NAMES.items() if k in blob for w in v.split("|")]:
        n = str(n).strip()
        if n and n.lower() not in [x.lower() for x in nm]:
            nm.append(n)
    if nm:
        tags = tags[:5] + [("name~", "|".join(nm))]
    return city, entity, terms, places, tags[:6], aliases

def geo_bbox(loc):
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search", params={"q": loc, "format": "json", "limit": 1},
                         headers={"User-Agent": "SFL-CommandCentre/1.0"}, timeout=30)
        s_, n_, w_, e_ = [float(x) for x in r.json()[0]["boundingbox"]]
        return s_, w_, n_, e_
    except Exception:
        return None

def osm_fetch(cid, loc, tags):
    bb = geo_bbox(loc)
    if not bb:
        log(cid, "  map: could not locate " + loc)
        return []
    s_, w_, n_, e_ = bb
    if (n_ - s_) > 1.0 or (e_ - w_) > 1.0:
        log(cid, "  map: area too large for one map query, skipped")
        return []
    body = "".join(('nwr["name"~"%s",i]["shop"](%s,%s,%s,%s);' if k == "name~" else 'nwr["%s"="%s"](%s,%s,%s,%s);') %
                   ((v, s_, w_, n_, e_) if k == "name~" else (k, v, s_, w_, n_, e_)) for k, v in tags)
    q = "[out:json][timeout:60];(" + body + ");out center tags;"
    # same area and tags asked within 24 hours: reuse the answer instead of hitting the public servers again
    cache_f = BASE / "osm_cache.json"
    key = loc + "|" + ";".join(sorted(k + "=" + v for k, v in tags))
    try:
        cache = json.loads(cache_f.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(key)
    if hit and time.time() - hit[0] < 86400:
        log(cid, "  map: using answer saved in the last 24 hours (" + str(len(hit[1])) + " places)")
        return hit[1]
    mirrors = ("https://overpass.openstreetmap.fr/api/interpreter", "https://overpass-api.de/api/interpreter",
               "https://overpass.private.coffee/api/interpreter", "https://overpass.kumi.systems/api/interpreter")
    t0 = time.time()
    for rnd in range(2):
        for ep in mirrors:
            left = 240 - (time.time() - t0)
            if left < 15:
                break
            try:
                r = requests.post(ep, data={"data": q}, headers={"User-Agent": "SFL-CommandCentre/1.0", "Accept": "application/json"}, timeout=min(70, left))
                if r.status_code == 200:
                    els = r.json().get("elements", [])
                    cache[key] = [time.time(), els]
                    try:
                        cache_f.write_text(json.dumps(cache), encoding="utf-8")
                    except Exception:
                        pass
                    return els
                log(cid, "  map server " + ep.split("/")[2] + " answered " + str(r.status_code) + ", trying the next one")
            except Exception as e:
                log(cid, "  map server " + ep.split("/")[2] + " failed: " + type(e).__name__)
        if time.time() - t0 > 225:
            break
        if rnd == 0:
            log(cid, "  map: all servers busy, waiting 10s before one more round")
            time.sleep(10)
    log(cid, "  map: no server answered in time")
    return []

def web(cid, q, home):
    for attempt in range(3):
        for region in dict.fromkeys([home, "wt-wt"]):
            try:
                res = DDGS().text(q, region=region, max_results=30)
                if res:
                    return res
            except Exception:
                pass
            time.sleep(2)
        time.sleep(8 * (attempt + 1))
    log(cid, "  search gave nothing after 3 tries (network or rate limit)")
    return []

def extract_businesses(entity, city, text):
    size = 9000 if env_val("GEMINI_API_KEY") else 3500
    out = []
    for k in range(0, min(len(text), size * 3), size):
        p = ("Extract every distinct " + entity + " listed in the page text below that is located in or near " + city + ". "
             "Output ONLY a JSON array like [{\"name\":\"\",\"phone\":\"\",\"address\":\"\",\"website\":\"\"}]. "
             "Rules: copy names and phone numbers exactly as written; leave a field empty if it is not on the page; "
             "never invent anything; a business is a shop, company or dealer, so product names, categories and menu items are NOT businesses; ignore ads and items in other cities. If none, output [].\n\nPage text:\n" + text[k:k + size])
        try:
            arr = parse_json(ask_research(p, 4096), "[]")
        except Exception:
            arr = None
        time.sleep(3)
        if isinstance(arr, list):
            out += [x for x in arr if isinstance(x, dict)]
    return out

def clean_phones(raw, pagedigits):
    res = []
    for part in re.split(r"[,/;]|\bor\b", str(raw or "")):
        dg = p10(part)
        if dg and dg in pagedigits and dg not in res:
            res.append(dg)
    return res

JUNK_MAIL = re.compile(r"(@\d|\.(png|jpg|jpeg|gif|svg|webp|css|js)$|^(feedback|noreply|no-reply|webmaster|privacy|abuse|postmaster)@|@(example|domain|business|yourdomain|email|sentry|wixpress)\.|^(name|yourname|user|username|test)@)", re.I)

def clean_emails(lst):
    out = []
    for e in lst:
        e = str(e).strip().lower()
        if "@" in e and re.search(r"\.[a-z]{2,}$", e) and not JUNK_MAIL.search(e) and e not in out:
            out.append(e)
    return out[:3]

def relevant(entity, title, body):
    try:
        out = ask_research("Answer only YES or NO. Is this web page from a business that sells, makes or provides: " + entity +
                           "? Page title: " + title[:120] + " | Description: " + body[:300], 1024)
        return not out.strip().upper().startswith("NO")
    except Exception:
        return True

def run_campaign(cid):
    if cid in RUNNING:
        return
    RUNNING.add(cid)
    total = 0
    t0 = time.time()
    active = 0.0
    last = time.time()
    try:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        except Exception:
            pass
        c = conn()
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        rows = c.execute("SELECT title, domain, url, phones FROM leads WHERE campaign_id=?", (cid,)).fetchall()
        c.close()
        seen_dom = {r["domain"] for r in rows}
        seen_url = {r["url"] for r in rows}
        known = [(tok(r["title"] or ""), {p10(x) for x in (r["phones"] or "").split(",") if p10(x)}) for r in rows]
        A, B = [], []
        limit = camp["max_leads"]
        home = "in-en" if camp["location"].strip().endswith("India") else "wt-wt"
        log(cid, "=== Search started: " + camp["name"] + " ===")

        mctx = {}   # current search context for the master list (city, state, entity, places, aliases)

        def dup(tk, phs):
            for t, p in known:
                if phs & p:
                    return True
                if tk and tk == t and (not phs or not p):
                    return True
            return False

        def save(name, url, dom, snippet, phones, emails, notes="", biz=None):
            nonlocal total
            c2 = conn()
            c2.execute("INSERT INTO leads(campaign_id,title,url,domain,snippet,phones,emails,notes,created) VALUES(?,?,?,?,?,?,?,?,?)",
                       (cid, name, url, dom, snippet[:500], ",".join(phones), ",".join(emails), notes, time.strftime("%Y-%m-%d %H:%M")))
            c2.commit()
            c2.close()
            total += 1
            known.append((tok(name), {p10(x) for x in phones if p10(x)}))
            log(cid, "    saved: " + name[:50] + " | " + (", ".join(phones) or "no phone") + " | " + (", ".join(emails) or "no email"))
            try:
                res = master_save(cid, mctx, name, url, snippet, phones, emails, biz or {})
                if res:
                    log(cid, "      master list: " + {"new": "new business", "merged": "merged into existing #%s (matched by %s)" % (res[1], res[2]),
                                                      "review": "sent to review (%s)" % res[2]}[res[0]])
            except Exception as ex:
                log(cid, "      master list error: " + str(ex)[:120])

        for term in [t.strip() for t in camp["queries"].splitlines() if t.strip()]:
            city, entity, terms, places, tags, aliases = plan_leads(cid, camp, term)
            _parts = [x.strip() for x in camp["location"].split(",")]
            mctx.update(city=city, entity=entity, places=places, aliases=aliases, state=_parts[1] if len(_parts) >= 3 else "")
            log(cid, "Understood: looking for '" + entity + "' in " + city + " | search terms: " + ", ".join(terms) +
                (" | areas: " + ", ".join(places) if places else ""))

            # ---- layer 1: OpenStreetMap
            if tags:
                log(cid, "Map layer: asking OpenStreetMap for " + ", ".join(k + "=" + v for k, v in tags))
                els = osm_fetch(cid, camp["location"], tags)
                log(cid, "  map returned " + str(len(els)) + " places")
                enrich = 0
                found_phone = 0
                for el in els:
                    if total >= limit:
                        break
                    tg = el.get("tags", {})
                    name = (tg.get("name") or tg.get("name:en") or "").strip()
                    if not name:
                        continue
                    phs = []
                    for f in ("phone", "contact:phone", "contact:mobile", "mobile"):
                        for part in re.split(r"[;,/]", tg.get(f, "")):
                            if p10(part) and p10(part) not in phs:
                                phs.append(p10(part))
                    A.append((tok(name), set(phs)))
                    if dup(tok(name), set(phs)):
                        continue
                    site = (tg.get("website") or tg.get("contact:website") or "").strip()
                    emails = [tg[f] for f in ("email", "contact:email") if tg.get(f)][:2]
                    if site.startswith("http") and not phs and enrich < 60 and not blocked(domain_of(site)):
                        enrich += 1
                        try:
                            p2, e2 = scan(site)
                            phs = [p10(x) for x in p2 if p10(x)][:3]
                            emails = emails or list(e2)[:3]
                        except Exception:
                            pass
                    addr = " ".join(tg.get("addr:" + k, "") for k in ("housenumber", "street", "suburb", "city")).strip()
                    note, psrc = "", ""
                    if not phs and found_phone < 25:
                        found_phone += 1
                        try:
                            phs, note, psrc = find_phones(cid, name, aliases)
                        except Exception:
                            phs, note, psrc = [], "", ""
                    osm_url = "https://www.openstreetmap.org/" + str(el.get("type")) + "/" + str(el.get("id"))
                    ctr = el.get("center") or {}
                    save(name, site or osm_url,
                         domain_of(site) if site.startswith("http") else "openstreetmap.org",
                         addr + " | source: OpenStreetMap" + note, phs, emails,
                         biz={"address": addr, "lat": el.get("lat", ctr.get("lat")), "lon": el.get("lon", ctr.get("lon")), "src": osm_url,
                              "phone_src": psrc if psrc else "", "website": site, "city_ok": True, "locality": tg.get("addr:suburb") or tg.get("addr:neighbourhood") or "",
                              "pincode": tg.get("addr:postcode") or "", "district": tg.get("addr:district") or "", "state": tg.get("addr:state") or "",
                              "tags": [k + "=" + tg[k] for k in ("shop", "amenity", "craft", "office", "healthcare", "tourism") if tg.get(k)]})

            # ---- layer 2: web searches and directory pages
            parts_ = [x.strip() for x in camp["location"].split(",")]
            locq = city + (" " + parts_[1] if len(parts_) > 1 else "")
            qs = []
            for t in terms:
                qs += [t + " in " + locq, t + " " + locq + " contact number", t + " " + locq + " list"]
            for pl in places[:8]:
                qs.append(entity + " " + pl + " " + locq)
            zero_run = 0
            for q in list(dict.fromkeys(qs))[:45]:
                if total >= limit:
                    break
                now = time.time()
                active += min(now - last, 300)
                last = now
                if active > 7200:
                    log(cid, "Stopping: 2 hour working-time budget reached")
                    break
                before = total
                log(cid, "Searching: " + q + "  (" + str(total) + "/" + str(limit) + " leads)")
                results = web(cid, q, home)
                for r in results:
                    if total >= limit:
                        break
                    url = r.get("href") or ""
                    if not url or url in seen_url:
                        continue
                    seen_url.add(url)
                    d = domain_of(url)
                    if blocked(d):
                        continue
                    title = r.get("title") or d
                    blob = (title + " " + (r.get("body") or "") + " " + url).lower()
                    tld = re.search(r"\.([a-z]{2})$", d)
                    if not any(a.lower() in blob for a in aliases) or (home == "in-en" and tld and tld.group(1) != "in"):
                        continue
                    if LISTING.search(title + " " + url):
                        page_url, added, valid = url, 0, 0
                        for pg in range(6):
                            text, html = fetch_page(page_url)
                            if len(text) < 200:
                                log(cid, "    " + d + ": page text only " + str(len(text)) + " chars, nothing to extract")
                                break
                            pagedigits = re.sub(r"\D", "", text)
                            low = text.lower()
                            items = extract_businesses(entity, city, text)
                            log(cid, "    " + d + ": " + str(len(text)) + " chars of text, AI listed " + str(len(items)) + " business(es)")
                            for it in items:
                                name = str(it.get("name") or "").strip()
                                if len(name) < 3 or len(name) > 120 or name.lower()[:12] not in low:
                                    continue
                                phs = clean_phones(it.get("phone"), pagedigits)
                                valid += 1
                                tk = tok(name)
                                if not any(t == tk or (p & set(phs)) for t, p in B):
                                    B.append((tk, set(phs)))
                                if total >= limit or dup(tk, set(phs)):
                                    continue
                                site = str(it.get("website") or "").strip()
                                emails = []
                                if not phs and not site.startswith("http") and len(items) == 1 and not category_page(title, d):
                                    # a page about one single business: its own visible phones and emails belong to that business
                                    pp, ee = visible_contacts(html)
                                    phs = pp[:3]
                                    emails = clean_emails(ee)
                                    if phs and dup(tk, set(phs)):
                                        continue
                                if site.startswith("http") and not phs and not blocked(domain_of(site)):
                                    try:
                                        p2, e2 = scan(site)
                                        phs = [p10(x) for x in p2 if p10(x)][:3]
                                        emails = list(e2)[:3]
                                    except Exception:
                                        pass
                                # relevance: ask the AI using the page's own words about this business (copied from the page, nothing invented)
                                pos = low.find(name.lower()[:12])
                                # listings put the product details just before the seller's name, so take text on both sides
                                ctx = text[max(pos - 300, 0):max(pos, 0) + 150] if len(items) > 1 else text[:400]
                                if not relevant(entity, name, ctx):
                                    log(cid, "    skipped (not " + entity + "): " + name[:50])
                                    continue
                                addr = re.sub(r"\s+", " ", str(it.get("address") or "")).strip()
                                if addr and addr.lower()[:20] not in re.sub(r"\s+", " ", low):
                                    addr = ""  # keep an address only if the page really contains it
                                if not phs and not site.startswith("http"):
                                    # flagged lead: name and address copied from the directory page, no phone published there
                                    save(name, page_url, d, addr + " | source: " + d + " | listed without a phone: " + page_url, [], emails,
                                         "NO PHONE: directory listing only", biz={"address": addr, "src": page_url, "website": site})
                                else:
                                    save(name, site or page_url, domain_of(site) if site.startswith("http") else d,
                                         addr + " | source: " + d, phs, emails, biz={"address": addr, "src": page_url, "website": site})
                                added += 1
                            nxt = next_link(html, page_url)
                            if not nxt or nxt in seen_url or total >= limit:
                                break
                            seen_url.add(nxt)
                            page_url = nxt
                            time.sleep(1)
                        if valid == 0 and d not in seen_dom and category_page(title, d):
                            log(cid, "    " + d + ": directory category page, not saved as a lead")
                        elif valid == 0 and d not in seen_dom:
                            phones, emails = scan(url)
                            emails = clean_emails(emails)
                            if (phones or emails) and relevant(entity, title, r.get("body") or ""):
                                seen_dom.add(d)
                                save(title, url, d, r.get("body") or "", list(phones), emails,
                                     biz={"from_title": True, "src": url, "website": homepage_only(url), "city_ok": True})
                        log(cid, "  " + d + ": " + str(added) + " new businesses from list pages")
                    else:
                        if d in seen_dom:
                            continue
                        if category_page(title, d):
                            log(cid, "    " + d + ": directory category page, not saved as a lead")
                            continue
                        seen_dom.add(d)
                        phones, emails = scan(url)
                        emails = clean_emails(emails)
                        if (phones or emails) and relevant(entity, title, r.get("body") or ""):
                            save(title, url, d, r.get("body") or "", list(phones), emails,
                                 biz={"from_title": True, "src": url, "website": homepage_only(url), "city_ok": True})
                time.sleep(1)
                if total != before:
                    zero_run = 0
                elif results:  # a failed or empty search (rate limit, network) says nothing about saturation, so it is not counted
                    zero_run += 1
                if zero_run >= 12:
                    log(cid, "Saturated: the last 12 searches found nothing new, stopping.")
                    break

            # ---- coverage estimate
            def matched(b):
                bt, bp = b
                for at, ap in A:
                    if bp & ap:
                        return True
                    u = bt | at
                    if u and len(bt & at) / len(u) >= 0.6:
                        return True
                return False
            m = sum(1 for b in B if matched(b))
            log(cid, "Coverage: map layer " + str(len(A)) + " places, web directories " + str(len(B)) + ", found in both " + str(m))
            cov_pct = None
            if m >= 3 and A and B:
                est = len(A) * len(B) / m
                union = len(A) + len(B) - m
                cov_pct = min(100, int(100 * union / est))
                log(cid, "  Rough estimate: about " + str(int(est)) + " exist online; you have covered about " +
                    str(cov_pct) + "%. Optimistic, because the two sources are not fully independent.")
            else:
                log(cid, "  Not enough overlap between sources to estimate coverage. Treat the lead count as a minimum.")
            log_coverage(entity, city, total, cov_pct)
    except Exception as e:
        log(cid, "ERROR: " + str(e)[:200])
    finally:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        except Exception:
            pass
        bc = BIZ_COUNTS.pop(cid, None)
        if bc:
            log(cid, "Master list: " + str(bc["new"]) + " new, " + str(bc["merged"]) + " merged into existing, " + str(bc["review"]) + " sent to review")
        log(cid, "=== Search finished: " + str(total) + " new leads ===")
        RUNNING.discard(cid)

def draft_all(cid):
    if cid in RUNNING:
        return
    RUNNING.add(cid)
    try:
        c = conn()
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        rows = c.execute("SELECT * FROM leads WHERE campaign_id=? AND (draft IS NULL OR draft='')", (cid,)).fetchall()
        c.close()
        log(cid, "=== Writing " + str(len(rows)) + " first messages with the local AI ===")
        for l in rows:
            log(cid, "  writing message for " + (l["title"] or "")[:60])
            d = draft(l, camp)
            c = conn()
            c.execute("UPDATE leads SET draft=? WHERE id=?", (d, l["id"]))
            c.commit()
            c.close()
        log(cid, "=== Drafts finished ===")
    except Exception as e:
        log(cid, "ERROR: " + str(e)[:200])
    finally:
        RUNNING.discard(cid)

HEAD = """<!doctype html><html><head><meta charset='utf-8'><title>Lead Campaigns</title>
<style>
body{font-family:Arial,sans-serif;margin:0;background:#f5f6f8;color:#222}
nav{position:fixed;top:0;left:0;bottom:0;width:200px;background:#1f2937;padding:16px 0;overflow:auto}nav .brand{font-weight:bold;color:#fff;padding:0 18px 14px;font-size:15px}nav a,nav span{display:block;padding:10px 18px;color:#cbd5e1;text-decoration:none;font-size:14px}nav a:hover{background:#374151;color:#fff}nav a.on{background:#2563eb;color:#fff}nav span.soon{color:#6b7280}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}.tile{background:#fff;border-radius:8px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}.tile b{display:block;font-size:28px}.tile small{color:#666}
main{max-width:1250px;margin:20px 0 20px 200px;padding:0 24px}
.card{background:#fff;border-radius:8px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
input,textarea,select{width:100%;padding:8px;margin:4px 0 10px;box-sizing:border-box;border:1px solid #ccc;border-radius:4px;font-family:inherit}
button,.btn{background:#2563eb;color:#fff;border:0;padding:8px 14px;border-radius:4px;cursor:pointer;text-decoration:none;display:inline-block;font-size:14px;margin-right:6px}
.grey{background:#6b7280}
table{width:100%;border-collapse:collapse}
th,td{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;vertical-align:top;font-size:14px}
pre{background:#111;color:#9fe29f;padding:10px;max-height:260px;overflow:auto;font-size:13px;border-radius:6px;white-space:pre-wrap}
small{color:#666}
</style></head><body><nav><div class='brand'>Command Centre</div><a href='/' class='{{"on" if request.path=="/"}}'>Home</a><a href='/campaigns' class='{{"on" if request.path.startswith("/campaign")}}'>Campaigns</a><a href='/leads' class='{{"on" if request.path=="/leads"}}'>All leads</a><a href='/businesses' class='{{"on" if request.path.startswith("/businesses")}}'>Businesses</a><a href='/digests' class='{{"on" if request.path.startswith("/digests")}}'>Digests</a><a href='/notes' class='{{'on' if request.path=='/notes'}}'>Notes</a><a href='/research' class='{{'on' if request.path.startswith('/research')}}'>Research</a><a href='/testing' class='{{'on' if request.path.startswith('/testing')}}'>Testing</a><a href='/system' class='{{"on" if request.path=="/system"}}'>System</a></nav><main>"""

HOME = HEAD + """
<div class='card'><h2>New campaign</h2>
<form method='post' action='/campaign'>
<label>Campaign name</label><input name='name' required placeholder='e.g. Pathology labs in Buxar'>
<label>What are you looking for? (one per line if several)</label><textarea name='queries' rows='3' required placeholder='pathology lab&#10;diagnostic centre'></textarea>
<label>Country</label><select name='country' id='country' onchange='fillStates()'></select><label>State / region</label><select name='state' id='state'></select><label>City / town (optional)</label><input name='city' placeholder='Buxar'>
<label>What you are offering (used to write the first message)</label><textarea name='pitch' rows='3' required placeholder='LabFlow LIMS: lab software for billing, reports and patient records'></textarea>
<label>Message language</label><select name='language'><option>English</option><option>Hinglish</option><option>Hindi</option></select>
<label>Max leads</label><input name='max_leads' type='number' value='40' min='5' max='1000'>
<button>Create and start search</button></form></div>
<div class='card'><h2>Your campaigns</h2>
{% if camps %}<table><tr><th>Name</th><th>Area</th><th>Leads</th><th>Created</th></tr>
{% for c in camps %}<tr><td><a href='/campaign/{{c.id}}'>{{c.name}}</a></td><td>{{c.location}}</td><td>{{c.n}}</td><td>{{c.created}}</td></tr>{% endfor %}</table>
{% else %}<p>No campaigns yet.</p>{% endif %}</div>
<script>
var C=["India","United States","United Kingdom","United Arab Emirates","Canada","Australia","Nepal","Bangladesh","Sri Lanka","Singapore","Saudi Arabia","Qatar","Oman","Kuwait","Bahrain","Malaysia","Indonesia","Thailand","Philippines","Vietnam","Pakistan","Afghanistan","Bhutan","Maldives","Germany","France","Italy","Spain","Netherlands","Ireland","Switzerland","Sweden","Norway","Denmark","Turkey","Egypt","Kenya","Nigeria","South Africa","Ghana","Ethiopia","Tanzania","Mauritius","Brazil","Mexico","Argentina","Japan","South Korea","China","Hong Kong","New Zealand","Russia"];
var S={
"India":["Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh","Goa","Gujarat","Haryana","Himachal Pradesh","Jharkhand","Karnataka","Kerala","Madhya Pradesh","Maharashtra","Manipur","Meghalaya","Mizoram","Nagaland","Odisha","Punjab","Rajasthan","Sikkim","Tamil Nadu","Telangana","Tripura","Uttar Pradesh","Uttarakhand","West Bengal","Andaman and Nicobar Islands","Chandigarh","Dadra and Nagar Haveli and Daman and Diu","Delhi","Jammu and Kashmir","Ladakh","Lakshadweep","Puducherry"],
"United States":["Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut","Delaware","Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan","Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada","New Hampshire","New Jersey","New Mexico","New York","North Carolina","North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island","South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont","Virginia","Washington","West Virginia","Wisconsin","Wyoming","District of Columbia"],
"United Kingdom":["England","Scotland","Wales","Northern Ireland"],
"United Arab Emirates":["Abu Dhabi","Dubai","Sharjah","Ajman","Umm Al Quwain","Ras Al Khaimah","Fujairah"],
"Canada":["Alberta","British Columbia","Manitoba","New Brunswick","Newfoundland and Labrador","Nova Scotia","Ontario","Prince Edward Island","Quebec","Saskatchewan"],
"Australia":["New South Wales","Victoria","Queensland","Western Australia","South Australia","Tasmania","Australian Capital Territory","Northern Territory"]
};
function fillStates(){
  var c=document.getElementById('country').value,l=S[c]||[];
  document.getElementById('state').innerHTML='<option value="">(any / not applicable)</option>'+l.map(function(x){return '<option>'+x+'</option>';}).join('');
}
document.getElementById('country').innerHTML=C.map(function(x){return '<option'+(x=='India'?' selected':'')+'>'+x+'</option>';}).join('');
fillStates();
</script></main></body></html>"""

CAMP = HEAD + """
{% if pre.get('state')=='ask' %}<div class='card' style='border-left:4px solid #2563eb'>
<h3>You already have {{pre.n}} businesses for this search.</h3>
<p>Last updated {{pre.last}}. They are saved in your master list.</p>
<form method='post' action='/campaign/{{c.id}}/prestart'><button name='choice' value='use'>Use these</button>
<button name='choice' value='more' class='grey'>Search for more</button></form></div>
{% elif pre.get('state')=='used' %}<div class='card'><p>Using {{pre.n}} existing businesses; no new search was run. <a href='/businesses?campaign={{c.id}}'>View them</a></p></div>
{% elif pre.get('state')=='more' %}<div class='card'><p>Searching for more. New finds are merged into your {{pre.n}} existing businesses. <a href='/businesses?campaign={{c.id}}'>View businesses</a></p></div>{% endif %}
<div class='card'>
<h2>{{c.name}}</h2>
<p><b>Area:</b> {{c.location}} &nbsp; <b>Searches:</b> {{qline}}</p>
<p><b>Offer:</b> {{c.pitch}}</p>
<button onclick="runAgain()">Search again</button>
<a class='btn grey' href='/campaign/{{c.id}}/export.csv'>Export CSV</a><form method='post' action='/campaign/{{c.id}}/import' enctype='multipart/form-data' style='display:inline-block;margin-left:12px'><input type='file' name='file' accept='.csv' required style='width:auto;display:inline-block;margin:0'><button>Import CSV</button></form>
</div>
<div class='card'><h3>Live activity <small id='st'></small></h3><pre id='log'>Waiting...</pre></div>
<div class='card'><h3>Leads ({{total}})</h3>
<form method='get' style='display:flex;gap:8px;margin-bottom:10px'>
<input name='q' value='{{q}}' placeholder='Search name, phone, email, website, notes' style='margin:0'>
<select name='status' style='margin:0;width:190px'><option value=''>All statuses</option>{% for st in statuses %}<option {{'selected' if st==status}}>{{st}}</option>{% endfor %}</select>
<select name='has' style='margin:0;width:150px'><option value=''>Any contact</option><option value='phone' {{'selected' if has=='phone'}}>Has phone</option><option value='email' {{'selected' if has=='email'}}>Has email</option></select>
<button style='margin:0'>Filter</button></form>
{% set pager %}<p>Page {{page}} of {{pages}} &nbsp; {% if page>1 %}<a class='btn grey' href='?{{qs}}&page={{page-1}}'>Prev</a>{% endif %}{% if page<pages %}<a class='btn grey' href='?{{qs}}&page={{page+1}}'>Next</a>{% endif %}</p>{% endset %}
{{pager|safe}}
<table><tr><th style='width:4%'>#</th><th style='width:30%'>Business</th><th style='width:13%'>Phone</th><th style='width:20%'>Email</th><th style='width:13%'>Status</th><th>Notes</th></tr>
{% for l in leads %}<tr>
<td>{{loop.index + (page-1)*50}}</td>
<td><b><a href='{{l.url}}' target='_blank'>{{(l.title or l.domain)[:70]}}</a></b><br><small>{{l.domain}}</small></td>
<td>{% for p in l.phones.split(',') if p %}<div>{{p}}</div>{% endfor %}</td>
<td>{% for e in l.emails.split(',') if e %}<div><small>{{e}}</small></div>{% endfor %}</td>
<td><select id='s{{l.id}}' onchange="save({{l.id}})">{% for st in statuses %}<option {{'selected' if st==l.status}}>{{st}}</option>{% endfor %}</select></td>
<td><input id='n{{l.id}}' value='{{l.notes}}' placeholder='notes' onchange="save({{l.id}})" style='margin:0'></td>
</tr>{% endfor %}</table>
{{pager|safe}}</div>
<script>
var CID={{c.id}};
function save(id){
  var body={status:document.getElementById('s'+id).value,notes:document.getElementById('n'+id).value};
  fetch('/api/lead/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
}
function runAgain(){fetch('/api/run/'+CID,{method:'POST'});setTimeout(poll,800);}
function draftAll(){fetch('/api/draftall/'+CID,{method:'POST'});setTimeout(poll,800);}
function poll(){
  fetch('/api/log/'+CID).then(function(r){return r.json();}).then(function(j){
    var el=document.getElementById('log');var atEnd=el.scrollTop+el.clientHeight>=el.scrollHeight-20;
    el.textContent=j.lines.length?j.lines.join('\\n'):'Nothing running right now.';
    if(atEnd){el.scrollTop=el.scrollHeight;}
    document.getElementById('st').textContent=j.running?'(running...)':'(idle)';
    if(!j.running&&window.wasRunning){location.reload();}
    window.wasRunning=j.running;
  });
}
setInterval(poll,2000);poll();
</script></main></body></html>"""

def status_info():
    info = {}
    rep_dir = pathlib.Path(r"D:\vault\outputs\reports")
    files = sorted(rep_dir.glob("digest-*.html")) if rep_dir.exists() else []
    info["digest_today"] = any(f.name == "digest-%s.html" % time.strftime("%Y-%m-%d") for f in files)
    info["digest_last"] = files[-1].name[7:17] if files else ""
    try:
        info["ollama"] = [m["name"] for m in requests.get("http://127.0.0.1:11434/api/tags", timeout=2).json().get("models", [])]
    except Exception:
        info["ollama"] = None
    try:
        out = subprocess.run(["schtasks", "/query", "/tn", "MorningDigest", "/fo", "LIST"], capture_output=True, text=True, timeout=8).stdout
        nxt = re.search(r"Next Run Time:\s*(.+)", out)
        st = re.search(r"Status:\s*(.+)", out)
        info["task"] = (st.group(1).strip() + ", next run " + nxt.group(1).strip()) if st and nxt else None
    except Exception:
        info["task"] = None
    try:
        du = shutil.disk_usage("D:\\")
        info["disk"] = "%.0f GB free of %.0f GB" % (du.free / 1e9, du.total / 1e9)
    except Exception:
        info["disk"] = "unknown"
    return info

DASH = HEAD + """
<h2 style='margin-top:0'>Home</h2>
<div class='tiles'>
<div class='tile'><b>{{n_camp}}</b><small>Campaigns</small></div>
<div class='tile'><b>{{n_leads}}</b><small>Total leads</small></div>
<div class='tile'><b>{{new_week}}</b><small>New in last 7 days</small></div>
<div class='tile'><b>{{worked}}</b><small>Contacted or beyond</small></div>
</div>
<div class='card'><h3>System</h3><table>
<tr><td style='width:220px'>Morning digest</td><td>{% if digest_today %}Ready for today{% else %}Not run today{% endif %}{% if digest_last %} (last file: {{digest_last}}){% endif %}</td></tr>
<tr><td>Local AI (Ollama)</td><td>{% if ollama is none %}Not reachable - start Ollama{% else %}Running: {{ollama|join(', ')}}{% endif %}</td></tr>
<tr><td>Scheduled digest task</td><td>{% if task %}{{task}}{% else %}Not scheduled yet{% endif %}</td></tr>
<tr><td>Disk D:</td><td>{{disk}}</td></tr>
<tr><td>Running now</td><td>{% if running %}{% for r in running %}<a href='/campaign/{{r.id}}'>{{r.name}}</a> {% endfor %}{% else %}Nothing running{% endif %}</td></tr>
</table></div>
<div class='card'><h3>Reminders</h3>
{% if reminders %}<table>{% for n in reminders %}<tr><td style='width:130px'><b style='color:{{'#b91c1c' if n.state=='overdue' else '#222'}}'>{{n.pretty}}</b><br><small>{{n.state}}</small></td><td style='white-space:pre-wrap'>{{n.text[:200]}}</td><td style='width:70px'><a href='/notes'>Open</a></td></tr>{% endfor %}</table>
{% else %}<p>No reminders due in the next 14 days. <a href='/notes'>Add a note</a>.</p>{% endif %}</div>
<div class='card'><h3>Pipeline</h3><table><tr>{% for st in statuses %}<th>{{st}}</th>{% endfor %}</tr>
<tr>{% for st in statuses %}<td>{{counts.get(st,0)}}</td>{% endfor %}</tr></table></div>
<div class='card'><h3>Latest leads</h3>
{% if recent %}<table><tr><th>Business</th><th>Campaign</th><th>Phone</th><th>Status</th></tr>
{% for l in recent %}<tr><td>{{(l.title or l.domain)[:70]}}</td><td><a href='/campaign/{{l.campaign_id}}'>{{l.camp}}</a></td><td>{{l.phones.split(',')[0]}}</td><td>{{l.status}}</td></tr>{% endfor %}</table>
{% else %}<p>No leads yet. <a href='/campaigns'>Start a campaign</a>.</p>{% endif %}</div>
</main></body></html>"""

ALL = HEAD + """
<div class='card'><h3>All leads ({{total}})</h3>
<form method='get' style='display:flex;gap:8px;margin-bottom:10px'>
<input name='q' value='{{q}}' placeholder='Search name, phone, email, website, notes' style='margin:0'>
<select name='camp' style='margin:0;width:200px'><option value=''>All campaigns</option>{% for c in camps %}<option value='{{c.id}}' {{'selected' if c.id|string==camp}}>{{c.name}}</option>{% endfor %}</select>
<select name='status' style='margin:0;width:160px'><option value=''>All statuses</option>{% for st in statuses %}<option {{'selected' if st==status}}>{{st}}</option>{% endfor %}</select>
<select name='has' style='margin:0;width:140px'><option value=''>Any contact</option><option value='phone' {{'selected' if has=='phone'}}>Has phone</option><option value='email' {{'selected' if has=='email'}}>Has email</option></select>
<button style='margin:0'>Filter</button></form>
{% set pager %}<p>Page {{page}} of {{pages}} &nbsp; {% if page>1 %}<a class='btn grey' href='?{{qs}}&page={{page-1}}'>Prev</a>{% endif %}{% if page<pages %}<a class='btn grey' href='?{{qs}}&page={{page+1}}'>Next</a>{% endif %}</p>{% endset %}
{{pager|safe}}
<table><tr><th style='width:4%'>#</th><th style='width:26%'>Business</th><th style='width:14%'>Campaign</th><th style='width:12%'>Phone</th><th style='width:18%'>Email</th><th style='width:12%'>Status</th><th>Notes</th></tr>
{% for l in leads %}<tr>
<td>{{loop.index + (page-1)*50}}</td>
<td><b><a href='{{l.url}}' target='_blank'>{{(l.title or l.domain)[:70]}}</a></b><br><small>{{l.domain}}</small></td>
<td><a href='/campaign/{{l.campaign_id}}'>{{l.camp}}</a></td>
<td>{% for p in l.phones.split(',') if p %}<div>{{p}}</div>{% endfor %}</td>
<td>{% for e in l.emails.split(',') if e %}<div><small>{{e}}</small></div>{% endfor %}</td>
<td><select id='s{{l.id}}' onchange="save({{l.id}})">{% for st in statuses %}<option {{'selected' if st==l.status}}>{{st}}</option>{% endfor %}</select></td>
<td><input id='n{{l.id}}' value='{{l.notes}}' placeholder='notes' onchange="save({{l.id}})" style='margin:0'></td>
</tr>{% endfor %}</table>
{{pager|safe}}</div>
<script>
function save(id){
  var body={status:document.getElementById('s'+id).value,notes:document.getElementById('n'+id).value};
  fetch('/api/lead/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
}
</script></main></body></html>"""

@app.route("/")
def dashboard():
    ensure_notes()
    c = conn()
    week = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    n_camp = c.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    n_leads = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    new_week = c.execute("SELECT COUNT(*) FROM leads WHERE created>=?", (week,)).fetchone()[0]
    worked = c.execute("SELECT COUNT(*) FROM leads WHERE status<>'New'").fetchone()[0]
    counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status")}
    recent = c.execute("SELECT l.*, c.name AS camp FROM leads l JOIN campaigns c ON c.id=l.campaign_id ORDER BY l.id DESC LIMIT 8").fetchall()
    running = [r for r in c.execute("SELECT id, name FROM campaigns") if r["id"] in RUNNING]
    horizon = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()
    reminders = [note_view(r) for r in c.execute("SELECT * FROM notes WHERE done=0 AND remind_on<>'' AND remind_on<=? ORDER BY remind_on LIMIT 10", (horizon,))]
    c.close()
    return render_template_string(DASH, reminders=reminders, n_camp=n_camp, n_leads=n_leads, new_week=new_week, worked=worked, counts=counts,
                                  recent=recent, running=running, statuses=STATUSES, **status_info())

@app.route("/leads")
def all_leads():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    has = request.args.get("has", "")
    camp = request.args.get("camp", "")
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    per = 50
    where, args = ["1=1"], []
    if q:
        where.append("(l.title LIKE ? OR l.phones LIKE ? OR l.emails LIKE ? OR l.notes LIKE ? OR l.domain LIKE ?)")
        args += ["%" + q + "%"] * 5
    if camp:
        where.append("l.campaign_id=?")
        args.append(camp)
    if status:
        where.append("l.status=?")
        args.append(status)
    if has == "phone":
        where.append("l.phones<>''")
    if has == "email":
        where.append("l.emails<>''")
    w = " AND ".join(where)
    c = conn()
    total = c.execute("SELECT COUNT(*) FROM leads l WHERE " + w, args).fetchone()[0]
    pages = max((total + per - 1) // per, 1)
    page = min(page, pages)
    leads = c.execute("SELECT l.*, c.name AS camp FROM leads l JOIN campaigns c ON c.id=l.campaign_id WHERE " + w +
                      " ORDER BY l.id DESC LIMIT ? OFFSET ?", args + [per, (page - 1) * per]).fetchall()
    camps = c.execute("SELECT id, name FROM campaigns ORDER BY id DESC").fetchall()
    c.close()
    qs = urllib.parse.urlencode({"q": q, "status": status, "has": has, "camp": camp})
    return render_template_string(ALL, leads=leads, camps=camps, statuses=STATUSES, total=total, page=page, pages=pages,
                                  q=q, status=status, has=has, camp=camp, qs=qs)

REPORTS = pathlib.Path(r"D:\vault\outputs\reports")
DLOG = pathlib.Path(r"D:\vault\outputs\logs\digest.log")
DIGEST = {"proc": None}

DIG = HEAD + """
<div class='card'><h3>Daily schedule <small id='sst'></small></h3>
<p id='sinfo'>Checking...</p>
<label>Run every day at</label>
<input type='time' id='stime' value='06:30' style='width:160px;display:inline-block'>
<button onclick="saveSched()">Save schedule</button>
<button class='grey' id='stog' onclick="toggleSched()" style='display:none'></button>
<p><small id='smsg'></small></p></div>
<div class='card'><h3>Morning digest <small id='dst'></small></h3>
<button onclick="runDigest()">Run digest now</button>
<pre id='dlog' style='margin-top:10px'>Nothing running.</pre></div>
<div class='card'><h3>Saved digests</h3>
{% if files %}
<p>{% for f in files %}<a class='btn {{'' if f==sel else 'grey'}}' href='?d={{f}}' style='margin-bottom:6px'>{{f[7:17]}}</a>{% endfor %}</p>
<p><a href='/digests/view/{{sel}}' target='_blank'>Open full page</a></p>
<iframe src='/digests/view/{{sel}}' style='width:100%;height:720px;border:1px solid #ddd;border-radius:6px;background:#fff'></iframe>
{% else %}<p>No digests yet. Click Run digest now.</p>{% endif %}</div>
<script>
var schedOn=false;
function loadSched(){
  fetch('/api/digest/schedule').then(function(r){return r.json();}).then(function(j){
    var info=document.getElementById('sinfo'),tog=document.getElementById('stog');
    if(!j.exists){info.textContent=j.error?('Could not read schedule: '+j.error):'Not scheduled yet. Pick a time and click Save schedule.';tog.style.display='none';return;}
    schedOn=(j.state!=='Disabled');
    info.textContent=(schedOn?'On':'Paused')+'. Next run: '+(j.next||'none')+'. Last run: '+(j.last||'never');
    if(j.time){document.getElementById('stime').value=j.time.slice(11,16);}
    tog.style.display='inline-block';tog.textContent=schedOn?'Pause':'Resume';
  });
}
function schedPost(url,body){
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.json();}).then(function(j){
    document.getElementById('smsg').textContent=j.ok?'Saved.':(j.error||'Failed.');loadSched();});
}
function saveSched(){schedPost('/api/digest/schedule',{time:document.getElementById('stime').value});}
function toggleSched(){schedPost('/api/digest/schedule/toggle',{enabled:!schedOn});}
loadSched();
function runDigest(){fetch('/api/digest/run',{method:'POST'});setTimeout(poll,800);}
function poll(){
  fetch('/api/digest/log').then(function(r){return r.json();}).then(function(j){
    var el=document.getElementById('dlog');var atEnd=el.scrollTop+el.clientHeight>=el.scrollHeight-20;
    el.textContent=j.lines.length?j.lines.join('\\n'):'Nothing running.';
    if(atEnd){el.scrollTop=el.scrollHeight;}
    document.getElementById('dst').textContent=j.running?'(running...)':'(idle)';
    if(!j.running&&window.wasRunning){location.reload();}
    window.wasRunning=j.running;
  });
}
setInterval(poll,2000);poll();
</script></main></body></html>"""

def digest_files():
    return sorted((f.name for f in REPORTS.glob("digest-*.html")), reverse=True) if REPORTS.exists() else []

@app.route("/digests")
def digests():
    files = digest_files()
    sel = request.args.get("d", "")
    if sel not in files:
        sel = files[0] if files else ""
    return render_template_string(DIG, files=files, sel=sel)

@app.route("/digests/view/<name>")
def digest_view(name):
    if not re.fullmatch(r"digest-\d{4}-\d{2}-\d{2}\.html", name) or not (REPORTS / name).exists():
        return "Not found", 404
    return Response((REPORTS / name).read_text(encoding="utf-8", errors="replace"), mimetype="text/html")

def run_ps(script):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference='Stop'; " + script],
                       capture_output=True, text=True, timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()

SCHED_INFO = r"""
$t = Get-ScheduledTask -TaskName MorningDigest -ErrorAction SilentlyContinue
if ($t) {
  $i = Get-ScheduledTaskInfo -TaskName MorningDigest
  $last = ''
  if ($i.LastRunTime.Year -gt 2000) { $last = [string]$i.LastRunTime }
  [pscustomobject]@{exists=$true; state=[string]$t.State; time=[string]$t.Triggers[0].StartBoundary; next=[string]$i.NextRunTime; last=$last} | ConvertTo-Json
} else { '{"exists": false}' }
"""

SCHED_SET = r"""
$a = New-ScheduledTaskAction -Execute "D:\vault\automation\.venv\Scripts\python.exe" -Argument "D:\vault\automation\research\morning_digest.py" -WorkingDirectory "D:\vault\automation\research"
$t = New-ScheduledTaskTrigger -Daily -At ([datetime]'2000-01-01T__T__:00')
$s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "MorningDigest" -Action $a -Trigger $t -Settings $s -Force | Out-Null
"""

@app.route("/api/digest/schedule")
def sched_get():
    try:
        code, out, err = run_ps(SCHED_INFO)
        if out:
            return jsonify(json.loads(out))
        return jsonify(exists=False, error=err[:200])
    except Exception as ex:
        return jsonify(exists=False, error=str(ex)[:200])

@app.route("/api/digest/schedule", methods=["POST"])
def sched_set():
    t = (request.get_json(force=True).get("time") or "").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        return jsonify(ok=False, error="Enter a time like 06:30"), 400
    try:
        code, out, err = run_ps(SCHED_SET.replace("__T__", t))
        if code != 0:
            return jsonify(ok=False, error=(err or out)[:300])
        return jsonify(ok=True)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex)[:200])

@app.route("/api/digest/schedule/toggle", methods=["POST"])
def sched_toggle():
    en = bool(request.get_json(force=True).get("enabled"))
    try:
        code, out, err = run_ps(("Enable" if en else "Disable") + "-ScheduledTask -TaskName MorningDigest | Out-Null")
        if code != 0:
            return jsonify(ok=False, error=(err or out)[:300])
        return jsonify(ok=True)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex)[:200])

@app.route("/api/digest/run", methods=["POST"])
def digest_run():
    pr = DIGEST["proc"]
    if pr is None or pr.poll() is not None:
        DIGEST["proc"] = subprocess.Popen(
            [r"D:\vault\automation\.venv\Scripts\python.exe", r"D:\vault\automation\research\morning_digest.py"],
            cwd=r"D:\vault\automation\research", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return jsonify(ok=True)

@app.route("/api/digest/log")
def digest_log():
    pr = DIGEST["proc"]
    lines = DLOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:] if DLOG.exists() else []
    return jsonify(running=bool(pr and pr.poll() is None), lines=lines)

MONTH_RE = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"
MONTH_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

def ensure_notes():
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, tags TEXT DEFAULT '', remind_on TEXT DEFAULT '', done INTEGER DEFAULT 0, created TEXT)")
    c.commit()
    c.close()

def find_date(text):
    t = text.lower()
    today = datetime.date.today()
    if "tomorrow" in t:
        return today + datetime.timedelta(days=1)
    m = re.search(r"\bin\s+(\d{1,3})\s+days?\b", t)
    if m:
        return today + datetime.timedelta(days=int(m.group(1)))
    if "next week" in t:
        return today + datetime.timedelta(days=7)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + MONTH_RE + r"\b(?:,?\s+(\d{4}))?", t)
    if m:
        day, mon, year = m.group(1), m.group(2), m.group(3)
    else:
        m = re.search(r"\b" + MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?", t)
        if not m:
            return None
        mon, day, year = m.group(1), m.group(2), m.group(3)
    try:
        mo = MONTH_NUM[mon[:3]]
        y = int(year) if year else today.year
        d = datetime.date(y, mo, int(day))
        if not year and d < today:
            d = datetime.date(y + 1, mo, int(day))
        return d
    except Exception:
        return None

def note_view(r):
    d = dict(r)
    today = datetime.date.today().isoformat()
    ro = r["remind_on"]
    d["pretty"] = datetime.date.fromisoformat(ro).strftime("%d %b %Y") if ro else ""
    d["state"] = "" if (not ro or r["done"]) else ("overdue" if ro < today else ("today" if ro == today else "upcoming"))
    return d

NOTES = HEAD + """
<div class='card'><h3>New note</h3>
<form method='post' action='/notes/add'>
<textarea name='text' rows='4' required placeholder='e.g. Spoke to Sharma Steel. Asked him to send the rate list on 30th December.'></textarea>
<div style='display:flex;gap:12px'>
<div style='flex:1'><label>Tags (optional)</label><input name='tags' placeholder='vendor, steel'></div>
<div style='flex:1'><label>Remind me on (empty = detect from the note)</label><input type='date' name='remind_on'></div></div>
<button>Save note</button></form></div>
<div class='card'><h3>Notes ({{notes|length}})</h3>
<form method='get' style='display:flex;gap:8px;margin-bottom:10px'>
<input name='q' value='{{q}}' placeholder='Search notes and tags' style='margin:0'>
<select name='show' style='margin:0;width:200px'><option value=''>All notes</option><option value='open' {{'selected' if show=='open'}}>Open reminders</option><option value='done' {{'selected' if show=='done'}}>Done</option></select>
<button style='margin:0'>Search</button></form>
{% for n in notes %}
<div style='border-top:1px solid #e5e7eb;padding:10px 0'>
<div style='white-space:pre-wrap'>{{n.text}}</div>
<small>{{n.created}}{% if n.tags %} &nbsp;|&nbsp; {{n.tags}}{% endif %}</small>
<div style='margin-top:6px;display:flex;gap:8px;align-items:center'>
<span>Remind on:</span><input type='date' value='{{n.remind_on}}' onchange="setDate({{n.id}},this.value)" style='width:170px;margin:0'>
{% if n.state=='overdue' %}<b style='color:#b91c1c'>Overdue</b>{% elif n.state=='today' %}<b style='color:#b45309'>Today</b>{% endif %}
{% if n.remind_on %}<button class='grey' onclick="setDone({{n.id}},{{0 if n.done else 1}})">{{'Reopen' if n.done else 'Mark done'}}</button>{% endif %}
<button class='grey' onclick="del({{n.id}})">Delete</button></div></div>
{% else %}<p>No notes yet.</p>{% endfor %}</div>
<script>
function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});}
function setDate(id,v){post('/api/note/'+id,{remind_on:v}).then(function(){location.reload();});}
function setDone(id,v){post('/api/note/'+id,{done:v==1}).then(function(){location.reload();});}
function del(id){if(confirm('Delete this note?')){post('/api/note/'+id+'/delete').then(function(){location.reload();});}}
</script></main></body></html>"""

@app.route("/notes")
def notes():
    ensure_notes()
    q = request.args.get("q", "").strip()
    show = request.args.get("show", "")
    where, args = ["1=1"], []
    if q:
        where.append("(text LIKE ? OR tags LIKE ?)")
        args += ["%" + q + "%"] * 2
    if show == "open":
        where.append("remind_on<>'' AND done=0")
    if show == "done":
        where.append("done=1")
    c = conn()
    rows = c.execute("SELECT * FROM notes WHERE " + " AND ".join(where) +
                     " ORDER BY CASE WHEN remind_on<>'' AND done=0 THEN 0 ELSE 1 END, CASE WHEN remind_on<>'' AND done=0 THEN remind_on END, id DESC LIMIT 300", args).fetchall()
    c.close()
    return render_template_string(NOTES, notes=[note_view(r) for r in rows], q=q, show=show)

@app.route("/notes/add", methods=["POST"])
def notes_add():
    ensure_notes()
    f = request.form
    text = f.get("text", "").strip()
    if text:
        d = f.get("remind_on", "").strip()
        if not d:
            fd = find_date(text)
            d = fd.isoformat() if fd else ""
        c = conn()
        c.execute("INSERT INTO notes(text,tags,remind_on,created) VALUES(?,?,?,?)",
                  (text, f.get("tags", "").strip(), d, time.strftime("%Y-%m-%d %H:%M")))
        c.commit()
        c.close()
    return redirect("/notes")

@app.route("/api/note/<int:nid>", methods=["POST"])
def api_note(nid):
    j = request.get_json(force=True)
    c = conn()
    if "done" in j:
        c.execute("UPDATE notes SET done=? WHERE id=?", (1 if j["done"] else 0, nid))
    if "remind_on" in j:
        c.execute("UPDATE notes SET remind_on=? WHERE id=?", (j["remind_on"] or "", nid))
    c.commit()
    c.close()
    return jsonify(ok=True)

@app.route("/api/note/<int:nid>/delete", methods=["POST"])
def api_note_delete(nid):
    c = conn()
    c.execute("DELETE FROM notes WHERE id=?", (nid,))
    c.commit()
    c.close()
    return jsonify(ok=True)

TEST_RUNS = {}
TEST_BUSY = {"on": False}
TEST_CMDS = ("open", "click", "type", "see", "wait", "login", "nologin")
TESTS_DIR = pathlib.Path(r"D:\vault\outputs\tests")
PROFILE_DIR = r"D:\vault\automation\browser-profile"

def ensure_tests():
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS tests(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, start_url TEXT, description TEXT, steps TEXT, created TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS test_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, test_id INTEGER, started TEXT, finished TEXT, status TEXT, results TEXT)")
    c.commit()
    c.close()

def password_visible(page):
    try:
        return page.locator("input[type=password]:visible").count() > 0
    except Exception:
        return False

def find_first(page, makers, what):
    for _ in range(10):
        for make in makers:
            try:
                loc = make()
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass
        page.wait_for_timeout(1000)
    raise Exception("Could not find " + what)

def wait_login(st, page):
    st["status"] = "waiting_login"
    st["event"].clear()
    got = st["event"].wait(timeout=600)
    st["status"] = "running"
    if not got:
        raise Exception("Timed out waiting for you to log in")
    page.wait_for_timeout(1500)

def do_step(st, page, line, auto_login):
    cmd, _, arg = line.partition(" ")
    cmd = cmd.lower()
    arg = arg.strip()
    if cmd == "open":
        page.goto(arg if arg.startswith("http") else "https://" + arg, timeout=45000, wait_until="domcontentloaded")
    elif cmd == "click":
        find_first(page, [lambda: page.get_by_role("button", name=arg), lambda: page.get_by_role("link", name=arg),
                          lambda: page.get_by_text(arg, exact=False)], "'" + arg + "' to click").click(timeout=15000)
        page.wait_for_timeout(1000)
    elif cmd == "type":
        field, _, val = arg.partition("=")
        field = field.strip()
        loc = find_first(page, [lambda: page.get_by_label(field), lambda: page.get_by_placeholder(field),
                                lambda: page.get_by_role("textbox", name=field)], "field '" + field + "'")
        if (loc.get_attribute("type") or "").lower() == "password":
            raise Exception("Password fields are never filled by the tool. Use a login step and sign in yourself.")
        loc.fill(val.strip(), timeout=15000)
    elif cmd == "see":
        page.get_by_text(arg, exact=False).first.wait_for(state="visible", timeout=15000)
    elif cmd == "wait":
        page.wait_for_timeout(int(float(arg) * 1000))
    elif cmd == "login":
        wait_login(st, page)
    elif cmd == "nologin":
        pass
    else:
        raise Exception("Unknown step. Use open, click, type, see, wait or login.")
    if auto_login and cmd in ("open", "click") and password_visible(page):
        wait_login(st, page)

def run_test(run_id, steps):
    st = TEST_RUNS[run_id]
    folder = TESTS_DIR / str(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    auto_login = not any(x.strip().lower() == "nologin" for x in steps)
    final = "failed"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, channel="chrome", headless=False,
                                                        no_viewport=True, args=["--start-maximized"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            failed = False
            for i, line in enumerate(steps, 1):
                rec = {"n": i, "step": line, "status": "running", "note": "", "shot": ""}
                st["results"].append(rec)
                if failed:
                    rec["status"] = "skipped"
                    continue
                try:
                    do_step(st, page, line, auto_login)
                    rec["status"] = "passed"
                except Exception as ex:
                    rec["status"] = "failed"
                    rec["note"] = (str(ex).strip().splitlines() or ["Step failed"])[0][:200]
                    failed = True
                try:
                    page.screenshot(path=str(folder / (str(i) + ".png")))
                    rec["shot"] = str(i) + ".png"
                except Exception:
                    pass
            final = "failed" if failed else "passed"
            ctx.close()
    except Exception as ex:
        st["results"].append({"n": 0, "step": "Start Chrome", "status": "failed",
                              "note": (str(ex).strip().splitlines() or ["Could not start Chrome"])[0][:200], "shot": ""})
    finally:
        st["status"] = final
        c = conn()
        c.execute("UPDATE test_runs SET finished=?, status=?, results=? WHERE id=?",
                  (time.strftime("%Y-%m-%d %H:%M"), final, json.dumps(st["results"]), run_id))
        c.commit()
        c.close()
        TEST_BUSY["on"] = False

def clean_steps(text, url):
    out = []
    for ln in (text or "").splitlines():
        ln = re.sub(r"^[\s\-\*\d\.\)]+", "", ln).strip()
        if ln and ln.split(" ")[0].lower() in TEST_CMDS:
            out.append(ln)
    if url and not any(x.lower().startswith("open") for x in out):
        out.insert(0, "open " + url)
    return "\n".join(out[:14])

TESTING = HEAD + """
<div class='card'><h3>{{'Edit test' if e else 'New test'}}</h3>
<form method='post' action='/testing/save'>
<input type='hidden' name='id' value='{{ (e.id if e else '') }}'>
<label>Test name</label><input name='name' required value='{{ (e.name if e else '') }}' placeholder='e.g. LIMS login and patient list'>
<label>Start URL</label><input name='start_url' id='t_url' value='{{ (e.start_url if e else '') }}' placeholder='https://your-lims.up.railway.app'>
<label>What should happen? (plain words)</label><textarea name='description' id='t_desc' rows='3' placeholder='Sign in, open Patients, check that the patient list shows'>{{ (e.description if e else '') }}</textarea>
<button type='button' class='grey' onclick="aiSteps()">Write steps with AI</button> <small id='aist'></small>
<label>Steps (one per line, edit freely)</label><textarea name='steps' id='t_steps' rows='7'>{{ (e.steps if e else '') }}</textarea>
<small>Commands: open URL | click button or link text | type field label = text | see text that should appear | wait seconds | login (pause so you sign in) | nologin (do not pause at login pages)</small><br><br>
<button>Save test</button></form></div>
<div class='card'><h3>Import tests from a file</h3>
<form method='post' action='/testing/import' enctype='multipart/form-data'>
<input type='file' name='file' accept='.md,.txt' required style='width:auto;display:inline-block'>
<button>Import</button></form>
<small>Each line starting with # becomes a test. Write url: and the commands under a Steps heading.</small></div>
<div class='card'><h3>Your tests</h3>
{% if tests %}<table><tr><th>Test</th><th>URL</th><th>Last run</th><th></th></tr>
{% for t in tests %}<tr><td><b>{{t.name}}</b></td><td><small>{{t.start_url}}</small></td>
<td>{% if t.last_run %}<a href='/testing/run/{{t.last_run}}'>{{t.last_status}}</a>{% else %}never{% endif %}</td>
<td><button onclick="runTest({{t.id}})">Run</button><a class='btn grey' href='/testing?edit={{t.id}}'>Edit</a><button class='grey' onclick="delTest({{t.id}})">Delete</button></td></tr>{% endfor %}</table>
{% else %}<p>No tests yet.</p>{% endif %}</div>
<div class='card'><h3>Recent runs</h3>
{% if runs %}<table><tr><th>When</th><th>Test</th><th>Result</th></tr>
{% for r in runs %}<tr><td>{{r.started}}</td><td>{{r.name}}</td><td><a href='/testing/run/{{r.id}}'>{{r.status}}</a></td></tr>{% endfor %}</table>
{% else %}<p>No runs yet.</p>{% endif %}</div>
<script>
function aiSteps(){
  var u=document.getElementById('t_url').value,d=document.getElementById('t_desc').value;
  var st=document.getElementById('aist');st.textContent='Writing... (up to a minute)';
  fetch('/api/test/steps',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u,description:d})}).then(function(r){return r.json();}).then(function(j){
    document.getElementById('t_steps').value=j.steps||'';
    st.textContent=j.steps?'Review the steps, then save.':'AI gave no usable steps. Write them by hand.';
  });
}
function runTest(id){
  fetch('/api/test/'+id+'/run',{method:'POST'}).then(function(r){return r.json();}).then(function(j){
    if(j.ok){location.href='/testing/run/'+j.run_id;}else{alert(j.error);}
  });
}
function delTest(id){if(confirm('Delete this test?')){fetch('/testing/delete/'+id,{method:'POST'}).then(function(){location.reload();});}}
</script></main></body></html>"""

RUNPAGE = HEAD + """
<div class='card'><h3>{{name}} <small id='rst'></small></h3>
<div id='cont' style='display:none;background:#fef3c7;padding:12px;border-radius:6px;margin-bottom:10px'>
Chrome is waiting for you. Sign in in the Chrome window, then click Continue.
<button onclick="cont()" style='margin-left:12px'>Continue</button></div>
<table><tr><th>#</th><th>Step</th><th>Result</th><th>Note</th><th>Screenshot</th></tr><tbody id='rows'></tbody></table>
<p><a href='/testing'>Back to tests</a></p></div>
<script>
var RID={{rid}};var timer=null;
function cont(){fetch('/api/testrun/'+RID+'/continue',{method:'POST'});}
function render(j){
  document.getElementById('rst').textContent='(' + j.status + ')';
  document.getElementById('cont').style.display=(j.status==='waiting_login')?'block':'none';
  var tb=document.getElementById('rows');tb.innerHTML='';
  j.results.forEach(function(r){
    var tr=document.createElement('tr');
    [r.n,r.step,r.status,r.note].forEach(function(v,idx){
      var td=document.createElement('td');td.textContent=v;
      if(idx===2){td.style.fontWeight='bold';td.style.color=(r.status==='passed'?'#15803d':(r.status==='failed'?'#b91c1c':'#6b7280'));}
      tr.appendChild(td);
    });
    var td=document.createElement('td');
    if(r.shot){var a=document.createElement('a');a.href='/testing/shot/'+RID+'/'+r.shot;a.target='_blank';var im=document.createElement('img');im.src=a.href;im.style.width='160px';a.appendChild(im);td.appendChild(a);}
    tr.appendChild(td);tb.appendChild(tr);
  });
}
function poll(){
  fetch('/api/testrun/'+RID).then(function(r){return r.json();}).then(function(j){
    render(j);
    if(j.status==='passed'||j.status==='failed'){clearInterval(timer);}
  });
}
timer=setInterval(poll,1500);poll();
</script></main></body></html>"""

@app.route("/testing")
def testing():
    ensure_tests()
    c = conn()
    tests = c.execute("SELECT t.*, (SELECT status FROM test_runs r WHERE r.test_id=t.id ORDER BY r.id DESC LIMIT 1) AS last_status, "
                      "(SELECT id FROM test_runs r WHERE r.test_id=t.id ORDER BY r.id DESC LIMIT 1) AS last_run FROM tests t ORDER BY t.id DESC").fetchall()
    runs = c.execute("SELECT r.*, t.name FROM test_runs r JOIN tests t ON t.id=r.test_id ORDER BY r.id DESC LIMIT 10").fetchall()
    e = None
    if request.args.get("edit"):
        e = c.execute("SELECT * FROM tests WHERE id=?", (request.args.get("edit"),)).fetchone()
    c.close()
    return render_template_string(TESTING, tests=tests, runs=runs, e=e)

@app.route("/testing/save", methods=["POST"])
def testing_save():
    ensure_tests()
    f = request.form
    vals = (f.get("name", "").strip() or "Untitled test", f.get("start_url", "").strip(), f.get("description", "").strip(), f.get("steps", "").strip())
    c = conn()
    if f.get("id"):
        c.execute("UPDATE tests SET name=?, start_url=?, description=?, steps=? WHERE id=?", vals + (int(f["id"]),))
    else:
        c.execute("INSERT INTO tests(name,start_url,description,steps,created) VALUES(?,?,?,?,?)", vals + (time.strftime("%Y-%m-%d %H:%M"),))
    c.commit()
    c.close()
    return redirect("/testing")

@app.route("/testing/delete/<int:tid>", methods=["POST"])
def testing_delete(tid):
    c = conn()
    c.execute("DELETE FROM tests WHERE id=?", (tid,))
    c.commit()
    c.close()
    return jsonify(ok=True)

@app.route("/api/test/steps", methods=["POST"])
def api_test_steps():
    j = request.get_json(force=True)
    url = (j.get("url") or "").strip()
    prompt = ("Turn this test description into steps for a browser robot. Use ONLY these commands, one per line:\n"
              "open <url>\nclick <exact visible text of a button or link>\ntype <field label> = <text>\n"
              "see <text that should appear on the page>\nwait <seconds>\nlogin\n\n"
              "Rules: use login on its own line when the user must sign in. Never type passwords. Maximum 12 lines. Output only the steps.\n"
              "Start URL: " + url + "\nDescription: " + (j.get("description") or "") + "\n\nSteps:")
    return jsonify(steps=clean_steps(ask(prompt), url))

@app.route("/api/test/<int:tid>/run", methods=["POST"])
def api_test_run(tid):
    ensure_tests()
    if TEST_BUSY["on"]:
        return jsonify(ok=False, error="Another test is running. Wait for it to finish.")
    c = conn()
    t = c.execute("SELECT * FROM tests WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        return jsonify(ok=False, error="Test not found")
    steps = [x.strip() for x in (t["steps"] or "").splitlines() if x.strip() and not x.strip().startswith("#")]
    if t["start_url"] and not any(x.lower().startswith("open") for x in steps):
        steps.insert(0, "open " + t["start_url"])
    if not steps:
        c.close()
        return jsonify(ok=False, error="This test has no steps. Edit it and add some.")
    cur = c.execute("INSERT INTO test_runs(test_id,started,status,results) VALUES(?,?,?,?)", (tid, time.strftime("%Y-%m-%d %H:%M"), "running", "[]"))
    rid = cur.lastrowid
    c.commit()
    c.close()
    TEST_BUSY["on"] = True
    TEST_RUNS[rid] = {"status": "running", "results": [], "event": threading.Event(), "name": t["name"]}
    threading.Thread(target=run_test, args=(rid, steps), daemon=True).start()
    return jsonify(ok=True, run_id=rid)

@app.route("/testing/run/<int:rid>")
def testing_run(rid):
    c = conn()
    r = c.execute("SELECT r.*, t.name FROM test_runs r JOIN tests t ON t.id=r.test_id WHERE r.id=?", (rid,)).fetchone()
    c.close()
    if not r:
        return "Not found", 404
    return render_template_string(RUNPAGE, rid=rid, name=r["name"])

@app.route("/api/testrun/<int:rid>")
def api_testrun(rid):
    if rid in TEST_RUNS:
        st = TEST_RUNS[rid]
        return jsonify(status=st["status"], results=list(st["results"]))
    c = conn()
    r = c.execute("SELECT status, results FROM test_runs WHERE id=?", (rid,)).fetchone()
    c.close()
    if not r:
        return jsonify(status="unknown", results=[])
    return jsonify(status=r["status"], results=json.loads(r["results"] or "[]"))

@app.route("/api/testrun/<int:rid>/continue", methods=["POST"])
def api_testrun_continue(rid):
    if rid in TEST_RUNS:
        TEST_RUNS[rid]["event"].set()
    return jsonify(ok=True)

@app.route("/testing/shot/<int:rid>/<name>")
def test_shot(rid, name):
    f = TESTS_DIR / str(rid) / name
    if not re.fullmatch(r"\d{1,3}\.png", name) or not f.exists():
        return "Not found", 404
    return Response(f.read_bytes(), mimetype="image/png")

def parse_md_tests(text, fallback):
    tests, cur, in_steps, in_fence = [], None, False, False
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#\s+\S", ln):
            cur = {"name": ln.lstrip("#").strip(), "url": "", "desc": [], "steps": []}
            tests.append(cur)
            in_steps = False
            continue
        if cur is None:
            cur = {"name": fallback, "url": "", "desc": [], "steps": []}
            tests.append(cur)
        if not in_fence:
            if re.match(r"(?i)^(#{2,}\s*steps?\b.*|steps?\s*:)\s*$", ln):
                in_steps = True
                continue
            if re.match(r"^#{2,}\s", ln):
                in_steps = False
                continue
            m = re.match(r"(?i)^(?:[-*]\s*)?(?:url|start url|start)\s*:\s*(\S+)", ln)
            if m:
                cur["url"] = m.group(1)
                continue
        if in_steps or in_fence:
            body = re.sub(r"^[\s\-\*\d\.\)]+", "", ln).strip().strip("`").strip()
            if body and body.split(" ")[0].lower() in TEST_CMDS:
                cur["steps"].append(body)
        elif ln:
            cur["desc"].append(ln)
    return tests

@app.route("/testing/import", methods=["POST"])
def testing_import():
    ensure_tests()
    f = request.files.get("file")
    if f:
        raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        fallback = pathlib.Path(f.filename or "Imported test").stem
        c = conn()
        for t in parse_md_tests(text, fallback):
            if not t["steps"] and not t["url"]:
                continue
            c.execute("INSERT INTO tests(name,start_url,description,steps,created) VALUES(?,?,?,?,?)",
                      (t["name"], t["url"], "\n".join(t["desc"])[:2000], "\n".join(t["steps"]), time.strftime("%Y-%m-%d %H:%M")))
        c.commit()
        c.close()
    return redirect("/testing")

RES_LOGS = {}
RES_RUNNING = set()
RES_SKIP = ("facebook.com", "instagram.com", "pinterest.com", "quora.com", "x.com", "twitter.com")

def ensure_research():
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS research(id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, region TEXT, recent INTEGER DEFAULT 0, status TEXT, answer TEXT DEFAULT '', sources TEXT DEFAULT '[]', log TEXT DEFAULT '', created TEXT, finished TEXT)")
    c.commit()
    c.close()

def research_cleanup():
    c = conn()
    c.execute("UPDATE research SET status='failed', answer='Interrupted by a restart. Run it again.' WHERE status='running'")
    c.commit()
    c.close()

def rlog(rid, msg):
    with LOCK:
        RES_LOGS.setdefault(rid, []).append(time.strftime("%H:%M:%S") + "  " + msg)

def env_val(k):
    import os
    v = os.environ.get(k)
    if v:
        return v.strip()
    try:
        for line in open(r"D:\vault\automation\.env", encoding="utf-8-sig"):
            if line.startswith(k + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""

def ask_gemini(prompt, key):
    models = []
    for m in (env_val("GEMINI_MODEL"), "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite"):
        if m and m not in models:
            models.append(m)
    last = None
    for m in models:
        for n in range(2):
            try:
                return _ask_gemini_once(prompt, key, m)
            except Exception as ex:
                last = ex
                time.sleep(5)
    raise last

def gemini_log(model, ok, usage=None, error="", kind="call"):
    # Counts our own Gemini calls (never stores prompts, answers or keys). Must never break a call.
    try:
        u = usage or {}
        c = conn()
        c.execute("CREATE TABLE IF NOT EXISTS gemini_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, kind TEXT, model TEXT, ok INTEGER, prompt_tokens INTEGER, output_tokens INTEGER, thought_tokens INTEGER, total_tokens INTEGER, error TEXT)")
        c.execute("INSERT INTO gemini_calls(ts,day,kind,model,ok,prompt_tokens,output_tokens,thought_tokens,total_tokens,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (time.strftime("%Y-%m-%d %H:%M:%S"), time.strftime("%Y-%m-%d"), kind, model, 1 if ok else 0,
                   int(u.get("promptTokenCount") or 0), int(u.get("candidatesTokenCount") or 0),
                   int(u.get("thoughtsTokenCount") or 0), int(u.get("totalTokenCount") or 0), str(error)[:200]))
        c.commit()
        c.close()
    except Exception:
        pass

def _ask_gemini_once(prompt, key, model=None):
    model = model or env_val("GEMINI_MODEL") or "gemini-3.1-flash-lite"
    try:
        r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                          headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                          json={"contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {"temperature": 0.2}}, timeout=60)
    except Exception as ex:
        gemini_log(model, False, error="request failed: " + type(ex).__name__)
        raise
    try:
        j = r.json()
    except Exception:
        gemini_log(model, False, error="unreadable reply HTTP " + str(r.status_code))
        raise Exception("Gemini returned an unreadable reply (HTTP " + str(r.status_code) + ")")
    if "error" in j:
        msg = str(j["error"].get("message", j["error"]))[:250]
        gemini_log(model, False, j.get("usageMetadata"), "HTTP " + str(r.status_code) + " " + msg)
        raise Exception("Gemini error: " + msg)
    try:
        text = j["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        gemini_log(model, False, j.get("usageMetadata"), "no answer in reply")
        raise Exception("Gemini gave no answer: " + str(j)[:250])
    gemini_log(model, True, j.get("usageMetadata"))
    return text

def _ask_research_raw(prompt, ctx=4096):
    key = env_val("GEMINI_API_KEY")
    if key:
        try:
            return ask_gemini(prompt, key)
        except Exception:
            gemini_log("", False, error="all Gemini models failed; used local model " + MODEL, kind="fallback")
    r = requests.post(OLLAMA, json={"model": MODEL, "prompt": prompt, "stream": False,
                                    "options": {"num_ctx": ctx, "temperature": 0.2}}, timeout=900)
    try:
        j = r.json()
    except Exception:
        raise Exception("Local AI returned an unreadable reply (HTTP " + str(r.status_code) + ")")
    if "response" not in j:
        raise Exception("Local AI error: " + str(j.get("error", j))[:200])
    return j["response"].strip()

GROUND = ("STRICT RULES: Use only facts stated in the sources given. Never guess, estimate, infer, predict, calculate "
          "or add outside knowledge. Copy numbers, names and dates exactly. If something is not in the sources, write: Not found in these sources.")

def verify_answer(answer, prompt):
    srcs = {}
    tail = prompt.rsplit("Sources:", 1)[-1]
    for m in re.finditer(r"\[(\d+)\] (.*?)(?=\n\n\[\d+\] |\n\nAnswer:|$)", tail, re.S):
        srcs[m.group(1)] = re.sub(r"[,\s]", "", m.group(2)).lower()
    out = []
    for line in answer.splitlines():
        t = line.strip()
        if not t or t.lower().startswith("confidence") or "not found in" in t.lower():
            out.append(line)
            continue
        cites = re.findall(r"\[(\d+)\]", t)
        if not cites:
            out.append(line + "  \u26a0 no source cited")
            continue
        body = " ".join(srcs.get(c, "") for c in cites)
        nums = re.findall(r"\d[\d,\.]*", re.sub(r"\[\d+\]", "", t))
        bad = []
        for n in nums:
            k = re.sub(r"[,\s]", "", n).strip(".")
            if len(re.sub(r"\D", "", k)) >= 2 and k not in body:
                bad.append(n.strip(",."))
        out.append(line + ("  \u26a0 unverified figure: " + ", ".join(bad[:3]) if bad else ""))
    return "\n".join(out)

def ask_research(prompt, ctx=4096):
    strict = "Sources:" in prompt
    out = _ask_research_raw((GROUND + "\n\n" + prompt) if strict else prompt, ctx)
    return verify_answer(out, prompt) if strict else out

def plan_queries(q):
    out = ask("Write 3 different web search queries (3 to 7 words each) to research this question. One per line, no numbering, no quotes.\n\nQuestion: " + q)
    qs = []
    for ln in out.splitlines():
        ln = re.sub(r"^[\s\-\*\d\.\)]+", "", ln).strip().strip(chr(34)).strip(chr(39))
        if 2 <= len(ln.split()) <= 10 and not ln.startswith("("):
            qs.append(ln)
    if len(q.split()) <= 12:
        qs.insert(0, q)
    kw = " ".join(w for w in re.findall(r"[A-Za-z0-9]+", q) if w.lower() not in STOP_WORDS)
    if len(kw.split()) >= 2:
        qs.append(kw)
    return list(dict.fromkeys(qs))[:4] or [q[:100]]

def page_text(url):
    try:
        r = requests.get(url, headers=UA, timeout=12)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return ""
        import trafilatura
        return (trafilatura.extract(r.text, include_comments=False, include_tables=False) or "").strip()
    except Exception:
        return ""

STOP_WORDS = {"what", "which", "who", "whom", "when", "where", "why", "how", "is", "are", "was", "were", "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "do", "does", "did", "can", "could", "should", "me", "my", "i", "tell", "about", "give", "find", "out", "us", "we", "there", "any", "some"}

def web_search(rid, query, region, recent):
    limits = ["m", None] if recent else [None]
    for rg in dict.fromkeys([region, "wt-wt"]):
        for tl in limits:
            for attempt in range(2):
                try:
                    kw = {"region": rg, "max_results": 8}
                    if tl:
                        kw["timelimit"] = tl
                    res = DDGS().text(query, **kw)
                    if res:
                        return res
                except Exception as ex:
                    rlog(rid, "  [" + rg + "] " + str(ex)[:60])
                time.sleep(2)
    return []

def run_research(rid):
    RES_RUNNING.add(rid)
    status, answer, sources = "failed", "", []
    try:
        c = conn()
        row = c.execute("SELECT * FROM research WHERE id=?", (rid,)).fetchone()
        c.close()
        q = row["question"]
        rlog(rid, "Planning searches...")
        queries = plan_queries(q)
        region = "in-en" if row["region"] == "india" else "wt-wt"
        seen, hits = set(), []
        for x in queries:
            rlog(rid, "Searching: " + x)
            res = web_search(rid, x, region, row["recent"])
            rlog(rid, "  " + str(len(res)) + " results")
            for r in res:
                u = r.get("href") or ""
                d = domain_of(u)
                if not u or u in seen or any(d == z or d.endswith("." + z) for z in RES_SKIP):
                    continue
                seen.add(u)
                hits.append(r)
            time.sleep(1)
        if not hits:
            raise Exception("No search results found. Try rephrasing the question.")
        for r in hits[:14]:
            if len(sources) >= 7:
                break
            u = r["href"]
            d = domain_of(u)
            rlog(rid, "Reading " + d)
            txt = page_text(u)
            full = len(txt) >= 300
            if not full:
                txt = (r.get("body") or "").strip()
            if len(txt) < 60:
                continue
            sources.append({"n": len(sources) + 1, "title": (r.get("title") or d)[:120], "url": u, "domain": d, "text": txt[:1200], "full": full})
        if not sources:
            raise Exception("Found pages but could not read any of them.")
        block = "\n\n".join("[%d] %s (%s)\n%s" % (x["n"], x["title"], x["domain"], x["text"]) for x in sources)
        prompt = ("You are a careful research assistant. Answer the question using ONLY the numbered sources below.\n"
                  "Rules:\n- Every factual claim must end with its source number like [2].\n"
                  "- Copy figures, dates and names exactly as written in the sources. Do not calculate or guess.\n"
                  "- If sources disagree, say so. If the sources do not answer something, write: Not found in these sources.\n"
                  "- Write 5 to 8 short bullet points, then one last line starting with Confidence: (high, medium or low) and why.\n\n"
                  "Question: " + q + "\n\nSources:\n" + block + "\n\nAnswer:")
        rlog(rid, "Asking the local AI to write the answer from " + str(len(sources)) + " sources (this can take a few minutes)...")
        answer = ask_research(prompt)
        status = "done"
        rlog(rid, "Done.")
    except Exception as ex:
        answer = str(ex)[:300]
        rlog(rid, "ERROR: " + answer)
    finally:
        c = conn()
        c.execute("UPDATE research SET status=?, answer=?, sources=?, log=?, finished=? WHERE id=?",
                  (status, answer, json.dumps(sources), "\n".join(RES_LOGS.get(rid, [])), time.strftime("%Y-%m-%d %H:%M"), rid))
        c.commit()
        c.close()
        RES_RUNNING.discard(rid)

def build_prompt(q, sources, limit=900):
    block = "\n\n".join("[%d] %s (%s)\n%s" % (x["n"], x["title"], x["domain"], x["text"][:limit]) for x in sources)
    return ("You are a careful research assistant. Answer the question using ONLY the numbered sources below.\n"
            "Rules:\n- Every factual claim must end with its source number like [2].\n"
            "- Copy figures, dates and names exactly as written in the sources. Do not calculate or guess.\n"
            "- If sources disagree, say so. If the sources do not answer something, write: Not found in these sources.\n"
            "- Write 5 to 8 short bullet points, then one last line starting with Confidence: (high, medium or low) and why.\n\n"
            "Question: " + q + "\n\nSources:\n" + block + "\n\nAnswer:")

def rewrite_answer(rid):
    RES_RUNNING.add(rid)
    status, answer = "failed", ""
    try:
        c = conn()
        row = c.execute("SELECT * FROM research WHERE id=?", (rid,)).fetchone()
        c.close()
        sources = json.loads(row["sources"] or "[]")
        if not sources:
            raise Exception("No saved sources to write from. Run the research again.")
        with LOCK:
            RES_LOGS[rid] = []
        rlog(rid, "Asking the local AI again with a smaller input (this can take a few minutes)...")
        answer = ask_research(build_prompt(row["question"], sources), 3072)
        status = "done"
        rlog(rid, "Done.")
    except Exception as ex:
        answer = str(ex)[:300]
        rlog(rid, "ERROR: " + answer)
    finally:
        c = conn()
        c.execute("UPDATE research SET status=?, answer=?, finished=? WHERE id=?",
                  (status, answer, time.strftime("%Y-%m-%d %H:%M"), rid))
        c.commit()
        c.close()
        RES_RUNNING.discard(rid)

@app.route("/api/research/<int:rid>/rewrite", methods=["POST"])
def api_research_rewrite(rid):
    if RES_RUNNING:
        return jsonify(ok=False, error="Something is already running.")
    c = conn()
    c.execute("UPDATE research SET status='running' WHERE id=?", (rid,))
    c.commit()
    c.close()
    threading.Thread(target=rewrite_answer, args=(rid,), daemon=True).start()
    return jsonify(ok=True)

def link_cites(answer, sources):
    safe = html.escape(answer)
    urls = {x["n"]: x["url"] for x in sources}
    def sub(m):
        n = int(m.group(1))
        if n in urls:
            return "<a href='%s' target='_blank'>[%d]</a>" % (html.escape(urls[n], quote=True), n)
        return m.group(0)
    return re.sub(r"\[(\d{1,2})\]", sub, safe)

RESEARCH = HEAD + """
<div class='card'><h3>New research</h3>
<form method='post' action='/research/start'>
<label>What do you want to find out?</label>
<textarea name='question' rows='3' required placeholder='e.g. What do Indian hospitals pay for hospital management software, and who are the main vendors?'></textarea>
<div style='display:flex;gap:12px'>
<div style='flex:1'><label>Where</label><select name='region'><option value='india'>India</option><option value='world'>Worldwide</option></select></div>
<div style='flex:1'><label>How recent</label><select name='recent'><option value='0'>Any time</option><option value='1'>Past month</option></select></div></div>
<button>Start research</button></form></div>
<div class='card'><h3>Library ({{rows|length}})</h3>
<form method='get' style='display:flex;gap:8px;margin-bottom:10px'>
<input name='q' value='{{q}}' placeholder='Search past research' style='margin:0'><button style='margin:0'>Search</button></form>
{% if rows %}<table><tr><th>When</th><th>Question</th><th>Status</th><th></th></tr>
{% for r in rows %}<tr><td style='width:140px'>{{r.created}}</td><td><a href='/research/{{r.id}}'>{{r.question[:140]}}</a></td><td>{{r.status}}</td><td><button class='grey' onclick="delRes({{r.id}})">Delete</button></td></tr>{% endfor %}</table>
{% else %}<p>Nothing yet.</p>{% endif %}</div>
<script>function delRes(id){if(confirm('Delete this research?')){fetch('/research/delete/'+id,{method:'POST'}).then(function(){location.reload();});}}</script>
</main></body></html>"""

RESPAGE = HEAD + """
<div class='card'><h3>{{r.question}}</h3>
<small>{{r.created}} | {{'India' if r.region=='india' else 'Worldwide'}}{% if r.recent %} | past month{% endif %} | status: {{r.status}}</small>
{% if r.status=='running' %}<pre id='rlog' style='margin-top:10px'>Starting...</pre>
{% else %}
<div style='white-space:pre-wrap;margin-top:12px'>{{answer_html|safe}}</div>
<p><small>Written by the local AI from the sources below. It can misread a source, so open the links before relying on any figure.</small></p>
{% if (r.status=='failed' or (r.answer or '').startswith('(')) and sources %}<button onclick="rewrite()">Retry the AI step</button> {% endif %}<button onclick="toNote()">Save to Notes</button> <small id='nmsg'></small>
{% endif %}</div>
{% if sources %}<div class='card'><h3>Sources</h3>
{% for x in sources %}<div style='padding:6px 0;border-top:1px solid #e5e7eb'><b>[{{x.n}}]</b> <a href='{{x.url}}' target='_blank'>{{x.title}}</a> <small>({{x.domain}}{% if not x.full %}, search snippet only{% endif %})</small><br><small>{{x.text[:220]}}...</small></div>{% endfor %}</div>{% endif %}
<p><a href='/research'>Back to research</a></p>
<script>
var RID={{r.id}};
function rewrite(){fetch('/api/research/'+RID+'/rewrite',{method:'POST'}).then(function(){location.reload();});}
function toNote(){fetch('/api/research/'+RID+'/note',{method:'POST'}).then(function(r){return r.json();}).then(function(j){document.getElementById('nmsg').textContent=j.ok?'Saved to Notes.':'Could not save.';});}
{% if r.status=='running' %}
setInterval(function(){fetch('/api/research/'+RID).then(function(r){return r.json();}).then(function(j){
  document.getElementById('rlog').textContent=j.lines.join('\\n');
  if(j.status!=='running'){location.reload();}
});},2000);
{% endif %}
</script></main></body></html>"""

@app.route("/research")
def research():
    ensure_research()
    q = request.args.get("q", "").strip()
    c = conn()
    if q:
        rows = c.execute("SELECT * FROM research WHERE question LIKE ? OR answer LIKE ? ORDER BY id DESC LIMIT 100", ("%" + q + "%", "%" + q + "%")).fetchall()
    else:
        rows = c.execute("SELECT * FROM research ORDER BY id DESC LIMIT 100").fetchall()
    c.close()
    return render_template_string(RESEARCH, rows=rows, q=q)

@app.route("/research/start", methods=["POST"])
def research_start():
    ensure_research()
    if RES_RUNNING:
        return redirect("/research/%d" % list(RES_RUNNING)[0])
    f = request.form
    q = f.get("question", "").strip()
    if not q:
        return redirect("/research")
    c = conn()
    cur = c.execute("INSERT INTO research(question,region,recent,status,created) VALUES(?,?,?,?,?)",
                    (q, f.get("region", "india"), 1 if f.get("recent") == "1" else 0, "running", time.strftime("%Y-%m-%d %H:%M")))
    rid = cur.lastrowid
    c.commit()
    c.close()
    threading.Thread(target=run_research, args=(rid,), daemon=True).start()
    return redirect("/research/%d" % rid)

@app.route("/research/<int:rid>")
def research_view(rid):
    ensure_research()
    c = conn()
    r = c.execute("SELECT * FROM research WHERE id=?", (rid,)).fetchone()
    c.close()
    if not r:
        return "Not found", 404
    sources = json.loads(r["sources"] or "[]")
    return render_template_string(RESPAGE, r=r, sources=sources, answer_html=link_cites(r["answer"] or "", sources))

@app.route("/api/research/<int:rid>")
def api_research(rid):
    c = conn()
    r = c.execute("SELECT status, log FROM research WHERE id=?", (rid,)).fetchone()
    c.close()
    if not r:
        return jsonify(status="unknown", lines=[])
    with LOCK:
        lines = list(RES_LOGS.get(rid, [])) or (r["log"] or "").splitlines()
    return jsonify(status=r["status"], lines=lines[-60:])

@app.route("/api/research/<int:rid>/note", methods=["POST"])
def api_research_note(rid):
    ensure_notes()
    c = conn()
    r = c.execute("SELECT * FROM research WHERE id=?", (rid,)).fetchone()
    if not r:
        c.close()
        return jsonify(ok=False)
    c.execute("INSERT INTO notes(text,tags,remind_on,created) VALUES(?,?,?,?)",
              ("Research: " + r["question"] + "\n\n" + (r["answer"] or ""), "research", "", time.strftime("%Y-%m-%d %H:%M")))
    c.commit()
    c.close()
    return jsonify(ok=True)

@app.route("/research/delete/<int:rid>", methods=["POST"])
def research_delete(rid):
    c = conn()
    c.execute("DELETE FROM research WHERE id=?", (rid,))
    c.commit()
    c.close()
    return jsonify(ok=True)

@app.route("/campaigns")
def campaigns():
    c = conn()
    camps = c.execute("SELECT c.*, (SELECT COUNT(*) FROM leads l WHERE l.campaign_id=c.id) AS n FROM campaigns c ORDER BY c.id DESC").fetchall()
    c.close()
    return render_template_string(HOME, camps=camps)

@app.route("/campaign", methods=["POST"])
def create():
    f = request.form
    c = conn()
    cur = c.execute("INSERT INTO campaigns(name,queries,location,pitch,language,max_leads,created) VALUES(?,?,?,?,?,?,?)",
                    (f["name"].strip(), f["queries"].strip(), ", ".join(x for x in (f.get("city", "").strip(), f.get("state", "").strip(), f.get("country", "").strip()) if x), f["pitch"].strip(), f["language"],
                     int(f.get("max_leads") or 40), time.strftime("%Y-%m-%d %H:%M")))
    cid = cur.lastrowid
    c.commit()
    c.close()
    threading.Thread(target=start_campaign, args=(cid,), daemon=True).start()   # asks first if businesses for this search already exist
    return redirect("/campaign/%d" % cid)

@app.route("/campaign/<int:cid>")
def view(cid):
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    has = request.args.get("has", "")
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    per = 50
    where, args = ["campaign_id=?"], [cid]
    if q:
        where.append("(title LIKE ? OR phones LIKE ? OR emails LIKE ? OR notes LIKE ? OR domain LIKE ?)")
        args += ["%" + q + "%"] * 5
    if status:
        where.append("status=?")
        args.append(status)
    if has == "phone":
        where.append("phones<>''")
    if has == "email":
        where.append("emails<>''")
    w = " AND ".join(where)
    c = conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return "Not found", 404
    total = c.execute("SELECT COUNT(*) FROM leads WHERE " + w, args).fetchone()[0]
    pages = max((total + per - 1) // per, 1)
    page = min(page, pages)
    leads = c.execute("SELECT * FROM leads WHERE " + w + " ORDER BY id LIMIT ? OFFSET ?", args + [per, (page - 1) * per]).fetchall()
    c.close()
    qs = urllib.parse.urlencode({"q": q, "status": status, "has": has})
    try:
        pre = json.loads(camp["prestart"]) if camp["prestart"] else {}
    except Exception:
        pre = {}
    return render_template_string(CAMP, c=camp, leads=leads, statuses=STATUSES, qline=" | ".join(camp["queries"].splitlines()),
                                  total=total, page=page, pages=pages, q=q, status=status, has=has, qs=qs, pre=pre)

@app.route("/api/run/<int:cid>", methods=["POST"])
def api_run(cid):
    threading.Thread(target=run_campaign, args=(cid,), daemon=True).start()
    return jsonify(ok=True)

@app.route("/api/draftall/<int:cid>", methods=["POST"])
def api_draftall(cid):
    threading.Thread(target=draft_all, args=(cid,), daemon=True).start()
    return jsonify(ok=True)

@app.route("/api/log/<int:cid>")
def api_log(cid):
    with LOCK:
        lines = list(LOGS.get(cid, []))[-200:]
    return jsonify(running=cid in RUNNING, lines=lines)

@app.route("/api/lead/<int:lid>", methods=["POST"])
def api_lead(lid):
    j = request.get_json(force=True)
    c = conn()
    c.execute("UPDATE leads SET status=?, notes=? WHERE id=?",
              (j.get("status", "New"), j.get("notes", ""), lid))
    c.commit()
    c.close()
    return jsonify(ok=True)

@app.route("/api/draft/<int:lid>", methods=["POST"])
def api_draft(lid):
    c = conn()
    l = c.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (l["campaign_id"],)).fetchone()
    c.close()
    log(camp["id"], "Writing message for " + (l["title"] or "")[:60])
    d = draft(l, camp)
    c = conn()
    c.execute("UPDATE leads SET draft=? WHERE id=?", (d, lid))
    c.commit()
    c.close()
    return jsonify(draft=d)

NAME_COLS = ("name", "business", "business name", "company", "company name", "hospital", "hospital name", "title", "organisation", "organization", "facility name")
PHONE_COLS = ("phone", "phone number", "mobile", "mobile number", "contact", "contact number", "telephone", "tel", "whatsapp")
EMAIL_COLS = ("email", "e-mail", "email id", "email address", "mail")
SITE_COLS = ("website", "url", "web", "site", "web address")
ADDR_COLS = ("address", "location", "city", "area", "district", "full address")

def col(row, names):
    for k, v in row.items():
        if k and v and k.strip().lower() in names and v.strip():
            return v.strip()
    return ""

@app.route("/campaign/<int:cid>/import", methods=["POST"])
def import_csv(cid):
    f = request.files.get("file")
    if not f:
        return redirect("/campaign/%d" % cid)
    raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    c = conn()
    seen = {r["domain"] for r in c.execute("SELECT domain FROM leads WHERE campaign_id=?", (cid,))}
    added = skipped = 0
    for row in csv.DictReader(io.StringIO(text)):
        name = col(row, NAME_COLS)
        if not name:
            skipped += 1
            continue
        phone_raw = col(row, PHONE_COLS)
        phones = list(dict.fromkeys(PHONE.findall(re.sub(r"[\s\-\(\)\.]", "", phone_raw))))
        if not phones and phone_raw:
            phones = [phone_raw[:20]]
        mail = col(row, EMAIL_COLS).lower()
        site = col(row, SITE_COLS)
        if site and not site.startswith("http"):
            site = "http://" + site
        d = domain_of(site) if site else "csv-" + re.sub(r"\W+", "", name.lower()) + (phones[0] if phones else "")
        if d in seen:
            skipped += 1
            continue
        seen.add(d)
        c.execute("INSERT INTO leads(campaign_id,title,url,domain,snippet,phones,emails,created) VALUES(?,?,?,?,?,?,?,?)",
                  (cid, name, site, d, col(row, ADDR_COLS), ",".join(phones[:3]), mail, time.strftime("%Y-%m-%d %H:%M")))
        added += 1
    c.commit()
    c.close()
    log(cid, "CSV import: " + str(added) + " added, " + str(skipped) + " skipped (no name or duplicate)")
    return redirect("/campaign/%d" % cid)

@app.route("/campaign/<int:cid>/export.csv")
def export(cid):
    c = conn()
    rows = c.execute("SELECT * FROM leads WHERE campaign_id=? ORDER BY id", (cid,)).fetchall()
    c.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Business", "Website", "Phones", "Emails", "Status", "Notes", "Message"])
    for l in rows:
        w.writerow([l["title"], l["url"], l["phones"], l["emails"], l["status"], l["notes"], l["draft"]])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=leads-%d.csv" % cid})

# ---- System page: usage
CLAUDE_PROJECTS = pathlib.Path.home() / ".claude" / "projects"

def claude_usage(days=14):
    # Reads only token counts, timestamp and project folder from Claude Code's *.jsonl session logs (no message text is kept).
    cutoff = datetime.date.today() - datetime.timedelta(days=days - 1)
    per_day, per_proj, seen = {}, {}, set()
    files, errors = 0, 0
    try:
        paths = list(CLAUDE_PROJECTS.glob("**/*.jsonl"))
    except Exception:
        paths = []
    for p in paths:
        try:
            if datetime.date.fromtimestamp(p.stat().st_mtime) < cutoff:
                continue
            proj = p.relative_to(CLAUDE_PROJECTS).parts[0]
            files += 1
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        j = json.loads(line)
                        m = j.get("message") or {}
                        u = m.get("usage") if isinstance(m, dict) else None
                        if not u:
                            continue
                        key = m.get("id") or j.get("requestId") or j.get("uuid")
                        if key in seen:
                            continue
                        seen.add(key)
                        day = datetime.datetime.fromisoformat(j["timestamp"].replace("Z", "+00:00")).astimezone().date()
                        if day < cutoff:
                            continue
                        vals = [int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                                int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0)]
                        for store, k in ((per_day, day.isoformat()), (per_proj, proj)):
                            row = store.setdefault(k, [0, 0, 0, 0, 0])
                            for i, v in enumerate(vals):
                                row[i] += v
                            row[4] += 1
                    except Exception:
                        errors += 1
        except Exception:
            errors += 1
    d = datetime.date.today()
    day_rows = [(x.isoformat(), per_day.get(x.isoformat(), [0, 0, 0, 0, 0])) for x in (d - datetime.timedelta(days=i) for i in range(days))]
    proj_rows = sorted(per_proj.items(), key=lambda kv: -sum(kv[1][:4]))
    return day_rows, proj_rows, files, errors

def gemini_usage(days=14):
    since = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    try:
        c = conn()
        c.execute("CREATE TABLE IF NOT EXISTS gemini_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, kind TEXT, model TEXT, ok INTEGER, prompt_tokens INTEGER, output_tokens INTEGER, thought_tokens INTEGER, total_tokens INTEGER, error TEXT)")
        rows = c.execute("SELECT day, SUM(kind='call') AS calls, SUM(kind='call' AND ok=1) AS ok, SUM(kind='call' AND ok=0) AS bad, "
                         "SUM(CASE WHEN kind='call' THEN prompt_tokens ELSE 0 END) AS pt, SUM(CASE WHEN kind='call' THEN output_tokens ELSE 0 END) AS ot, "
                         "SUM(CASE WHEN kind='call' THEN thought_tokens ELSE 0 END) AS tt, SUM(CASE WHEN kind='call' THEN total_tokens ELSE 0 END) AS tot, "
                         "SUM(kind='fallback') AS fb FROM gemini_calls WHERE day>=? GROUP BY day ORDER BY day DESC", (since,)).fetchall()
        models = c.execute("SELECT model, COUNT(*) AS n, SUM(ok) AS ok FROM gemini_calls WHERE kind='call' AND day>=? GROUP BY model ORDER BY n DESC", (since,)).fetchall()
        last_err = c.execute("SELECT ts, model, error FROM gemini_calls WHERE kind='call' AND ok=0 ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        return rows, models, last_err
    except Exception:
        return [], [], None

@app.route("/system")
def system():
    day_rows, proj_rows, files, errors = claude_usage(14)
    g_rows, g_models, g_err = gemini_usage(14)
    tot = [sum(r[1][i] for r in day_rows) for i in range(5)]
    g_tot = {k: sum((r[k] or 0) for r in g_rows) for k in ("calls", "ok", "bad", "pt", "ot", "tt", "tot", "fb")}
    return render_template_string(SYS, day_rows=day_rows, proj_rows=proj_rows, files=files, errors=errors, tot=tot,
                                  g_rows=g_rows, g_models=g_models, g_err=g_err, g_tot=g_tot, model=MODEL)

SYS = HEAD + """
<h2>System</h2>
<div class='card'><h3>Usage: Claude Code, last 14 days</h3>
<p><small>Read from Claude Code's local session logs ({{files}} log file(s) scanned{% if errors %}, {{errors}} unreadable line(s) skipped{% endif %}). Only token counts are read, never message text. Days use this PC's local time. Cache write and cache read are prompt-cache tokens.</small></p>
<table><tr><th>Day</th><th>Requests</th><th>Input</th><th>Output</th><th>Cache write</th><th>Cache read</th></tr>
{% for d, r in day_rows %}<tr><td>{{d}}</td><td>{{r[4]}}</td><td>{{'{:,}'.format(r[0])}}</td><td>{{'{:,}'.format(r[1])}}</td><td>{{'{:,}'.format(r[2])}}</td><td>{{'{:,}'.format(r[3])}}</td></tr>{% endfor %}
<tr><th>Total</th><th>{{tot[4]}}</th><th>{{'{:,}'.format(tot[0])}}</th><th>{{'{:,}'.format(tot[1])}}</th><th>{{'{:,}'.format(tot[2])}}</th><th>{{'{:,}'.format(tot[3])}}</th></tr></table>
<h3 style='margin-top:18px'>Per project</h3>
<table><tr><th>Project folder</th><th>Requests</th><th>Input</th><th>Output</th><th>Cache write</th><th>Cache read</th></tr>
{% for p, r in proj_rows %}<tr><td>{{p}}</td><td>{{r[4]}}</td><td>{{'{:,}'.format(r[0])}}</td><td>{{'{:,}'.format(r[1])}}</td><td>{{'{:,}'.format(r[2])}}</td><td>{{'{:,}'.format(r[3])}}</td></tr>
{% else %}<tr><td colspan='6'>No Claude Code usage found in the last 14 days.</td></tr>{% endfor %}</table>
<p style='margin-top:12px'><a class='btn' href='https://claude.ai/settings/usage' target='_blank' rel='noopener'>Open claude.ai usage</a>
<small>Your plan usage percentage is shown only there. These logs hold token counts only, not your plan limit, so this page cannot show a percentage.</small></p></div>

<div class='card'><h3>Usage: this app's Gemini calls, last 14 days</h3>
<p><small>Counted by this app since logging was added (earlier calls are not included). Each row counts every API attempt; failed attempts may lack token counts. Calls made by the separate morning-digest script are not included.</small></p>
<p>Fell back to the local model (<b>{{model}}</b>) after Gemini failed: <b>{{g_tot.fb or 0}}</b> time(s){% if g_tot.calls %} &middot; Gemini attempts failed: <b>{{g_tot.bad or 0}}</b> of {{g_tot.calls}}{% endif %}.</p>
<table><tr><th>Day</th><th>Calls</th><th>OK</th><th>Failed</th><th>Prompt tokens</th><th>Output tokens</th><th>Thinking tokens</th><th>Total tokens</th><th>Fell back to local</th></tr>
{% for r in g_rows %}<tr><td>{{r.day}}</td><td>{{r.calls or 0}}</td><td>{{r.ok or 0}}</td><td>{{r.bad or 0}}</td><td>{{'{:,}'.format(r.pt or 0)}}</td><td>{{'{:,}'.format(r.ot or 0)}}</td><td>{{'{:,}'.format(r.tt or 0)}}</td><td>{{'{:,}'.format(r.tot or 0)}}</td><td>{{r.fb or 0}}</td></tr>
{% else %}<tr><td colspan='9'>No Gemini calls logged yet.</td></tr>{% endfor %}
{% if g_rows %}<tr><th>Total</th><th>{{g_tot.calls}}</th><th>{{g_tot.ok}}</th><th>{{g_tot.bad}}</th><th>{{'{:,}'.format(g_tot.pt)}}</th><th>{{'{:,}'.format(g_tot.ot)}}</th><th>{{'{:,}'.format(g_tot.tt)}}</th><th>{{'{:,}'.format(g_tot.tot)}}</th><th>{{g_tot.fb}}</th></tr>{% endif %}</table>
{% if g_models %}<p style='margin-top:10px'><small>By model: {% for m in g_models %}{{m.model}}: {{m.n}} call(s), {{m.ok or 0}} ok{% if not loop.last %}; {% endif %}{% endfor %}</small></p>{% endif %}
{% if g_err %}<p><small>Most recent failure: {{g_err.ts}} &middot; {{g_err.model}} &middot; {{g_err.error}}</small></p>{% endif %}</div>
</main></body></html>"""

# ---- Stage 1: master business list
TRUST_DAYS = 30        # businesses seen within this many days are offered before a new search starts
NEAR_M = 200           # two records this close (metres) can be the same shop
PLAN_TTL = 600         # seconds a search plan is reused (the pre-check and the run share one plan)
BIZ_STATUSES = ["new", "contacted", "interested", "not interested", "customer"]
PLAN_CACHE = {}
BIZ_COUNTS = {}

_BIZ_READY = False

def ensure_businesses():
    global _BIZ_READY
    if _BIZ_READY:
        return
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS businesses(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, name_key TEXT, category TEXT, category_tags TEXT DEFAULT '[]',
      address TEXT DEFAULT '', locality TEXT DEFAULT '', city TEXT DEFAULT '', district TEXT DEFAULT '', state TEXT DEFAULT '', pincode TEXT DEFAULT '',
      lat REAL, lon REAL, phones TEXT DEFAULT '[]', emails TEXT DEFAULT '[]', website TEXT DEFAULT '', sources TEXT DEFAULT '{}',
      quality_score INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT, last_verified TEXT DEFAULT '',
      status TEXT DEFAULT 'new', last_contacted TEXT DEFAULT '', owner_notes TEXT DEFAULT '', do_not_contact INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS ix_biz_city ON businesses(city);
    CREATE INDEX IF NOT EXISTS ix_biz_namekey ON businesses(name_key);
    CREATE TABLE IF NOT EXISTS campaign_businesses(campaign_id INTEGER, business_id INTEGER, action TEXT, found_at TEXT, PRIMARY KEY(campaign_id, business_id));
    CREATE TABLE IF NOT EXISTS coverage_log(id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT, place TEXT, date TEXT, leads_found INTEGER, coverage_estimate INTEGER);
    CREATE TABLE IF NOT EXISTS business_review(id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER, candidate_id INTEGER, name_key TEXT, reason TEXT, data TEXT, created TEXT, resolved TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS entity_profiles(entity_key TEXT PRIMARY KEY, tags TEXT, names TEXT, created TEXT);
    """)
    try:
        c.execute("ALTER TABLE campaigns ADD COLUMN prestart TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    c.commit()
    c.close()
    _BIZ_READY = True

def plan_leads(cid, camp, term):
    key = (cid, term)
    hit = PLAN_CACHE.get(key)
    if hit and time.time() - hit[0] < PLAN_TTL:
        return hit[1]
    res = _plan_leads(cid, camp, term)
    PLAN_CACHE[key] = (time.time(), res)
    return res

# ---- (4) one AI call per entity: map tags and name words (including Hindi/local words), cached in the DB
def entity_profile(cid, entity):
    key = re.sub(r"\s+", " ", entity.lower()).strip()
    try:
        ensure_businesses()
        c = conn()
        row = c.execute("SELECT tags, names FROM entity_profiles WHERE entity_key=?", (key,)).fetchone()
        c.close()
        if row:
            return json.loads(row["tags"]), json.loads(row["names"])
    except Exception:
        pass
    prompt = ("For the kind of business below, output ONLY JSON like {\"osm\":[\"shop=houseware\"],\"names\":[\"word\"]}.\n"
              "osm: up to 5 OpenStreetMap tags as key=value that places of this business type carry (shop=, amenity=, craft=, office=, healthcare=).\n"
              "names: up to 10 words or short phrases that commonly appear inside the NAMES of such businesses in India, including Hindi and other "
              "local-language words written in English letters (for example 'kirana' for grocery). Lowercase, no city names, "
              "and no generic words that would also match unrelated businesses.\n\nBusiness: " + entity)
    try:
        plan = parse_json(ask_research(prompt, 1024), "{}")
    except Exception as ex:
        log(cid, "  map profile unavailable: " + str(ex)[:80])
        plan = None
    if not isinstance(plan, dict):
        return [], []
    tags = list(dict.fromkeys(str(t).strip() for t in (plan.get("osm") or []) if re.fullmatch(r"[a-z_:]+=[A-Za-z0-9_:]+", str(t).strip())))[:5]
    names = list(dict.fromkeys(str(n).strip().lower() for n in (plan.get("names") or []) if re.fullmatch(r"[A-Za-z0-9 ]{3,24}", str(n).strip())))[:10]
    if tags or names:
        c = conn()
        c.execute("INSERT OR REPLACE INTO entity_profiles(entity_key,tags,names,created) VALUES(?,?,?,?)",
                  (key, json.dumps(tags), json.dumps(names), time.strftime("%Y-%m-%d")))
        c.commit()
        c.close()
        log(cid, "  AI map profile for '" + entity + "': tags " + (", ".join(tags) or "none") + " | name words " + (", ".join(names) or "none"))
    return tags, names

# ---- (3) master list: normalising, matching, merging
def now_day():
    return time.strftime("%Y-%m-%d")

def norm_phone(raw):
    d = re.sub(r"\D", "", str(raw or ""))
    trunk0 = False
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 13 and d.startswith("091"):
        d = d[3:]
    elif len(d) == 11 and d.startswith("0"):
        d, trunk0 = d[1:], True
    if len(d) != 10:
        return None
    if d[0] in "6789" and not (trunk0 and d[0] != "9"):
        return "+91" + d, "mobile"
    if d[0] in "12345678":
        return "+91" + d, "landline"   # marker is a pattern guess: 6-9 mobile, 1-5 or a 0-prefixed STD number landline
    return None

def fmt_phone(n):
    return "+91 " + n[3:8] + " " + n[8:] if len(n) == 13 else n

def name_key_of(name):
    t = tok(name)
    return " ".join(sorted(t)) if t else re.sub(r"\s+", " ", name.lower()).strip()

def jacc(a, b):
    a, b = set(a), set(b)
    u = a | b
    return len(a & b) / len(u) if u else 0.0

def dist_m(la1, lo1, la2, lo2):
    import math
    p1, p2 = math.radians(la1), math.radians(la2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))

def dir_domain(d):
    return (not d) or blocked(d) or "openstreetmap.org" in d or any(d == x or d.endswith("." + x) for x in AGG_DOMAINS)

def homepage_only(url):
    # a result page counts as the business's own website only when it is a site front page or a one-level page, not a deep directory profile
    return url if len([x for x in urllib.parse.urlparse(url).path.split("/") if x]) <= 1 else ""

def clean_name(name):
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    n = re.split(r"\s[-|–—]\s", n)[0]      # page titles: "Shop - tagline" -> "Shop"
    return n.strip(" .,…")[:120]

def b_load(row):
    d = dict(row)
    for k in ("phones", "emails", "category_tags"):
        d[k] = json.loads(d.get(k) or "[]")
    d["sources"] = json.loads(d.get("sources") or "{}")
    return d

def add_source(src, field, value, url, day):
    if value in (None, ""):
        return
    lst = src.setdefault(field, {}).setdefault(str(value), [])
    if url and not any(x.get("url") == url for x in lst):
        lst.append({"url": url, "date": day})

def score_of(b):
    s = 10
    if b["phones"]:
        s += 20 + (5 if any(p["t"] == "mobile" for p in b["phones"]) else 0)
    s += 10 if b["emails"] else 0
    s += 10 if b["website"] else 0
    s += 15 if b["address"] else 0
    s += 5 if b["locality"] else 0
    s += 5 if b["pincode"] else 0
    s += 10 if b.get("lat") is not None and b.get("lon") is not None else 0
    s += 5 if b["category"] else 0
    urls = {x["url"] for vals in b["sources"].values() for lst in vals.values() for x in lst}
    s += 5 if len(urls) >= 2 else 0
    return s

def place_rel(a, b):
    # returns (far, corroborated): far = clearly a different place/branch; corroborated = location evidence says the same place
    if a.get("lat") is not None and b.get("lat") is not None:
        d = dist_m(a["lat"], a["lon"], b["lat"], b["lon"])
        return d > NEAR_M, d <= NEAR_M
    la_, lb_ = (a.get("locality") or "").lower(), (b.get("locality") or "").lower()
    pa, pb = a.get("pincode") or "", b.get("pincode") or ""
    far = bool((la_ and lb_ and la_ != lb_) or (pa and pb and pa != pb))
    corr = bool((la_ and la_ == lb_) or (pa and pa == pb))
    if not corr and a.get("address") and b.get("address"):
        corr = jacc(re.findall(r"[a-z0-9]+", a["address"].lower()), re.findall(r"[a-z0-9]+", b["address"].lower())) >= 0.6
    return far, corr and not far

def find_match(c, rec):
    doubt = None
    for ph, _ in rec["phones"]:
        for r in c.execute("SELECT * FROM businesses WHERE phones LIKE ?", ('%"' + ph + '"%',)).fetchall():
            b = b_load(r)
            if jacc(rec["name_key"].split(), b["name_key"].split()) > 0:
                return "phone", b, ""
            doubt = doubt or (b, "same phone but a different name")
    wd = domain_of(rec["website"]) if rec["website"] else ""
    if wd and not dir_domain(wd):
        same = [b for b in (b_load(r) for r in c.execute("SELECT * FROM businesses WHERE website LIKE ?", ("%" + wd + "%",)).fetchall())
                if domain_of(b["website"]) == wd]
        if len(same) == 1 and not place_rel(rec, same[0])[0]:
            return "website", same[0], ""
    rows = {}
    if rec["city"]:
        for r in c.execute("SELECT * FROM businesses WHERE lower(city)=lower(?)", (rec["city"],)).fetchall():
            rows[r["id"]] = r
    if rec.get("lat") is not None:
        for r in c.execute("SELECT * FROM businesses WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                           (rec["lat"] - 0.004, rec["lat"] + 0.004, rec["lon"] - 0.004, rec["lon"] + 0.004)).fetchall():
            rows[r["id"]] = r
    best, best_sim = None, 0
    for r in rows.values():
        b = b_load(r)
        sim = jacc(rec["name_key"].split(), b["name_key"].split())
        if sim < 0.6:
            continue
        far, corr = place_rel(rec, b)
        if far:
            continue
        if corr and sim >= 0.8:
            if sim > best_sim:
                best, best_sim = b, sim
        elif not doubt or sim > 0.9:
            doubt = (b, "name-only match with no location evidence" if not corr else "similar name, not close enough to merge")
    if best:
        return "name", best, ""
    if doubt:
        return "review", doubt[0], doubt[1]
    return None, None, ""

def merge_rec(c, b, rec):
    day, src = rec["today"], b["sources"]
    for f in ("address", "locality", "city", "district", "state", "pincode", "website"):
        if rec[f] and (not b[f] or b[f] == rec[f]):
            b[f] = rec[f]
            add_source(src, f, rec[f], rec["src"], day)
    if b.get("lat") is None and rec.get("lat") is not None:
        b["lat"], b["lon"] = rec["lat"], rec["lon"]
        add_source(src, "location", "%s,%s" % (rec["lat"], rec["lon"]), rec["src"], day)
    add_source(src, "name", rec["name"], rec["src"], day)
    have = {p["n"] for p in b["phones"]}
    for n, t in rec["phones"]:
        if n not in have:
            b["phones"].append({"n": n, "t": t})
            have.add(n)
        add_source(src, "phone", n, rec["phone_src"] or rec["src"], day)
    for e in rec["emails"]:
        if e not in b["emails"]:
            b["emails"].append(e)
        add_source(src, "email", e, rec["src"], day)
    b["category_tags"] = list(dict.fromkeys(b["category_tags"] + rec["category_tags"]))
    b["last_seen"] = day
    if rec["phones"] or rec["emails"]:
        b["last_verified"] = day
    b["quality_score"] = score_of(b)
    c.execute("UPDATE businesses SET category_tags=?,address=?,locality=?,city=?,district=?,state=?,pincode=?,lat=?,lon=?,phones=?,emails=?,website=?,"
              "sources=?,quality_score=?,last_seen=?,last_verified=? WHERE id=?",
              (json.dumps(b["category_tags"]), b["address"], b["locality"], b["city"], b["district"], b["state"], b["pincode"], b["lat"], b["lon"],
               json.dumps(b["phones"]), json.dumps(b["emails"]), b["website"], json.dumps(b["sources"]), b["quality_score"], day,
               b["last_verified"], b["id"]))
    # status, last_contacted, owner_notes and do_not_contact are deliberately never touched here

def insert_rec(c, rec):
    day, src = rec["today"], {}
    for f in ("name", "address", "locality", "city", "district", "state", "pincode", "website"):
        add_source(src, f, rec[f], rec["src"], day)
    if rec.get("lat") is not None:
        add_source(src, "location", "%s,%s" % (rec["lat"], rec["lon"]), rec["src"], day)
    for n, t in rec["phones"]:
        add_source(src, "phone", n, rec["phone_src"] or rec["src"], day)
    for e in rec["emails"]:
        add_source(src, "email", e, rec["src"], day)
    b = {"phones": [{"n": n, "t": t} for n, t in rec["phones"]], "emails": rec["emails"], "website": rec["website"], "address": rec["address"],
         "locality": rec["locality"], "pincode": rec["pincode"], "lat": rec.get("lat"), "lon": rec.get("lon"), "category": rec["category"], "sources": src}
    cur = c.execute("INSERT INTO businesses(name,name_key,category,category_tags,address,locality,city,district,state,pincode,lat,lon,phones,emails,website,"
                    "sources,quality_score,first_seen,last_seen,last_verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rec["name"], rec["name_key"], rec["category"], json.dumps(rec["category_tags"]), rec["address"], rec["locality"], rec["city"],
                     rec["district"], rec["state"], rec["pincode"], rec.get("lat"), rec.get("lon"), json.dumps(b["phones"]), json.dumps(rec["emails"]),
                     rec["website"], json.dumps(src), score_of(b), day, day, day if (rec["phones"] or rec["emails"]) else ""))
    return cur.lastrowid

def link_campaign(c, cid, bid, action):
    c.execute("INSERT OR IGNORE INTO campaign_businesses(campaign_id,business_id,action,found_at) VALUES(?,?,?,?)", (cid, bid, action, now_day()))
    if action in ("new", "merged"):
        c.execute("UPDATE campaign_businesses SET action=? WHERE campaign_id=? AND business_id=? AND action='existing'", (action, cid, bid))

def upsert_business(cid, rec):
    ensure_businesses()
    c = conn()
    try:
        kind, b, reason = find_match(c, rec)
        if kind in ("phone", "website", "name"):
            merge_rec(c, b, rec)
            link_campaign(c, cid, b["id"], "merged")
            out = ("merged", b["id"], kind)
        elif kind == "review":
            dup = c.execute("SELECT id FROM business_review WHERE resolved='' AND candidate_id=? AND name_key=?", (b["id"], rec["name_key"])).fetchone()
            if not dup:
                c.execute("INSERT INTO business_review(campaign_id,candidate_id,name_key,reason,data,created) VALUES(?,?,?,?,?,?)",
                          (cid, b["id"], rec["name_key"], reason, json.dumps(rec), now_day()))
            out = ("review", b["id"], reason)
        else:
            bid = insert_rec(c, rec)
            link_campaign(c, cid, bid, "new")
            out = ("new", bid, "")
        c.commit()
        return out
    finally:
        c.close()

def master_save(cid, ctx, name, url, snippet, phones, emails, biz):
    nm = clean_name(name) if biz.get("from_title") else re.sub(r"\s+", " ", str(name or "")).strip()[:120]
    if not nm:
        return None
    addr = re.sub(r"\s+", " ", str(biz.get("address") or "")).strip()
    aliases = [a.lower() for a in ctx.get("aliases", []) if a]
    city_ok = biz.get("city_ok") or bool(addr and any(a in addr.lower() for a in aliases))
    city = biz.get("city") or (ctx.get("city", "") if city_ok else "")
    loc = biz.get("locality") or next((p for p in ctx.get("places", []) if p and p.lower() in addr.lower()), "")
    pin = biz.get("pincode") or (re.search(r"(?<!\d)\d{6}(?!\d)", addr).group(0) if re.search(r"(?<!\d)\d{6}(?!\d)", addr) else "")
    site = str(biz.get("website") or "").strip()
    if not (site.startswith("http") and not dir_domain(domain_of(site)) and homepage_only(site)):
        site = ""   # only a front-page style URL on a non-directory domain counts as the business's own website
    plist = []
    for p in phones or []:
        np_ = norm_phone("0" + str(p)[-10:] if str(p)[-10:] in TRUNK0 else p)
        if np_ and np_ not in plist:
            plist.append(np_)
    src = biz.get("src") or url
    rec = {"name": nm, "name_key": name_key_of(nm), "category": (ctx.get("entity") or "").lower(), "category_tags": list(biz.get("tags") or []),
           "address": addr, "locality": loc, "city": city, "district": biz.get("district") or "",
           "state": biz.get("state") or (ctx.get("state", "") if city else ""), "pincode": pin, "lat": biz.get("lat"), "lon": biz.get("lon"),
           "phones": plist, "emails": list(dict.fromkeys(str(e).lower() for e in (emails or []))), "website": site,
           "src": src, "phone_src": biz.get("phone_src") or "", "today": now_day()}
    res = upsert_business(cid, rec)
    cnt = BIZ_COUNTS.setdefault(cid, {"new": 0, "merged": 0, "review": 0})
    cnt[res[0]] += 1
    return res

def log_coverage(entity, place, found, pct):
    try:
        ensure_businesses()
        c = conn()
        c.execute("INSERT INTO coverage_log(entity,place,date,leads_found,coverage_estimate) VALUES(?,?,?,?,?)", (entity, place, now_day(), found, pct))
        c.commit()
        c.close()
    except Exception:
        pass

# ---- (5) existing businesses for this search
def cat_match(a, b):
    ka = {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", (a or "").lower())}
    kb = {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", (b or "").lower())}
    return bool(ka and kb and (ka <= kb or kb <= ka or jacc(ka, kb) >= 0.5))

def existing_for(entity, aliases):
    ensure_businesses()
    cutoff = (datetime.date.today() - datetime.timedelta(days=TRUST_DAYS)).isoformat()
    c = conn()
    rows = []
    for a in {x.lower() for x in aliases if x}:
        rows += c.execute("SELECT id, category, last_seen FROM businesses WHERE lower(city)=? AND last_seen>=?", (a, cutoff)).fetchall()
    c.close()
    rows = [r for r in {r["id"]: r for r in rows}.values() if cat_match(entity, r["category"])]
    return [r["id"] for r in rows], max([r["last_seen"] for r in rows] or [""])

def start_campaign(cid):
    try:
        c = conn()
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        c.close()
        term = next((t.strip() for t in camp["queries"].splitlines() if t.strip()), "")
        city, entity, terms, places, tags, aliases = plan_leads(cid, camp, term)
        ids, last = existing_for(entity, aliases)
        if ids:
            c = conn()
            c.execute("UPDATE campaigns SET prestart=? WHERE id=?", (json.dumps({"state": "ask", "n": len(ids), "last": last, "ids": ids}), cid))
            c.commit()
            c.close()
            log(cid, "You already have " + str(len(ids)) + " businesses for this search (last updated " + last + "). Choose below.")
            return
    except Exception as ex:
        log(cid, "  pre-check skipped: " + str(ex)[:100])
    run_campaign(cid)

@app.route("/campaign/<int:cid>/prestart", methods=["POST"])
def campaign_prestart(cid):
    c = conn()
    camp = c.execute("SELECT prestart FROM campaigns WHERE id=?", (cid,)).fetchone()
    pre = json.loads(camp["prestart"]) if camp and camp["prestart"] else {}
    if pre.get("state") != "ask":
        c.close()
        return redirect("/campaign/%d" % cid)
    choice = request.form.get("choice")
    for bid in pre.get("ids", []):
        link_campaign(c, cid, bid, "existing")
    pre["state"] = "more" if choice == "more" else "used"
    c.execute("UPDATE campaigns SET prestart=? WHERE id=?", (json.dumps(pre), cid))
    c.commit()
    c.close()
    if choice == "more":
        threading.Thread(target=run_campaign, args=(cid,), daemon=True).start()
        return redirect("/campaign/%d" % cid)
    return redirect("/businesses?campaign=%d" % cid)

# ---- (6) Businesses page
def biz_where(a):
    where, args = ["1=1"], []
    q = (a.get("q") or "").strip()
    if q:
        where.append("(name LIKE ? OR address LIKE ? OR phones LIKE ? OR emails LIKE ? OR website LIKE ? OR owner_notes LIKE ?)")
        args += ["%" + q + "%"] * 6
    for col in ("category", "state", "city", "status"):
        v = (a.get(col) or "").strip()
        if v:
            where.append("lower(" + col + ")=lower(?)")
            args.append(v)
    if a.get("has") == "phone":
        where.append("phones<>'[]'")
    elif a.get("has") == "email":
        where.append("emails<>'[]'")
    try:
        ms = int(a.get("min_score") or 0)
    except ValueError:
        ms = 0
    if ms:
        where.append("quality_score>=?")
        args.append(ms)
    if (a.get("campaign") or "").isdigit():
        where.append("id IN (SELECT business_id FROM campaign_businesses WHERE campaign_id=?)")
        args.append(int(a["campaign"]))
    return " AND ".join(where), args

BIZ_ORDER = {"score": "quality_score DESC, id", "name": "name COLLATE NOCASE", "recent": "last_seen DESC, id"}

def biz_row(r):
    b = b_load(r)
    ph = sorted(b["phones"], key=lambda p: p["t"] != "mobile")
    lines, urls = [], set()
    for field, vals in b["sources"].items():
        for val, lst in vals.items():
            for x in lst:
                lines.append("%s %s: %s (%s)" % (field, val, x.get("url"), x.get("date")))
                urls.add(x.get("url"))
    return {"id": b["id"], "score": b["quality_score"], "name": b["name"], "category": b["category"],
            "place": ", ".join(x for x in (b["locality"], b["city"], b["state"]) if x), "address": b["address"],
            "phones": [fmt_phone(p["n"]) + " (" + p["t"] + ")" for p in ph], "emails": b["emails"], "website": b["website"],
            "website_short": domain_of(b["website"]) if b["website"] else "", "status": b["status"], "dnc": b["do_not_contact"],
            "last_contacted": b["last_contacted"], "owner_notes": b["owner_notes"], "last_seen": b["last_seen"],
            "nsrc": len(urls), "sources_txt": "\n".join(lines[:40])}

@app.route("/businesses")
def businesses():
    ensure_businesses()
    a = request.args
    w, args = biz_where(a)
    try:
        page = max(int(a.get("page", 1)), 1)
    except ValueError:
        page = 1
    per = 50
    order = BIZ_ORDER.get(a.get("sort") or "score", BIZ_ORDER["score"])
    c = conn()
    total = c.execute("SELECT COUNT(*) FROM businesses WHERE " + w, args).fetchone()[0]
    pages = max((total + per - 1) // per, 1)
    page = min(page, pages)
    rows = [biz_row(r) for r in c.execute("SELECT * FROM businesses WHERE " + w + " ORDER BY " + order + " LIMIT ? OFFSET ?", args + [per, (page - 1) * per])]
    opts = {col: [r[0] for r in c.execute("SELECT DISTINCT " + col + " FROM businesses WHERE " + col + "<>'' ORDER BY 1")] for col in ("category", "state", "city", "status")}
    review_n = c.execute("SELECT COUNT(*) FROM business_review WHERE resolved=''").fetchone()[0]
    c.close()
    keys = ("q", "category", "state", "city", "status", "has", "min_score", "campaign", "sort")
    f = {k: a.get(k, "") for k in keys}
    qs = urllib.parse.urlencode({k: v for k, v in f.items() if v})
    return render_template_string(BIZ, rows=rows, total=total, page=page, pages=pages, opts=opts, f=f, qs=qs, review_n=review_n, bstatuses=BIZ_STATUSES)

@app.route("/api/business/<int:bid>", methods=["POST"])
def api_business(bid):
    j = request.get_json(force=True)
    status = j.get("status", "new")
    if status not in BIZ_STATUSES:
        return jsonify(ok=False, error="bad status"), 400
    c = conn()
    c.execute("UPDATE businesses SET status=?, owner_notes=?, last_contacted=?, do_not_contact=? WHERE id=?",
              (status, str(j.get("owner_notes", ""))[:2000], str(j.get("last_contacted", ""))[:10], 1 if j.get("do_not_contact") else 0, bid))
    c.commit()
    c.close()
    return jsonify(ok=True)

@app.route("/businesses/export.csv")
def businesses_export():
    ensure_businesses()
    w, args = biz_where(request.args)
    w += " AND do_not_contact=0 AND status<>'not interested' AND (phones<>'[]' OR emails<>'[]')"   # contactable leads only
    c = conn()
    rows = [b_load(r) for r in c.execute("SELECT * FROM businesses WHERE " + w + " ORDER BY quality_score DESC, id", args)]
    c.close()
    out = io.StringIO()
    wr = csv.writer(out)
    wr.writerow(["name", "category", "address", "locality", "city", "state", "pincode", "phones", "emails", "website", "score", "status",
                 "last_contacted", "owner_notes", "source_urls"])
    for b in rows:
        ph = sorted(b["phones"], key=lambda p: p["t"] != "mobile")
        urls = sorted({x["url"] for vals in b["sources"].values() for lst in vals.values() for x in lst if x.get("url")})
        wr.writerow([b["name"], b["category"], b["address"], b["locality"], b["city"], b["state"], b["pincode"],
                     "; ".join(p["n"] + " " + p["t"] for p in ph), "; ".join(b["emails"]), b["website"], b["quality_score"], b["status"],
                     b["last_contacted"], b["owner_notes"], " ".join(urls)])
    return Response("﻿" + out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=businesses.csv"})

@app.route("/businesses/review")
def businesses_review():
    ensure_businesses()
    c = conn()
    items = []
    for r in c.execute("SELECT * FROM business_review WHERE resolved='' ORDER BY id"):
        rec = json.loads(r["data"])
        cand = c.execute("SELECT * FROM businesses WHERE id=?", (r["candidate_id"],)).fetchone()
        if cand:
            items.append({"id": r["id"], "reason": r["reason"], "new": rec, "newphones": [fmt_phone(p[0]) for p in rec["phones"]],
                          "cand": biz_row(cand)})
    c.close()
    return render_template_string(REV, items=items)

@app.route("/businesses/review/<int:rid>", methods=["POST"])
def businesses_review_act(rid):
    act = request.form.get("action")
    c = conn()
    r = c.execute("SELECT * FROM business_review WHERE id=? AND resolved=''", (rid,)).fetchone()
    if r:
        rec = json.loads(r["data"])
        rec["phones"] = [tuple(p) for p in rec["phones"]]
        if act == "merge":
            cand = c.execute("SELECT * FROM businesses WHERE id=?", (r["candidate_id"],)).fetchone()
            merge_rec(c, b_load(cand), rec)
            link_campaign(c, r["campaign_id"], r["candidate_id"], "merged")
        elif act == "separate":
            link_campaign(c, r["campaign_id"], insert_rec(c, rec), "new")
        c.execute("UPDATE business_review SET resolved=? WHERE id=?", ({"merge": "merged", "separate": "separate"}.get(act, "dismissed"), rid))
        c.commit()
    c.close()
    return redirect("/businesses/review")

BIZ = HEAD + """
<div class='card'><h2>Businesses <small>({{total}} match{% if review_n %} &middot; <a href='/businesses/review'>{{review_n}} to review</a>{% endif %})</small></h2>
<form method='get' style='display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;align-items:center'>
<input name='q' value='{{f.q}}' placeholder='Search name, address, phone, email, notes' style='margin:0;flex:2;min-width:200px'>
{% for col, label in [('category','Category'),('state','State'),('city','City'),('status','Status')] %}
<select name='{{col}}' style='margin:0;width:auto'><option value=''>{{label}}: all</option>{% for v in opts[col] %}<option value='{{v}}' {{'selected' if f[col]==v}}>{{v}}</option>{% endfor %}</select>{% endfor %}
<select name='has' style='margin:0;width:auto'><option value=''>Contact: any</option><option value='phone' {{'selected' if f.has=='phone'}}>has phone</option><option value='email' {{'selected' if f.has=='email'}}>has email</option></select>
<input name='min_score' type='number' min='0' max='100' value='{{f.min_score}}' placeholder='min score' style='margin:0;width:100px'>
<select name='sort' style='margin:0;width:auto'><option value='score'>Sort: score</option><option value='name' {{'selected' if f.sort=='name'}}>name</option><option value='recent' {{'selected' if f.sort=='recent'}}>recently seen</option></select>
<input type='hidden' name='campaign' value='{{f.campaign}}'>
<button>Filter</button> <a class='btn grey' href='/businesses/export.csv?{{qs}}'>Export contactable CSV</a>
</form>
{% if f.campaign %}<p><small>Showing businesses linked to campaign {{f.campaign}}. <a href='/businesses'>Show all</a></small></p>{% endif %}
<table><tr><th>Score</th><th>Business</th><th>Place</th><th>Phones</th><th>Email / website</th><th>Status</th><th>Contacted / notes</th><th>Sources</th></tr>
{% for b in rows %}<tr>
<td><b>{{b.score}}</b></td>
<td><b>{{b.name}}</b><br><small>{{b.category}}</small></td>
<td>{{b.place}}<br><small>{{b.address}}</small></td>
<td>{% for p in b.phones %}{{p}}<br>{% endfor %}</td>
<td>{% for e in b.emails %}{{e}}<br>{% endfor %}{% if b.website %}<a href='{{b.website}}' target='_blank' rel='noopener'>{{b.website_short}}</a>{% endif %}</td>
<td><select id='st{{b.id}}' onchange='saveB({{b.id}})' style='margin:0'>{% for s in bstatuses %}<option value='{{s}}' {{'selected' if b.status==s}}>{{s}}</option>{% endfor %}</select>
<label style='font-size:12px'><input type='checkbox' id='dn{{b.id}}' {{'checked' if b.dnc}} onchange='saveB({{b.id}})' style='width:auto;margin:6px 4px 0 0'>do not contact</label></td>
<td><input type='date' id='lc{{b.id}}' value='{{b.last_contacted}}' onchange='saveB({{b.id}})' style='margin:0 0 4px'><input id='nt{{b.id}}' value='{{b.owner_notes}}' placeholder='notes' onchange='saveB({{b.id}})' style='margin:0'></td>
<td><small title='{{b.sources_txt}}'>{{b.nsrc}} source(s)<br>seen {{b.last_seen}}</small></td>
</tr>
{% else %}<tr><td colspan='8'>No businesses match yet. They are added when a campaign saves leads.</td></tr>{% endfor %}</table>
<p>Page {{page}} of {{pages}}{% if page>1 %} &middot; <a href='?{{qs}}&page={{page-1}}'>previous</a>{% endif %}{% if page<pages %} &middot; <a href='?{{qs}}&page={{page+1}}'>next</a>{% endif %}</p></div>
<script>
function saveB(id){var g=function(p){return document.getElementById(p+id);};
fetch('/api/business/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:g('st').value,owner_notes:g('nt').value,last_contacted:g('lc').value,do_not_contact:g('dn').checked?1:0})});}
</script></main></body></html>"""

REV = HEAD + """
<div class='card'><h2>Review doubtful matches <small>({{items|length}})</small></h2>
<p><small>A new find that looks like an existing business, but not certainly. Nothing is merged until you choose.</small></p>
{% for it in items %}<div style='border-top:1px solid #e5e7eb;padding:10px 0'>
<p><b>Why:</b> {{it.reason}}</p>
<table><tr><th></th><th>Name</th><th>Address</th><th>Phones</th></tr>
<tr><td>New find</td><td>{{it.new.name}}</td><td>{{it.new.locality}} {{it.new.address}}</td><td>{{it.newphones|join(', ')}}</td></tr>
<tr><td>Existing #{{it.cand.id}}</td><td>{{it.cand.name}}</td><td>{{it.cand.place}} {{it.cand.address}}</td><td>{{it.cand.phones|join(', ')}}</td></tr></table>
<small>New find source: {{it.new.src}}</small>
<form method='post' action='/businesses/review/{{it.id}}' style='margin-top:6px'>
<button name='action' value='merge'>Same business: merge</button>
<button name='action' value='separate' class='grey'>Different: keep separate</button>
<button name='action' value='dismiss' class='grey'>Dismiss</button></form></div>
{% else %}<p>Nothing to review.</p>{% endfor %}
<p><a href='/businesses'>Back to businesses</a></p></div></main></body></html>"""

if __name__ == "__main__":
    import sys
    if sys.stdout is None or sys.stderr is None:
        pathlib.Path(r"D:\vault\outputs\logs").mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(r"D:\vault\outputs\logs\command_centre.log", "a", buffering=1, encoding="utf-8")
    init()
    ensure_notes()
    ensure_tests()
    ensure_research()
    research_cleanup()
    print("Lead campaigns running. Open http://127.0.0.1:5000 in your browser. Keep this window open.")
    app.run(host="127.0.0.1", port=5000, debug=False)
