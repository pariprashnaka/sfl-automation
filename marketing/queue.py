"""Queue page: drafts -> Approve -> Schedule / Publish now. Nothing publishes unapproved.
State changes live in plain functions (approve, schedule, ...) so they can be tested without Flask."""
import json, pathlib, re, time, datetime, threading
from flask import request, redirect
from . import db as mdb
from .routes import bp, page, db, esc, flash
from . import routes

STATUSES = ["draft", "approved", "scheduled", "publishing", "published", "failed"]
TICK_SECS = 600
_last_tick = [0.0]
_tick_lock = threading.Lock()


# ---------- state machine (no Flask) ----------
def get_post(c, pid):
    return c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()


def approve(c, pid):
    p = get_post(c, pid)
    if not p:
        return False, "Post not found."
    if p["status"] != "draft":
        return False, "Only drafts can be approved."
    if not json.loads(p["image_paths"] or "[]"):
        return False, "This post has no image yet."
    c.execute("UPDATE posts SET status='approved', approved_at=?, error='' WHERE id=?", (mdb.now(), pid))
    c.commit()
    return True, "Approved."


def can_publish(p):
    """The approval gate. A post is publishable only if it was approved (approved_at set) and is in a waiting/failed state."""
    return bool(p and (p["approved_at"] or "").strip() and p["status"] in ("approved", "scheduled", "failed"))


