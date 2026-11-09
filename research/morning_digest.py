import feedparser, requests, datetime, urllib.parse, pathlib, os, smtplib, html, re, difflib
import yfinance as yf
import trafilatura
from googlenewsdecoder import gnewsdecoder
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv(r"D:\vault\automation\.env")

FOUNDER = "for an Indian founder"
TRADER = "for a stainless steel utensil trader in Buxar, Bihar"
SOFT = "for a software company selling LIMS, billing and hospital software to small labs, shops and clinics in Bihar"
WORLD = "and, for each, how it could affect India (oil and fuel prices, trade and exports, rupee and markets, supply chains)"

SECTIONS = {
    "Share market": ("Sensex OR Nifty OR Dalal Street OR India stock market", 8, FOUNDER),
    "Companies, earnings and IPOs": ("India quarterly results OR India IPO OR India company earnings", 6, FOUNDER),
    "Finance and economy": ("RBI OR India economy OR GST OR India inflation OR rupee", 6, FOUNDER),
    "World conflicts and geopolitics (India impact is inferred)": ("war OR ceasefire OR missile strike OR military conflict OR sanctions", 8, WORLD),
    "Government policy and MSME": ("MSME India OR India subsidy scheme OR Bihar government scheme OR GST council", 6, FOUNDER),
    "Commodities and metals": ("stainless steel price India OR steel price India OR gold price India OR crude oil price", 6, TRADER),
    "Tech and innovation (global)": ("artificial intelligence OR technology innovation OR semiconductor", 6, FOUNDER),
    "India tech and startups": ("India startup funding OR India tech OR India AI", 8, FOUNDER),
    "Healthcare and health-tech India": ("ABDM OR digital health India OR health tech India OR NMC India", 6, SOFT),
    "Your industry: LIMS, InventoryCloud, HMS": ("pathology lab software India OR billing software India OR hospital management system India", 6, SOFT),
    "Bihar and Buxar business": ("Bihar business OR Bihar industry OR Buxar", 6, TRADER),
}
TICKERS = {"Sensex": "^BSESN", "Nifty 50": "^NSEI", "Bank Nifty": "^NSEBANK", "USD/INR": "INR=X",
           "Gold (USD/oz)": "GC=F", "Brent crude (USD)": "BZ=F", "S&P 500": "^GSPC", "Nasdaq": "^IXIC"}

WIRE = ["reuters", "pti", "press trust of india", "ani", "pib", "rbi", "sebi", "bse", "nse", "the hindu",
        "associated press", "ap news", "bbc", "al jazeera", "bloomberg", "financial times", "wsj",
        "wall street journal", "nikkei", "afp"]
MAJOR = ["economic times", "et markets", "et healthworld", "livemint", "mint", "business standard", "moneycontrol",
         "financial express", "cnbc", "ndtv", "times of india", "indian express", "hindustan times", "theprint",
         "the print", "scroll", "techcrunch", "the verge", "inc42", "yourstory", "entrackr", "medianama",
         "india today", "the economist", "forbes", "fortune", "the guardian", "new york times",
         "washington post", "dw", "france 24", "cnn", "sky news"]
BADGE = {2: "wire/official", 1: "major outlet", 0: "unrated - verify"}

MODEL = "qwen2.5:3b"
OLLAMA = "http://127.0.0.1:11434/api/generate"
OUT = pathlib.Path(r"D:\vault\outputs\reports")
STATE = {"fails": 0}
LOG = pathlib.Path(r"D:\vault\outputs\logs\digest.log")
def log(msg):
    line = datetime.datetime.now().strftime("%H:%M:%S") + "  " + msg
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass

NUM = re.compile(r"(\d[\d,\.]*\s?(%|per cent|percent|crore|lakh|billion|million|trillion|bps|basis points|points)|\u20b9\s?\d|Rs\.?\s?\d|\$\s?\d|USD\s?\d)", re.I)
MOVE = re.compile(r"\b(rose|rises|fell|falls|gained|gains|declined|dropped|drops|jumped|surged|slipped|slumped|up|down|higher|lower|increase|decrease|cut|hike|hiked|raised)\b", re.I)

def has(s, k):
    return re.search(r"\b" + re.escape(k) + r"\b", s) is not None

def tier_of(src):
    s = src.lower()
    if any(has(s, k) for k in WIRE):
        return 2
    if any(has(s, k) for k in MAJOR):
        return 1
    return 0

def ask(prompt):
    try:
        r = requests.post(OLLAMA, json={"model": MODEL, "prompt": prompt, "stream": False,
                                        "options": {"num_ctx": 3072}}, timeout=600)
        return r.json()["response"].strip()
    except Exception:
        return ""

