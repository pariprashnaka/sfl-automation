"""Outreach tab for a campaign: draft, review, approve, WhatsApp hand-off, gated email sending.
Registered from leads_app.py with outreach.register(module). Nothing is ever sent automatically over WhatsApp.
Email sending stays off until SMTP_* exist in the environment and the setting is switched on.
"""
import json, re, time, random, threading, smtplib, urllib.parse, datetime
from email.message import EmailMessage
from flask import request, jsonify, render_template_string

M = None                      # the leads_app module (conn, env_val, HEAD, qset, ...)
JOBS = {}                     # cid -> {"total","done","running","errors"}
SEND = {"running": False, "sent": 0, "next": ""}
JLOCK = threading.Lock()

SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_FROM_NAME")
DAILY_CAP = 25
GAP = (60, 150)
WINDOW = (9, 19)
LANGS = ["English", "Hindi", "Hinglish", "Bengali", "Marathi", "Gujarati", "Odia", "Tamil", "Telugu", "Kannada", "Punjabi"]
PITCHES = {"inventory": "InventoryCloud inventory software", "hms": "HMS/LIMS hospital and lab software"}
DEFAULTS = {
    "out_signature": "Regards,\n[Your name]\n[Your company]\n[Your phone]",
    "out_pitch_inventory": "InventoryCloud is inventory software that helps a business keep track of stock, purchases and sales in one place.",
    "out_pitch_hms": "We make hospital management (HMS) and laboratory (LIMS) software that helps a hospital or lab manage patients, billing and reports in one place.",
    "out_optout": "If you would rather not hear from us, just reply \"unsubscribe\" and we will not contact you again.",
    "out_wa_optout": "Reply STOP and we will not message again.",
    "out_email_on": "0",
}

def setting(k):
    return M.qset(k, DEFAULTS.get(k, ""))

def ensure():
    M.ensure_businesses()
    c = M.conn()
    c.executescript("""CREATE TABLE IF NOT EXISTS outreach(lead_id INTEGER PRIMARY KEY, channel TEXT DEFAULT 'email', pitch TEXT DEFAULT '',
      language TEXT DEFAULT '', subject TEXT DEFAULT '', body TEXT DEFAULT '', status TEXT DEFAULT 'drafted', approved INTEGER DEFAULT 0,
      sent_at TEXT DEFAULT '', error TEXT DEFAULT '', updated TEXT DEFAULT '');""")
    try:
        c.execute("ALTER TABLE leads ADD COLUMN dnc INTEGER DEFAULT 0")
    except Exception:
        pass
    c.commit()
    c.close()

def smtp_cfg():
    return {k: M.env_val(k) for k in SMTP_KEYS}

def email_ready():
    # True only when every SMTP variable exists AND the owner switched sending on.
    return all(smtp_cfg().values()) and setting("out_email_on") == "1"

def sent_today():
    c = M.conn()
    n = c.execute("SELECT COUNT(*) FROM outreach WHERE channel='email' AND status='sent' AND sent_at LIKE ?", (time.strftime("%Y-%m-%d") + "%",)).fetchone()[0]
    c.close()
    return n

# ---------- lead data ----------
def first_mobile(phones):
    for p in (phones or "").split(","):
        r = M.norm_phone(p)
        if r and r[1] == "mobile":
            return r[0][3:]
    return ""

def first_email(emails):
    for e in (emails or "").split(","):
        e = e.strip()
        if "@" in e:
            return e
    return ""

def lead_rows(cid):
    c = M.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    leads = c.execute("SELECT * FROM leads WHERE campaign_id=? AND wrong=0 AND COALESCE(dnc,0)=0 ORDER BY id", (cid,)).fetchall()
    bizs = c.execute("SELECT b.* FROM businesses b JOIN campaign_businesses cb ON cb.business_id=b.id WHERE cb.campaign_id=?", (cid,)).fetchall()
    out = {r["lead_id"]: r for r in c.execute("SELECT * FROM outreach WHERE lead_id IN (SELECT id FROM leads WHERE campaign_id=?)", (cid,))}
    c.close()
    res = []
    for l in leads:
        b = M.match_biz(bizs, l["title"] or "", [x for x in (l["phones"] or "").split(",") if x]) if bizs else None
        if b is not None and (b["do_not_contact"] or b["status"] == "wrong"):
            continue
        o = out.get(l["id"])
        loc = (camp["location"] or "")
        if b is not None and (b["city"] or "").strip() and (b["city"] or "").strip().lower() not in loc.lower():
            b = None          # same name but a different city: not this lead, so use none of its facts
        city = (b["city"] if b else "") or loc.split(",")[0].strip()
        website = (b["website"] if b else "") or ""
        if not website and l["domain"] and not any(l["domain"].endswith(s) for s in M.SKIP) and "openstreetmap" not in (l["domain"] or ""):
            website = l["url"] or ""
        has_body = bool(o and (o["body"] or "").strip())
        status = "not drafted"
        if o and o["status"] == "sent":
            status = "sent"
        elif o and o["status"] == "failed":
            status = "failed"
        elif has_body:
            status = "approved" if o["approved"] else "drafted"
        mob = first_mobile(l["phones"])
        res.append({"id": l["id"], "name": M.clean_name(l["title"] or "") or (l["title"] or ""), "city": city, "email": first_email(l["emails"]),
                    "phone": mob or ((l["phones"] or "").split(",")[0] if l["phones"] else ""), "mobile": mob, "website": website,
                    "type": (b["category"] if b else "") or "", "lead_status": l["status"], "status": status,
                    "channel": o["channel"] if o else "", "subject": o["subject"] if o else "", "body": o["body"] if o else "",
                    "language": o["language"] if o else "", "pitch": o["pitch"] if o else "", "error": o["error"] if o else "",
                    "sent_at": o["sent_at"] if o else "",
                    "wa": ("https://wa.me/91" + mob + "?text=" + urllib.parse.quote(o["body"] or "", safe="")) if (mob and has_body and o["channel"] == "whatsapp") else ""})
    return camp, res