def parse_when(s):
    s = (s or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return ""


def schedule(c, pid, when):
    p = get_post(c, pid)
    w = parse_when(when)
    if not p:
        return False, "Post not found."
    if not w:
        return False, "Choose a valid date and time."
    if not can_publish(p) or p["status"] == "failed":
        return False, "Approve the post before scheduling it."
    c.execute("UPDATE posts SET status='scheduled', scheduled_at=?, error='' WHERE id=?", (w, pid))
    c.commit()
    return True, "Scheduled for " + w[:16] + "."


def unschedule(c, pid):
    c.execute("UPDATE posts SET status='approved', scheduled_at='' WHERE id=? AND status='scheduled'", (pid,))
    c.commit()
    return True, "Schedule removed."


def edit_text(c, pid, caption, hashtags, alt):
    """Editing text sends an approved/scheduled post back to draft: it has to be approved again."""
    p = get_post(c, pid)
    if not p or p["status"] in ("publishing", "published"):
        return False, "This post can no longer be edited."
    c.execute("UPDATE posts SET caption=?, hashtags=?, description_alt=?, status='draft', approved_at='', scheduled_at='', error='' WHERE id=?",
              (caption, hashtags, alt, pid))
    c.commit()
    return True, "Saved. Approve it again before publishing."


def delete(c, pid):
    p = get_post(c, pid)
    if not p or p["status"] == "publishing":
        return False, "This post cannot be deleted right now."
    c.execute("DELETE FROM posts WHERE id=?", (pid,))
    c.commit()
    return True, "Deleted."


def due_posts(c, now_str=None):
    now_str = now_str or mdb.now()
    return c.execute("SELECT id FROM posts WHERE status='scheduled' AND approved_at<>'' AND scheduled_at<>'' AND scheduled_at<=? ORDER BY scheduled_at", (now_str,)).fetchall()


def publish_post(pid):
    """Hand a post to the publisher (stage 5). Returns (ok, message)."""
    try:
        from . import ig_publish
    except Exception:
        return False, "The publisher is not installed yet."
    return ig_publish.publish_post(routes.M, pid)


def scheduler_tick(force=False):
    """Called from the existing queue worker thread; runs at most once every 10 minutes."""
    with _tick_lock:
        if not force and time.time() - _last_tick[0] < TICK_SECS:
            return 0
        _last_tick[0] = time.time()
    c = db()
    ids = [r["id"] for r in due_posts(c)]
    c.close()
    for pid in ids:
        publish_post(pid)
    return len(ids)


# ---------- page ----------
def back(msg, status=""):
    return redirect("/marketing/queue?m=%s%s" % (re.sub(r"\s", "+", msg), ("&status=" + status) if status else ""))


@bp.route("/marketing/queue")
def queue_page():
    flt = request.args.get("status", "")
    c = db()
    counts = {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM posts GROUP BY status")}
    q = "SELECT p.*, b.name AS bname, pr.name AS pname FROM posts p LEFT JOIN brands b ON b.id=p.brand_id LEFT JOIN products pr ON pr.id=p.product_id"
    rows = c.execute(q + (" WHERE p.status=?" if flt in STATUSES else "") + " ORDER BY p.id DESC LIMIT 100", (flt,) if flt in STATUSES else ()).fetchall()
    c.close()
    chips = "<a class='chip%s' href='/marketing/queue'>All %d</a> " % (" on" if not flt else "", sum(counts.values())) + " ".join(
        "<a class='chip%s' href='/marketing/queue?status=%s'>%s %d</a>" % (" on" if flt == s else "", s, s.capitalize(), counts.get(s, 0)) for s in STATUSES)
    cards = []
    for p in rows:
        files = json.loads(p["image_paths"] or "[]")
        thumbs = "".join("<img class='pv' style='height:150px;width:auto;margin:0 8px 8px 0' src='/marketing/out/%d/%s?t=%s'>" % (
            p["id"], esc(pathlib.Path(f).name), int(time.time())) for f in files)
        st = p["status"]
        acts = ""
        if st == "draft":
            acts += "<form method='post' action='/marketing/post/%d/approve' style='display:inline'><input type='hidden' name='status' value='%s'><button>Approve</button></form> " % (p["id"], esc(flt))
        if st in ("approved", "scheduled"):
            acts += ("<form method='post' action='/marketing/post/%d/schedule' style='display:inline-flex;gap:6px;align-items:center'>"
                     "<input type='datetime-local' name='when' value='%s' style='width:auto;margin:0'><button class='grey'>Schedule</button></form> "
                     "<form method='post' action='/marketing/post/%d/publish' style='display:inline'><button>Publish now</button></form> ") % (
                p["id"], esc((p["scheduled_at"] or "")[:16].replace(" ", "T")), p["id"])
        if st == "scheduled":
            acts += "<form method='post' action='/marketing/post/%d/unschedule' style='display:inline'><button class='grey'>Unschedule</button></form> " % p["id"]
        if st == "failed":
            acts += "<form method='post' action='/marketing/post/%d/publish' style='display:inline'><button>Retry</button></form> " % p["id"]
        if st != "publishing":
            acts += "<form method='post' action='/marketing/post/%d/delete' style='display:inline' onsubmit=\"return confirm('Delete this post?')\"><button class='grey'>Delete</button></form>" % p["id"]
        edit = ""
        if st not in ("publishing", "published"):
            edit = ("<details style='margin-top:10px'><summary>Edit text</summary><form method='post' action='/marketing/post/%d/edit'>"
                    "<label>Caption</label><textarea name='caption' rows='4'>%s</textarea><label>Hashtags</label><textarea name='hashtags' rows='2'>%s</textarea>"
                    "<label>Alt-text description</label><textarea name='alt' rows='2'>%s</textarea>"
                    "<div class='note'>Saving sends the post back to draft; approve it again.</div><button style='margin-top:8px'>Save text</button></form></details>") % (
                p["id"], esc(p["caption"]), esc(p["hashtags"]), esc(p["description_alt"]))
        err = "<div style='color:var(--bad);margin-top:8px'>%s</div>" % esc(p["error"]) if p["error"] else ""
        link = " <a href='%s' target='_blank'>view on Instagram</a>" % esc(p["ig_permalink"]) if p["ig_permalink"] else ""
        meta = "%s / %s / %s / %s" % (esc(p["bname"]), esc(p["pname"]), esc(p["type"]), esc(p["platforms"]))
        sched = " <span class='note'>scheduled %s</span>" % esc(p["scheduled_at"][:16]) if p["scheduled_at"] else ""
        cards.append("<div class='card'><div class='row' style='justify-content:space-between'><div><b>#%d</b> %s <span class='chip %s'>%s</span>%s%s</div></div>"
                     "<div class='note' style='margin:6px 0'>%s</div><div>%s</div><div style='margin:8px 0;white-space:pre-wrap'>%s</div>"
                     "<div class='note'>%s</div>%s<div style='margin-top:10px'>%s</div>%s</div>" % (
                         p["id"], esc(p["topic"]), "ok" if st == "published" else ("bad" if st == "failed" else "on"), st, sched, link, meta, thumbs,
                         esc(p["caption"]), esc(p["hashtags"]), err, acts, edit))
    body = flash() + "<div style='margin-bottom:14px'>%s</div>" % chips + ("".join(cards) or "<div class='card note'>Nothing here yet. Use Create to make a draft.</div>")
    return page("/marketing/queue", body, "Queue")


def _act(pid, fn, *a):
    c = db()
    try:
        ok, msg = fn(c, pid, *a)
    finally:
        c.close()
    return back(msg, request.form.get("status", ""))


@bp.route("/marketing/post/<int:pid>/approve", methods=["POST"])
def post_approve(pid):
    return _act(pid, approve)


@bp.route("/marketing/post/<int:pid>/schedule", methods=["POST"])
def post_schedule(pid):
    return _act(pid, schedule, request.form.get("when", ""))


@bp.route("/marketing/post/<int:pid>/unschedule", methods=["POST"])
def post_unschedule(pid):
    return _act(pid, unschedule)


@bp.route("/marketing/post/<int:pid>/edit", methods=["POST"])
def post_edit(pid):
    f = request.form
    return _act(pid, edit_text, f.get("caption", ""), f.get("hashtags", ""), f.get("alt", ""))


@bp.route("/marketing/post/<int:pid>/delete", methods=["POST"])
def post_delete(pid):
    return _act(pid, delete)


@bp.route("/marketing/post/<int:pid>/publish", methods=["POST"])
def post_publish(pid):
    c = db()
    p = get_post(c, pid)
    c.close()
    if not can_publish(p):
        return back("Approve the post before publishing it.")
    ok, msg = publish_post(pid)
    return back(msg)