def norm(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower())

def fetch(query, pool=30):
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query + " when:1d") + "&hl=en-IN&gl=IN&ceid=IN:en"
    out = []
    for e in feedparser.parse(url).entries[:pool]:
        title = e.title
        src = ""
        try:
            src = e.source.title
        except Exception:
            pass
        if " - " in title:
            head, tail = title.rsplit(" - ", 1)
            if not src:
                src = tail
            title = head
        when = ""
        try:
            d = datetime.datetime(*e.published_parsed[:6]) + datetime.timedelta(hours=5, minutes=30)
            when = d.strftime("%d %b %H:%M")
        except Exception:
            pass
        out.append({"title": title, "link": e.link, "src": src, "when": when, "tier": tier_of(src), "key": norm(title)})
    return out

def cluster(items):
    groups = []
    for it in items:
        for g in groups:
            if difflib.SequenceMatcher(None, it["key"], g[0]["key"]).ratio() > 0.6:
                g.append(it)
                break
        else:
            groups.append([it])
    res = []
    for g in groups:
        best = max(g, key=lambda x: x["tier"])
        names = sorted({x["src"] for x in g if x["src"]})
        res.append({"best": best, "outlets": len(names), "names": names, "tier": best["tier"], "figs": []})
    return res

def pick(groups, n, seen):
    groups = [g for g in groups if g["best"]["key"] not in seen]
    keep = [g for g in groups if g["tier"] >= 1 or g["outlets"] >= 2]
    if len(keep) < 3:
        keep = groups
    keep.sort(key=lambda g: g["tier"] * 2 + min(g["outlets"], 4), reverse=True)
    keep = keep[:n]
    for g in keep:
        seen.add(g["best"]["key"])
    return keep

def figures(url):
    if STATE["fails"] >= 4:
        return "", []
    real = None
    try:
        d = gnewsdecoder(url, interval=1)
        if isinstance(d, dict) and d.get("status"):
            real = d.get("decoded_url")
    except Exception:
        real = None
    if not real:
        log("    could not resolve article link, using Google link")
        STATE["fails"] += 1
        return "", []
    STATE["fails"] = 0
    try:
        text = trafilatura.extract(trafilatura.fetch_url(real)) or ""
    except Exception:
        text = ""
    sents = re.split(r"(?<=[.!?])\s+", text)
    good = [s.strip() for s in sents if NUM.search(s) and MOVE.search(s) and 40 < len(s) < 260]
    return real, good[:2]

def bullets_of(text):
    return [l.lstrip("-*\u2022 ").strip() for l in text.splitlines() if l.strip()]

def linkify(text, items):
    t = html.escape(text)
    def rep(m):
        n = int(m.group(1))
        if 1 <= n <= len(items):
            return '<a href="' + html.escape(items[n - 1]["best"]["url"]) + '">[' + str(n) + "]</a>"
        return ""
    return re.sub(r"\[(\d+)\]", rep, t)

def cell(last, base):
    ch = last - base
    pc = ch / base * 100
    col = "#0a7d2c" if ch >= 0 else "#c62828"
    return "<td align=right style='color:%s'>%s (%s)</td>" % (col, "{:+,.2f}".format(ch), "{:+.2f}%".format(pc))

def market_table():
    rows = []
    for name, t in TICKERS.items():
        link = "https://finance.yahoo.com/quote/" + urllib.parse.quote(t)
        try:
            h = yf.Ticker(t).history(period="1mo")["Close"].dropna()
            last, d1, w, m = float(h.iloc[-1]), float(h.iloc[-2]), float(h.iloc[-6]), float(h.iloc[0])
            rows.append("<tr><td><a href='%s'>%s</a></td><td align=right>%s</td>%s%s%s</tr>"
                        % (link, name, "{:,.2f}".format(last), cell(last, d1), cell(last, w), cell(last, m)))
        except Exception:
            rows.append("<tr><td><a href='%s'>%s</a></td><td colspan=4>unavailable</td></tr>" % (link, name))
    head = "<tr><th align=left>Instrument</th><th>Last close</th><th>1 day</th><th>5 days</th><th>1 month</th></tr>"
    return ("<h2>Market snapshot (last close)</h2><table cellpadding=6 style='border-collapse:collapse'>" + head + "".join(rows) +
            "</table><p><small>Source: Yahoo Finance, delayed and unofficial. Click a name to check the live quote.</small></p>")