def facts(lead, camp):
    f = ["Business name: " + lead["name"]]
    if lead["city"]:
        f.append("City: " + lead["city"])
    if lead["type"]:
        f.append("Business type: " + lead["type"])
    if lead["website"]:
        f.append("Website: " + lead["website"])
    return "\n".join(f)

# ---------- drafting ----------
def write_one(lead, camp, pitch, language, channel):
    pt = setting("out_pitch_" + pitch)
    sig = setting("out_signature").strip()
    if channel == "email":
        shape = ("First line: SUBJECT: followed by a short plain subject (under 8 words). Then a blank line, then the email: a greeting to the business, "
                 "2 to 3 short sentences, and one polite question asking if they would like a quick 10-minute demo or call. Under 90 words. Do not write a signature.")
    else:
        shape = ("A WhatsApp message under 60 words: a greeting, one line on what we offer, and a polite question asking if they would like a quick demo. "
                 "Do not write a signature. No subject line.")
    prompt = ("You write short, polite first messages for a small Indian software company.\n"
              "What we offer: " + pt + "\n"
              "FACTS about the recipient (use only these, nothing else):\n" + facts(lead, camp) + "\n"
              "Language: " + language + "\n\n" + shape + "\n"
              "Strict rules: use ONLY the facts above. Do not invent facts, numbers, clients, results, prices or claims about us or them. "
              "No emojis, no hype, no pricing. Plain, respectful tone. Output only the message.")
    txt = (M.ai("draft", prompt) or "").strip()
    if not txt:
        raise ValueError("empty answer")
    subject = ""
    if channel == "email":
        m = re.match(r"\s*SUBJECT:\s*(.+)", txt, re.I)
        if m:
            subject = m.group(1).strip().strip('"')
            txt = txt[m.end():].strip()
        if not subject:
            subject = "Quick question about " + (lead["name"] or "your business")
    txt = txt.replace("‑", "-").replace("–", "-").replace("—", "-")
    txt = txt.replace("‑", "-").replace("–", "-").replace("—", "-")
    txt = re.sub(r"^\s*(subject|message|email)\s*:.*\n", "", txt, flags=re.I).strip()
    body = txt + "\n\n" + sig
    body += "\n\n" + (setting("out_optout") if channel == "email" else setting("out_wa_optout"))
    return subject[:150], body.strip()

def save_draft(lid, channel, pitch, language, subject, body, status="drafted"):
    c = M.conn()
    c.execute("INSERT OR REPLACE INTO outreach(lead_id,channel,pitch,language,subject,body,status,approved,sent_at,error,updated) VALUES(?,?,?,?,?,?,?,0,'','',?)",
              (lid, channel, pitch, language, subject, body, status, time.strftime("%Y-%m-%d %H:%M")))
    c.commit()
    c.close()

def run_generate(cid, ids, pitch, language, channel):
    with JLOCK:
        if JOBS.get(cid, {}).get("running"):
            return
        JOBS[cid] = {"total": len(ids), "done": 0, "running": True, "errors": 0}
    try:
        camp, rows = lead_rows(cid)
        by = {r["id"]: r for r in rows}
        for lid in ids:
            r = by.get(lid)
            if r and r["status"] != "sent":
                try:
                    s, b = write_one(r, camp, pitch, language, channel)
                    save_draft(lid, channel, pitch, language, s, b)
                except Exception as e:
                    JOBS[cid]["errors"] += 1
                    c = M.conn()
                    c.execute("INSERT OR REPLACE INTO outreach(lead_id,channel,pitch,language,subject,body,status,approved,error,updated) VALUES(?,?,?,?,'','','failed',0,?,?)",
                              (lid, channel, pitch, language, str(e)[:160], time.strftime("%Y-%m-%d %H:%M")))
                    c.commit()
                    c.close()
            JOBS[cid]["done"] += 1
    finally:
        JOBS[cid]["running"] = False

