import sqlite3, threading, re, time, csv, io, urllib.parse, pathlib, subprocess, shutil, datetime
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

def scan(url):
    page = grab(url)
    extra = ""
    m = re.search(r'href=["\']([^"\']*contact[^"\']*)["\']', page, re.I)
    if m:
        extra = grab(urllib.parse.urljoin(url, m.group(1)))
    blob = page + " " + extra
    squeezed = re.sub(r"[\s\-\(\)\.]", "", blob)
    phones = []
    for p in PHONE.findall(squeezed):
        if p not in phones:
            phones.append(p)
    emails = []
    for e in EMAIL.findall(blob):
        e = e.lower().rstrip(".")
        if e.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")) or "example" in e or "sentry" in e:
            continue
        if e not in emails:
            emails.append(e)
    return phones[:3], emails[:2]

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

def run_campaign(cid):
    if cid in RUNNING:
        return
    RUNNING.add(cid)
    try:
        c = conn()
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        seen = {r["domain"] for r in c.execute("SELECT domain FROM leads WHERE campaign_id=?", (cid,))}
        c.close()
        log(cid, "=== Search started: " + camp["name"] + " ===")
        total = 0
        limit = camp["max_leads"]
        for term in [t.strip() for t in camp["queries"].splitlines() if t.strip()]:
            results = find(cid, term, camp["location"])
            if not results:
                log(cid, "  nothing found for this search")
                continue
            log(cid, "  " + str(len(results)) + " results")
            for r in results:
                if total >= limit:
                    break
                url = r.get("href") or ""
                if not url:
                    continue
                d = domain_of(url)
                if d in seen:
                    continue
                if any(d == s or d.endswith("." + s) for s in SKIP):
                    log(cid, "  skipped directory or social site: " + d)
                    continue
                seen.add(d)
                log(cid, "  opening " + d)
                phones, emails = scan(url)
                c = conn()
                c.execute("INSERT INTO leads(campaign_id,title,url,domain,snippet,phones,emails,created) VALUES(?,?,?,?,?,?,?,?)",
                          (cid, r.get("title") or d, url, d, r.get("body") or "", ",".join(phones), ",".join(emails),
                           time.strftime("%Y-%m-%d %H:%M")))
                c.commit()
                c.close()
                total += 1
                log(cid, "    saved: " + (r.get("title") or d)[:60] + " | phones: " + (", ".join(phones) or "none") +
                    " | emails: " + (", ".join(emails) or "none"))
                time.sleep(1)
            if total >= limit:
                break
        log(cid, "=== Search finished: " + str(total) + " new leads ===")
    except Exception as e:
        log(cid, "ERROR: " + str(e)[:200])
    finally:
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
</style></head><body><nav><div class='brand'>Command Centre</div><a href='/' class='{{"on" if request.path=="/"}}'>Home</a><a href='/campaigns' class='{{"on" if request.path.startswith("/campaign")}}'>Campaigns</a><a href='/leads' class='{{"on" if request.path=="/leads"}}'>All leads</a><a href='/digests' class='{{"on" if request.path.startswith("/digests")}}'>Digests</a><span class='soon'>Research (soon)</span><span class='soon'>Testing (soon)</span><span class='soon'>System (soon)</span></nav><main>"""

HOME = HEAD + """
<div class='card'><h2>New campaign</h2>
<form method='post' action='/campaign'>
<label>Campaign name</label><input name='name' required placeholder='e.g. Pathology labs in Buxar'>
<label>What are you looking for? (one per line if several)</label><textarea name='queries' rows='3' required placeholder='pathology lab&#10;diagnostic centre'></textarea>
<label>Country</label><select name='country' id='country' onchange='fillStates()'></select><label>State / region</label><select name='state' id='state'></select><label>City / town (optional)</label><input name='city' placeholder='Buxar'>
<label>What you are offering (used to write the first message)</label><textarea name='pitch' rows='3' required placeholder='LabFlow LIMS: lab software for billing, reports and patient records'></textarea>
<label>Message language</label><select name='language'><option>English</option><option>Hinglish</option><option>Hindi</option></select>
<label>Max leads</label><input name='max_leads' type='number' value='40' min='5' max='200'>
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
    c = conn()
    week = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    n_camp = c.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    n_leads = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    new_week = c.execute("SELECT COUNT(*) FROM leads WHERE created>=?", (week,)).fetchone()[0]
    worked = c.execute("SELECT COUNT(*) FROM leads WHERE status<>'New'").fetchone()[0]
    counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status")}
    recent = c.execute("SELECT l.*, c.name AS camp FROM leads l JOIN campaigns c ON c.id=l.campaign_id ORDER BY l.id DESC LIMIT 8").fetchall()
    running = [r for r in c.execute("SELECT id, name FROM campaigns") if r["id"] in RUNNING]
    c.close()
    return render_template_string(DASH, n_camp=n_camp, n_leads=n_leads, new_week=new_week, worked=worked, counts=counts,
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
    threading.Thread(target=run_campaign, args=(cid,), daemon=True).start()
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
    return render_template_string(CAMP, c=camp, leads=leads, statuses=STATUSES, qline=" | ".join(camp["queries"].splitlines()),
                                  total=total, page=page, pages=pages, q=q, status=status, has=has, qs=qs)

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

if __name__ == "__main__":
    init()
    print("Lead campaigns running. Open http://127.0.0.1:5000 in your browser. Keep this window open.")
    app.run(host="127.0.0.1", port=5000, debug=False)