def build():
    today = datetime.date.today().strftime("%A, %d %B %Y")
    seen, sections, allg = set(), [], []
    for section, (query, n, angle) in SECTIONS.items():
        log("SECTION: " + section)
        raw = fetch(query)
        log("  fetched " + str(len(raw)) + " headlines")
        items = pick(cluster(raw), n, seen)
        log("  kept " + str(len(items)) + " stories after source and duplicate filter")
        sections.append("<h2>" + html.escape(section) + "</h2>")
        if not items:
            sections.append("<p>No new stories in the last 24 hours.</p>")
            continue
        for idx, g in enumerate(items):
            b = g["best"]
            b["url"] = b["link"]
            if idx < 5:
                log("  opening article " + str(idx + 1) + ": " + b["title"][:70])
                real, figs = figures(b["link"])
                log("    figures found: " + str(len(figs)))
                if real:
                    b["url"] = real
                g["figs"] = figs
            allg.append((section, g))
        lines = []
        for i, g in enumerate(items, 1):
            line = str(i) + ". " + g["best"]["title"]
            if g["figs"]:
                line += " | Figures: " + " ".join(g["figs"])
            lines.append(line)
        prompt = ("Section: " + section + "\nStories:\n" + "\n".join(lines) +
                  "\n\nWrite 3-4 short bullet points with the key takeaways " + angle +
                  ". Include the specific numbers and percentage changes given in the stories. "
                  "Never state a number or fact that is not in the stories above. "
                  "Start each line with a dash and end it with the story numbers it comes from, like [2][5].")
        log("  asking local AI for takeaways...")
        bl = bullets_of(ask(prompt))
        log("  takeaways done: " + str(len(bl)) + " bullets")
        if bl:
            sections.append("<ul>" + "".join("<li>" + linkify(x, items) + "</li>" for x in bl) + "</ul>")
        sections.append("<p><b>Stories</b></p><ul style='list-style:none;padding-left:0'>")
        for i, g in enumerate(items, 1):
            b = g["best"]
            meta = ", ".join(x for x in (b["src"], b["when"]) if x)
            extra = " | " + BADGE[g["tier"]]
            if g["outlets"] > 1:
                extra += " | carried by " + str(g["outlets"]) + " outlets: " + ", ".join(g["names"][:5])
            li = ("<li style='margin-bottom:10px'><b>[" + str(i) + "]</b> <a href='" + html.escape(b["url"]) + "'>" +
                  html.escape(b["title"]) + "</a> <small>(" + html.escape(meta) + html.escape(extra) + ")</small>")
            for f in g["figs"]:
                li += "<div style='margin:4px 0 0 18px;color:#444'><i>From the article:</i> " + html.escape(f) + "</div>"
            sections.append(li + "</li>")
        sections.append("</ul>")
    log("Building market table and final page...")
    top = [x for x in sorted(allg, key=lambda x: (x[1]["outlets"], x[1]["tier"]), reverse=True)[:6] if x[1]["outlets"] >= 2]
    topht = ""
    if top:
        topht = "<h2>Most widely reported today</h2><ol>" + "".join(
            "<li><a href='" + html.escape(g["best"]["url"]) + "'>" + html.escape(g["best"]["title"]) + "</a> <small>(" +
            str(g["outlets"]) + " outlets, " + html.escape(sec) + ")</small></li>" for sec, g in top) + "</ol>"
    note = ("<p style='background:#f4f4f4;padding:8px'><small><b>How to read this.</b> Every story links to the publisher. "
            "Quoted figures are copied from the article, not written by the AI. Takeaway lines are an AI summary; click the "
            "[numbers] to check them. Source rating: wire/official = news agencies, regulators, exchanges and major international "
            "outlets; major outlet = established business and tech press; unrated = not on the script's list, verify before relying. "
            "Unrated stories appear only when two or more outlets carry them. Open the article before acting on any figure.</small></p>")
    page = ("<html><head><meta charset='utf-8'></head><body style='font-family:Arial,sans-serif;max-width:800px'>"
            "<h1>Morning digest - " + today + "</h1>" + note + topht + market_table() + "".join(sections) + "</body></html>")
    return today, page

def send(subject, page):
    user, pw, to = os.getenv("EMAIL_USER"), os.getenv("EMAIL_APP_PASSWORD"), os.getenv("EMAIL_TO")
    if not (user and pw and to) or "you@" in user:
        print("Email not configured, saved file only.")
        return
    m = MIMEMultipart("alternative")
    m["Subject"], m["From"], m["To"] = subject, user, to
    m.attach(MIMEText(page, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [to], m.as_string())
    print("Email sent to", to)

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    log("=== Digest started ===")
    today, page = build()
    log("=== Digest finished ===")
    f = OUT / ("digest-" + datetime.date.today().isoformat() + ".html")
    f.write_text(page, encoding="utf-8")
    print("Saved", f)
    send("Morning digest - " + today, page)

main()