# ---------- email sending (kept off until configured; never exercised by tests) ----------
def build_msg(to, subject, body, cfg):
    m = EmailMessage()
    m["From"] = (cfg["SMTP_FROM_NAME"] + " <" + cfg["SMTP_FROM"] + ">") if cfg["SMTP_FROM_NAME"] else cfg["SMTP_FROM"]
    m["To"] = to
    m["Subject"] = subject
    m.set_content(body)
    return m

def smtp_send(msg):
    cfg = smtp_cfg()
    port = int(cfg["SMTP_PORT"])
    if port == 465:
        s = smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, timeout=30)
    else:
        s = smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=30)
        s.starttls()
    try:
        s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
        s.send_message(msg)
    finally:
        s.quit()

def in_window():
    return WINDOW[0] <= datetime.datetime.now().hour < WINDOW[1]

def send_worker(cid):
    with JLOCK:
        if SEND["running"]:
            return
        SEND["running"] = True
    try:
        cfg = smtp_cfg()
        camp, rows = lead_rows(cid)
        queue = [r for r in rows if r["status"] == "approved" and r["channel"] == "email" and r["email"]]
        for r in queue:
            if not email_ready() or sent_today() >= DAILY_CAP or not in_window():
                break
            try:
                smtp_send(build_msg(r["email"], r["subject"], r["body"], cfg))
                mark_sent(r["id"], "email")
            except Exception as e:
                c = M.conn()
                c.execute("UPDATE outreach SET status='failed', error=? WHERE lead_id=?", (str(e)[:160], r["id"]))
                c.commit()
                c.close()
            wait = random.randint(*GAP)
            SEND["next"] = time.strftime("%H:%M:%S", time.localtime(time.time() + wait))
            time.sleep(wait)
    finally:
        SEND["running"] = False
        SEND["next"] = ""

def mark_sent(lid, channel):
    now = time.strftime("%Y-%m-%d %H:%M")
    c = M.conn()
    c.execute("UPDATE outreach SET status='sent', approved=1, sent_at=?, error='' WHERE lead_id=?", (now, lid))
    c.execute("UPDATE leads SET status='Contacted' WHERE id=?", (lid,))
    l = c.execute("SELECT title, phones, campaign_id FROM leads WHERE id=?", (lid,)).fetchone()
    bizs = c.execute("SELECT b.* FROM businesses b JOIN campaign_businesses cb ON cb.business_id=b.id WHERE cb.campaign_id=?", (l["campaign_id"],)).fetchall()
    b = M.match_biz(bizs, l["title"] or "", [x for x in (l["phones"] or "").split(",") if x])
    if b:
        c.execute("UPDATE businesses SET status='contacted', last_contacted=? WHERE id=?", (now[:10], b["id"]))
    c.commit()
    c.close()

# ---------- routes ----------
def summary(rows):
    return {"total": len(rows), "email": sum(1 for r in rows if r["email"]), "phone": sum(1 for r in rows if r["mobile"]),
            "drafted": sum(1 for r in rows if r["status"] in ("drafted", "approved", "sent")),
            "approved": sum(1 for r in rows if r["status"] == "approved"), "sent": sum(1 for r in rows if r["status"] == "sent")}

def settings_obj():
    return {k: setting(k) for k in ("out_signature", "out_pitch_inventory", "out_pitch_hms", "out_optout", "out_wa_optout")}

def register(mod):
    global M
    M = mod
    app = mod.app
    ensure()

    @app.route("/campaign/<int:cid>/outreach")
    def outreach_page(cid):
        c = M.conn()
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        c.close()
        if not camp:
            return "Not found", 404
        lang = camp["language"] if camp["language"] in LANGS else "English"
        pitch = "hms" if re.search(r"hospital|lab|clinic|diagnos|pathol", (camp["pitch"] or "") + (camp["queries"] or ""), re.I) else "inventory"
        head = render_template_string(M.HEAD)
        page = (PAGE.replace("__CID__", str(cid)).replace("__NAME__", re.sub(r"[<>&]", "", camp["name"] or ""))
                .replace("__LANG__", lang).replace("__PITCH__", pitch)
                .replace("__LANGS__", "".join("<option%s>%s</option>" % (" selected" if l == lang else "", l) for l in LANGS)))
        return head + page

    @app.route("/api/outreach/<int:cid>")
    def outreach_data(cid):
        camp, rows = lead_rows(cid)
        with JLOCK:
            job = dict(JOBS.get(cid, {}))
        cfg = smtp_cfg()
        missing = [k for k in SMTP_KEYS if not cfg[k]]
        return jsonify(rows=rows, summary=summary(rows), job=job, settings=settings_obj(),
                       email={"ready": email_ready(), "missing": missing, "on": setting("out_email_on") == "1", "today": sent_today(), "cap": DAILY_CAP,
                              "in_window": in_window(), "sending": SEND["running"], "next": SEND["next"]})

    @app.route("/api/outreach/<int:cid>/generate", methods=["POST"])
    def outreach_generate(cid):
        j = request.get_json(force=True) or {}
        ids = [int(x) for x in j.get("ids", [])]
        pitch = j.get("pitch") if j.get("pitch") in PITCHES else "inventory"
        lang = j.get("language") if j.get("language") in LANGS else "English"
        channel = "whatsapp" if j.get("channel") == "whatsapp" else "email"
        if not ids:
            return jsonify(ok=False, error="Select at least one lead"), 400
        if JOBS.get(cid, {}).get("running"):
            return jsonify(ok=False, error="Drafts are already being written"), 409
        threading.Thread(target=run_generate, args=(cid, ids, pitch, lang, channel), daemon=True).start()
        return jsonify(ok=True)

    @app.route("/api/outreach/lead/<int:lid>", methods=["POST"])
    def outreach_save(lid):
        j = request.get_json(force=True) or {}
        c = M.conn()
        o = c.execute("SELECT * FROM outreach WHERE lead_id=?", (lid,)).fetchone()
        if not o:
            c.close()
            return jsonify(ok=False, error="No draft yet"), 404
        if "subject" in j or "body" in j:
            c.execute("UPDATE outreach SET subject=?, body=?, status=CASE WHEN status='failed' THEN 'drafted' ELSE status END, approved=0, updated=? WHERE lead_id=?",
                      (str(j.get("subject", o["subject"]))[:200], str(j.get("body", o["body"]))[:4000], time.strftime("%Y-%m-%d %H:%M"), lid))
        if "approved" in j and (o["body"] or j.get("body")):
            c.execute("UPDATE outreach SET approved=? WHERE lead_id=? AND status<>'sent'", (1 if j["approved"] else 0, lid))
        c.commit()
        c.close()
        return jsonify(ok=True)

    @app.route("/api/outreach/bulk", methods=["POST"])
    def outreach_bulk():
        j = request.get_json(force=True) or {}
        ids = [int(x) for x in j.get("ids", [])]
        val = 1 if j.get("action") == "approve" else 0
        c = M.conn()
        for i in ids:
            c.execute("UPDATE outreach SET approved=? WHERE lead_id=? AND status<>'sent' AND body<>''", (val, i))
        c.commit()
        c.close()
        return jsonify(ok=True)

    @app.route("/api/outreach/lead/<int:lid>/dnc", methods=["POST"])
    def outreach_dnc(lid):
        c = M.conn()
        l = c.execute("SELECT title, phones, campaign_id FROM leads WHERE id=?", (lid,)).fetchone()
        if not l:
            c.close()
            return jsonify(ok=False), 404
        c.execute("UPDATE leads SET dnc=1 WHERE id=?", (lid,))
        bizs = c.execute("SELECT b.* FROM businesses b JOIN campaign_businesses cb ON cb.business_id=b.id WHERE cb.campaign_id=?", (l["campaign_id"],)).fetchall()
        b = M.match_biz(bizs, l["title"] or "", [x for x in (l["phones"] or "").split(",") if x])
        if b:
            c.execute("UPDATE businesses SET do_not_contact=1 WHERE id=?", (b["id"],))
        c.commit()
        c.close()
        return jsonify(ok=True)

    @app.route("/api/outreach/lead/<int:lid>/wa-sent", methods=["POST"])
    def outreach_wa_sent(lid):
        mark_sent(lid, "whatsapp")
        return jsonify(ok=True)

    @app.route("/api/outreach/settings", methods=["POST"])
    def outreach_settings():
        j = request.get_json(force=True) or {}
        for k in ("out_signature", "out_pitch_inventory", "out_pitch_hms", "out_optout", "out_wa_optout"):
            if k in j:
                M.qset_save(k, str(j[k])[:1500])
        if "email_on" in j:
            M.qset_save("out_email_on", "1" if j["email_on"] else "0")
        return jsonify(ok=True)

    @app.route("/api/outreach/<int:cid>/send", methods=["POST"])
    def outreach_send(cid):
        if not email_ready():
            return jsonify(ok=False, error="Email sending is not set up yet"), 400
        threading.Thread(target=send_worker, args=(cid,), daemon=True).start()
        return jsonify(ok=True)

    @app.route("/api/outreach/send-test", methods=["POST"])
    def outreach_send_test():
        if not email_ready():
            return jsonify(ok=False, error="Email sending is not set up yet"), 400
        cfg = smtp_cfg()
        try:
            smtp_send(build_msg(cfg["SMTP_FROM"], "Test from Command Centre", "This is a test message from your outreach tool.", cfg))
        except Exception as e:
            return jsonify(ok=False, error=str(e)[:160]), 500
        return jsonify(ok=True)

PAGE = r"""
<style>
.oc{--bg:#f5f6f8;--card:#fff;--ink:#111827;--mute:#4b5563;--line:#e5e7eb;--brand:#2563eb;--brand-d:#1d4ed8;--brand-l:#eff6ff;
 --ok:#047857;--ok-bg:#d1fae5;--warn:#92400e;--warn-bg:#fef3c7;--bad:#b91c1c;--bad-bg:#fee2e2;--info:#1e40af;--info-bg:#dbeafe;--grey-bg:#f3f4f6;
 --sh:0 1px 2px rgba(17,24,39,.06),0 4px 16px rgba(17,24,39,.06);--r:12px;
 font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);font-size:15px;line-height:1.5}
.oc *{box-sizing:border-box}
.oc .tabs{display:flex;gap:8px;margin:0 0 16px;border-bottom:1px solid var(--line)}
.oc .tabs a{padding:8px 16px;text-decoration:none;color:var(--mute);border-bottom:2px solid transparent;font-weight:600}
.oc .tabs a.on{color:var(--brand);border-color:var(--brand)}
.oc h2{margin:0 0 8px;font-size:22px}
.oc .banner{display:flex;gap:16px;align-items:center;background:var(--warn-bg);color:var(--warn);border-radius:var(--r);padding:16px;margin-bottom:16px;font-weight:600}
.oc .banner.ok{background:var(--ok-bg);color:var(--ok)}
.oc .cards{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:16px}
.oc .sc{background:var(--card);border-radius:var(--r);box-shadow:var(--sh);padding:16px}
.oc .sc b{display:block;font-size:28px;line-height:1.2}.oc .sc span{color:var(--mute);font-size:13px}
.oc .panel{background:var(--card);border-radius:var(--r);box-shadow:var(--sh);padding:16px;margin-bottom:16px}
.oc .bar{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end}
.oc label{display:block;font-size:13px;color:var(--mute);font-weight:600}
.oc select,.oc input[type=text],.oc textarea{width:100%;padding:8px 16px 8px 8px;margin:4px 0 0;border:1px solid #d1d5db;border-radius:8px;font:inherit;background:#fff;color:var(--ink)}
.oc select{width:auto;min-width:160px}
.oc textarea{resize:vertical}
.oc :focus-visible{outline:2px solid var(--brand);outline-offset:1px}
.oc button{font:inherit;font-weight:600;font-size:14px;border:0;border-radius:8px;padding:8px 16px;cursor:pointer;background:var(--brand);color:#fff;margin:0}
.oc button:hover:not(:disabled){background:var(--brand-d)}
.oc button.sec{background:var(--grey-bg);color:var(--ink)}.oc button.sec:hover:not(:disabled){background:#e5e7eb}
.oc button.ghost{background:transparent;color:var(--brand);padding:4px 8px}.oc button.ghost:hover{background:var(--brand-l)}
.oc button.danger{background:transparent;color:var(--bad);padding:4px 8px}.oc button.danger:hover{background:var(--bad-bg)}
.oc button.wa{background:#15803d}.oc button.wa:hover{background:#166534}
.oc button:disabled{opacity:.5;cursor:not-allowed}
.oc .grow{flex:1}
.oc .tw{overflow:auto;max-height:68vh;border-radius:var(--r)}
.oc table{width:100%;border-collapse:separate;border-spacing:0;min-width:820px}
.oc th{position:sticky;top:0;background:#f9fafb;z-index:2;text-align:left;font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--mute);padding:8px;border-bottom:1px solid var(--line)}
.oc td{padding:8px;border-bottom:1px solid var(--line);vertical-align:middle;font-size:14px}
.oc tbody tr:hover{background:#f9fafb}.oc tr.sel{background:var(--brand-l)!important}
.oc td.n{font-weight:600;max-width:240px}.oc td.n small{display:block;color:var(--mute);font-weight:400}
.oc td.em{min-width:200px;max-width:240px;overflow-wrap:anywhere}.oc td.n{min-width:180px}
.oc .dr{max-width:180px;color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.oc .chip{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap}
.oc .c-not{background:var(--grey-bg);color:#374151}.oc .c-dr{background:var(--info-bg);color:var(--info)}
.oc .c-ap{background:var(--warn-bg);color:var(--warn)}.oc .c-se{background:var(--ok-bg);color:var(--ok)}.oc .c-fa{background:var(--bad-bg);color:var(--bad)}
.oc .acts{min-width:150px}.oc .ab{display:flex;gap:4px;justify-content:flex-end;flex-wrap:wrap;align-items:center}
.oc .muted{color:var(--mute)}
.oc .sticky{position:sticky;bottom:16px;z-index:5;display:none;align-items:center;gap:8px;flex-wrap:wrap;background:#111827;color:#fff;border-radius:var(--r);padding:8px 16px;box-shadow:0 8px 24px rgba(17,24,39,.3);margin-top:16px}
.oc .sticky.show{display:flex}.oc .sticky button.sec{background:#374151;color:#fff}.oc .sticky button.sec:hover:not(:disabled){background:#4b5563}
.oc .empty{text-align:center;padding:48px 16px;color:var(--mute)}.oc .empty b{display:block;font-size:18px;color:var(--ink);margin-bottom:8px}
.oc .prog{height:8px;border-radius:99px;background:var(--grey-bg);overflow:hidden;margin-top:8px}.oc .prog i{display:block;height:100%;background:var(--brand);transition:width .4s}
.oc .side{position:fixed;top:0;right:0;bottom:0;width:440px;max-width:100vw;background:#fff;box-shadow:-8px 0 32px rgba(17,24,39,.18);transform:translateX(105%);transition:transform .25s;z-index:20;display:flex;flex-direction:column}
.oc .side.open{transform:none}
.oc .side header{padding:16px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:flex-start}
.oc .side .sb{padding:16px;overflow:auto;flex:1}.oc .side footer{padding:16px;border-top:1px solid var(--line);display:flex;gap:8px;flex-wrap:wrap}
.oc .meta{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}.oc .count{font-size:12px;color:var(--mute);text-align:right}
.oc details summary{cursor:pointer;font-weight:600}.oc details textarea{min-height:72px}
.oc .toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#111827;color:#fff;padding:8px 16px;border-radius:99px;z-index:30;display:none}
@media (max-width:900px){.oc .cards{grid-template-columns:repeat(2,1fr)}}
@media (max-width:760px){body nav{position:static;width:auto;display:flex;flex-wrap:wrap;padding:8px}body main{margin:8px 0 0 0!important;padding:0 16px!important}
 .oc .cards{grid-template-columns:repeat(2,1fr);gap:8px}.oc .cards .sc:last-child{grid-column:1/-1}.oc .bar{gap:8px}.oc select{min-width:0;width:100%}.oc .bar>div{flex:1 1 100%}.oc .grow{display:none}.oc .sticky{bottom:8px;padding:8px;gap:4px}.oc .sticky button{padding:4px 8px;font-size:13px}.oc .banner{flex-direction:column;align-items:flex-start}
 .oc .side{width:100vw}.oc .sc b{font-size:24px}}
</style>
<div class='oc' id='oc'>
<div class='tabs'><a href='/campaign/__CID__'>Leads</a><a class='on' href='/campaign/__CID__/outreach'>Outreach</a></div>
<h2>Outreach <span class='muted' style='font-weight:400;font-size:16px'>· __NAME__</span></h2>
<div id='banner' class='banner'><span>Email sending is not set up yet - drafts only</span></div>
<div class='cards' id='cards'></div>
<div class='panel'>
 <div class='bar'>
  <div><label>Pitch</label><select id='pitch'><option value='inventory'>InventoryCloud inventory software</option><option value='hms'>HMS/LIMS hospital and lab software</option></select></div>
  <div><label>Language</label><select id='lang'>__LANGS__</select></div>
  <div><label>Channel</label><select id='chan'><option value='email'>Email</option><option value='whatsapp'>WhatsApp</option></select></div>
  <div class='grow'></div>
  <button id='wd' onclick='writeDrafts()'>Write drafts</button>
  <span id='sendwrap' title='Email sending is not set up yet'><button id='sendbtn' class='sec' disabled onclick='sendEmails()'>Send approved emails</button></span>
  <span id='testwrap' title='Email sending is not set up yet'><button id='testbtn' class='sec' disabled onclick='sendTest()'>Send test to myself</button></span>
 </div>
 <div id='jobbox' style='display:none;margin-top:16px'><b id='jobtxt'></b><div class='prog'><i id='jobbar' style='width:0'></i></div></div>
 <p class='muted' id='hint' style='margin:8px 0 0;font-size:13px'>Tick the leads to write for, or leave all unticked to write for every lead that has no draft yet.</p>
 <details style='margin-top:16px'><summary>Settings: signature, pitch texts, opt-out line</summary>
  <div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-top:8px'>
   <div><label>Signature</label><textarea id='s_sig' rows='4'></textarea></div>
   <div><label>InventoryCloud pitch text</label><textarea id='s_pi' rows='4'></textarea></div>
   <div><label>HMS/LIMS pitch text</label><textarea id='s_ph' rows='4'></textarea></div>
   <div><label>Email opt-out line</label><textarea id='s_oo' rows='4'></textarea></div>
   <div><label>WhatsApp opt-out line</label><textarea id='s_wo' rows='4'></textarea></div>
  </div><p><button class='sec' onclick='saveSettings()'>Save settings</button> <span class='muted' style='font-size:13px'>Used for new drafts. Existing drafts keep their text.</span></p>
 </details>
</div>
<div class='panel' style='padding:0'><div class='tw' id='tw'></div></div>
<div class='sticky' id='sticky'><b id='selc'></b><span class='grow'></span>
 <button class='sec' onclick='selAll(true)'>Select all</button><button class='sec' onclick='bulkApprove(1)'>Approve selected</button>
 <button class='sec' onclick='bulkApprove(0)'>Unapprove</button><button onclick='regen()'>Regenerate selected</button><button class='sec' onclick='selAll(false)'>Clear</button></div>
<aside class='side' id='side'>
 <header><div class='grow'><b id='pn' style='font-size:17px'></b><div class='muted' id='pm' style='font-size:13px'></div></div><button class='ghost' onclick='closeP()' aria-label='Close'>Close</button></header>
 <div class='sb'><div class='meta' id='pchips'></div>
  <div id='subw'><label>Subject</label><input type='text' id='psub'></div>
  <label style='margin-top:16px'>Message</label><textarea id='pbody' rows='14'></textarea><div class='count' id='pcount'></div>
  <div class='muted' id='perr' style='color:var(--bad);font-size:13px'></div></div>
 <footer><button onclick='saveP(false)'>Save</button><button class='sec' onclick='saveP(true)'>Approve</button><button class='sec' onclick='regenOne()'>Regenerate</button></footer>
</aside>
<div class='toast' id='toast'></div>
</div>
<script>
var CID=__CID__,D={rows:[],email:{}},SEL={},CUR=null,POLL=null,SEEN=false;
function $(i){return document.getElementById(i);}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function api(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(function(r){return r.json();});}
function toast(t){var e=$('toast');e.textContent=t;e.style.display='block';setTimeout(function(){e.style.display='none';},2600);}
var CH={'not drafted':'c-not','drafted':'c-dr','approved':'c-ap','sent':'c-se','failed':'c-fa'};
function load(){return fetch('/api/outreach/'+CID).then(function(r){return r.json();}).then(function(j){D=j;draw();return j;});}
function draw(){
 var s=D.summary,e=D.email;
 var b=$('banner');
 if(e.ready){b.className='banner ok';b.innerHTML='<span>Email sending is on. Sent today: '+e.today+' of '+e.cap+(e.in_window?'':' (outside 09:00-19:00, will wait)')+'</span>';}
 else{b.className='banner';b.innerHTML='<span>Email sending is not set up yet - drafts only</span>'+(e.missing.length?'<span style="font-weight:400;font-size:13px">Missing: '+e.missing.join(', ')+'</span>':'');}
 $('sendbtn').disabled=!e.ready;$('testbtn').disabled=!e.ready;
 var tip=e.ready?'':'Email sending is not set up yet. Add the SMTP settings to the environment first.';
 $('sendwrap').title=tip;$('testwrap').title=tip;
 var cards=[['Leads with email',s.email],['Leads with phone',s.phone],['Drafted',s.drafted],['Approved',s.approved],['Sent',s.sent]];
 $('cards').innerHTML=cards.map(function(c){return '<div class="sc"><b>'+c[1]+'</b><span>'+c[0]+'</span></div>';}).join('');
 var st=D.settings;if(!SEEN){$('s_sig').value=st.out_signature;$('s_pi').value=st.out_pitch_inventory;$('s_ph').value=st.out_pitch_hms;$('s_oo').value=st.out_optout;$('s_wo').value=st.out_wa_optout;$('pitch').value='__PITCH__';SEEN=true;}
 $('hint').innerHTML='Tick the leads to write for, or leave all unticked to write for every lead that has no draft yet.'+(st.out_signature.indexOf('[Your')>=0?' <b style="color:var(--warn)">Set your real signature in Settings before approving drafts.</b>':'');
 var j=D.job||{};var jb=$('jobbox');
 if(j.running){jb.style.display='block';$('jobtxt').textContent='Writing '+j.total+' drafts... '+j.done+' done';$('jobbar').style.width=(j.total?100*j.done/j.total:0)+'%';$('wd').disabled=true;}
 else{jb.style.display='none';$('wd').disabled=false;}
 if(!D.rows.length){$('tw').innerHTML='<div class="empty"><b>No leads to reach out to yet</b>Leads that are not flagged wrong or do-not-contact will appear here.<br><br><a href="/campaign/'+CID+'">Back to the campaign</a></div>';stickyUpd();return;}
 var h='<table><thead><tr><th style="width:32px"><input type="checkbox" id="all" onchange="selAll(this.checked)" aria-label="Select all"></th><th>Business</th><th>Email</th><th>Phone</th><th>Draft</th><th>Status</th><th></th></tr></thead><tbody>';
 D.rows.forEach(function(r){
  var sel=SEL[r.id];
  h+='<tr class="'+(sel?'sel':'')+'"><td><input type="checkbox" '+(sel?'checked':'')+' onchange="tick('+r.id+',this.checked)" aria-label="Select '+esc(r.name)+'"></td>'+
  '<td class="n">'+esc(r.name)+'<small>'+esc(r.city||'')+'</small></td><td class="em">'+(r.email?esc(r.email):'<span class="muted">-</span>')+'</td><td>'+(r.phone?esc(r.phone):'<span class="muted">-</span>')+'</td>'+
  '<td class="dr" title="'+esc(r.body)+'">'+(r.body?esc((r.channel=='email'&&r.subject?r.subject+' - ':'')+r.body.replace(/\s+/g,' ')):'<span class="muted">Not written</span>')+'</td>'+
  '<td><span class="chip '+CH[r.status]+'" '+(r.error?'title="'+esc(r.error)+'"':'')+'>'+cap(r.status)+'</span></td><td class="acts"><div class="ab">'+
  (r.body&&r.channel=='whatsapp'&&r.status!='sent'?(r.wa?'<button class="wa" onclick="waOpen('+r.id+')">Send on WhatsApp</button> <button class="sec" onclick="waMark('+r.id+')">Mark sent</button> ':'<span class="muted" style="font-size:12px">No mobile number</span> '):'')+
  '<button class="ghost" onclick="openP('+r.id+')">'+(r.body?'Edit':'Write')+'</button><button class="danger" title="Never contact this business again" onclick="dnc('+r.id+')">Do not contact</button></div></td></tr>';
 });
 $('tw').innerHTML=h+'</tbody></table>';stickyUpd();
}
function cap(t){return t.charAt(0).toUpperCase()+t.slice(1);}
function ids(){return Object.keys(SEL).filter(function(k){return SEL[k];}).map(Number);}
function tick(i,v){SEL[i]=v;draw();}
function selAll(v){SEL={};if(v){D.rows.forEach(function(r){SEL[r.id]=true;});}draw();}
function stickyUpd(){var n=ids().length;$('sticky').className='sticky'+(n?' show':'');$('selc').textContent=n+' selected';var a=$('all');if(a){a.checked=n&&n==D.rows.length;}}
function writeDrafts(i){
 var list=i||ids();
 if(!list.length){list=D.rows.filter(function(r){return r.status=='not drafted'||r.status=='failed';}).map(function(r){return r.id;});}
 if(!list.length){toast('Every lead already has a draft. Tick leads to regenerate.');return;}
 start(list);
}
function start(list){
 api('/api/outreach/'+CID+'/generate',{ids:list,pitch:$('pitch').value,language:$('lang').value,channel:$('chan').value}).then(function(j){
  if(!j.ok){toast(j.error||'Could not start');return;}
  SEL={};poll();
 });
}
function poll(){clearInterval(POLL);POLL=setInterval(function(){load().then(function(j){if(!(j.job&&j.job.running)){clearInterval(POLL);toast('Drafts are ready'+(j.job&&j.job.errors?' ('+j.job.errors+' failed)':''));if(CUR){openP(CUR);}}});},1500);load();}
function regen(){var l=ids();if(l.length){start(l);}}
function bulkApprove(v){api('/api/outreach/bulk',{ids:ids(),action:v?'approve':'unapprove'}).then(function(){load();toast(v?'Approved':'Approval removed');});}
function rowOf(i){return D.rows.filter(function(r){return r.id==i;})[0];}
function openP(i){
 var r=rowOf(i);if(!r){return;}CUR=i;
 $('pn').textContent=r.name;$('pm').textContent=[r.city,r.email,r.phone].filter(Boolean).join(' · ');
 $('pchips').innerHTML='<span class="chip '+CH[r.status]+'">'+cap(r.status)+'</span>'+(r.language?'<span class="chip c-not">'+esc(r.language)+'</span>':'')+(r.channel?'<span class="chip c-not">'+(r.channel=='email'?'Email':'WhatsApp')+'</span>':'');
 $('subw').style.display=r.channel=='whatsapp'?'none':'block';
 $('psub').value=r.subject||'';$('pbody').value=r.body||'';$('perr').textContent=r.error||'';
 $('pbody').placeholder='No draft yet. Press Regenerate to write one with the options chosen above.';
 cnt();$('side').className='side open';
}
function closeP(){CUR=null;$('side').className='side';}
function cnt(){var n=$('pbody').value.length;$('pcount').textContent=n+' characters'+(n>1000?' (long)':'');}
$('pbody').oninput=cnt;
function saveP(ap){
 if(!rowOf(CUR).body&&!$('pbody').value.trim()){toast('Write or generate a draft first');return;}
 api('/api/outreach/lead/'+CUR,{subject:$('psub').value,body:$('pbody').value}).then(function(j){
  if(!j.ok){toast(j.error);return;}
  return ap?api('/api/outreach/lead/'+CUR,{approved:true}):null;
 }).then(function(){toast(ap?'Approved':'Saved');load().then(function(){if(CUR){openP(CUR);}});});
}
function regenOne(){start([CUR]);}
function dnc(i){if(!confirm('Never contact this business again? It will be hidden from outreach for good.')){return;}api('/api/outreach/lead/'+i+'/dnc').then(function(){if(CUR==i){closeP();}load();toast('Hidden: do not contact');});}
function waOpen(i){var r=rowOf(i);window.open(r.wa,'_blank','noopener');toast('WhatsApp opened. Send it there, then press Mark sent.');}
function waMark(i){api('/api/outreach/lead/'+i+'/wa-sent').then(function(){load();toast('Marked sent. Lead set to Contacted.');});}
function saveSettings(){api('/api/outreach/settings',{out_signature:$('s_sig').value,out_pitch_inventory:$('s_pi').value,out_pitch_hms:$('s_ph').value,out_optout:$('s_oo').value,out_wa_optout:$('s_wo').value}).then(function(){toast('Settings saved');});}
function sendEmails(){api('/api/outreach/'+CID+'/send').then(function(j){toast(j.ok?'Sending started':j.error);});}
function sendTest(){api('/api/outreach/send-test').then(function(j){toast(j.ok?'Test sent':j.error);});}
document.addEventListener('keydown',function(e){if(e.key=='Escape'){closeP();}});
load().then(function(j){if(j.job&&j.job.running){poll();}});
</script></main></body></html>"""
