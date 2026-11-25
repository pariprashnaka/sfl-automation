"""Marketing hub routes (Flask blueprint). Registered from leads_app.py: marketing.routes.register(module)."""
import html, json, pathlib, re, time, datetime
from flask import Blueprint, request, redirect, render_template_string, jsonify, send_file, abort
from . import db as mdb

bp = Blueprint("marketing", __name__)
M = None
CAP = 40

TABS = [("/marketing", "Dashboard"), ("/marketing/create", "Create"), ("/marketing/queue", "Queue"), ("/marketing/brands", "Brand kit")]

CSS = """<style>
.mk h2{margin:0 0 14px}.mk .tabs{display:flex;gap:8px;margin:0 0 20px;border-bottom:1px solid var(--border)}
.mk .tabs a{padding:8px 16px;color:var(--muted);border-bottom:2px solid transparent;text-decoration:none;font-weight:600}
.mk .tabs a.on{color:var(--text);border-color:var(--accent)}
.mk .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.mk .chip{display:inline-block;font-size:12px;padding:2px 10px;border-radius:99px;border:1px solid var(--border);color:var(--muted)}
.mk .chip.ok{color:var(--ok);border-color:var(--ok)}.mk .chip.bad{color:var(--bad);border-color:var(--bad)}.mk .chip.on{color:var(--accent);border-color:var(--accent)}
.mk label{display:block;font-size:13px;color:var(--muted);margin-top:8px}
.mk .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}.mk .row>*{flex:1;min-width:140px}
.mk .sw{display:inline-block;width:18px;height:18px;border-radius:5px;border:1px solid var(--border);vertical-align:middle}
.mk table td,.mk table th{padding:8px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
.mk .note{color:var(--muted);font-size:13px}.mk details>summary{cursor:pointer;font-weight:600}
.mk img.pv{max-width:100%;border-radius:12px;border:1px solid var(--border)}
.mk .msg{padding:10px 14px;border-radius:10px;background:var(--surface2);border:1px solid var(--border);margin-bottom:14px}
</style>"""


def esc(s):
    return html.escape(str(s if s is not None else ""))


def page(active, body, title="Marketing"):
    tabs = "".join("<a href='%s'%s>%s</a>" % (u, " class='on'" if u == active else "", n) for u, n in TABS)
    return (render_template_string(M.HEAD) + CSS + "<div class='mk'><h2>%s</h2><div class='tabs'>%s</div>%s</div></main></body></html>" % (esc(title), tabs, body))


def db():
    c = M.conn()
    mdb.ensure(c)
    return c


def published_last_24h(c):
    since = (datetime.datetime.now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    return c.execute("SELECT COUNT(*) FROM posts WHERE status='published' AND published_at>=?", (since,)).fetchone()[0]


def health():
    """Booleans only: whether each setting exists. Values are never put on the page."""
    keys = ["CF_ACCOUNT_ID", "CF_API_TOKEN", "GEMINI_API_KEY", "GEMINI_IMAGE_MODEL", "IG_USER_ID", "IG_ACCESS_TOKEN", "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"]
    return [(k, bool(M.env_val(k))) for k in keys]


def flash():
    m = request.args.get("m", "")
    return "<div class='msg'>%s</div>" % esc(m) if m else ""


# ---------------- dashboard ----------------
@bp.route("/marketing")
def dashboard():
    c = db()
    counts = {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM posts GROUP BY status")}
    n24 = published_last_24h(c)
    from . import imagegen
    iu = imagegen.used_today(c)
    last = c.execute("SELECT p.*, b.name AS bname FROM posts p LEFT JOIN brands b ON b.id=p.brand_id ORDER BY p.id DESC LIMIT 5").fetchall()
    c.close()
    tiles = "".join("<div class='tile'><b>%d</b><small>%s</small></div>" % (counts.get(s, 0), s.capitalize()) for s in
                    ["draft", "approved", "scheduled", "publishing", "published", "failed"])
    rows = "".join("<tr><td>#%d</td><td>%s</td><td>%s</td><td>%s</td><td><span class='chip'>%s</span></td></tr>" % (
        p["id"], esc(p["bname"]), esc(p["type"]), esc((p["topic"] or "")[:60]), esc(p["status"])) for p in last) or "<tr><td class='note'>No posts yet. Use Create.</td></tr>"
    hl = "".join("<div>%s <span class='chip %s'>%s</span></div>" % (k, "ok" if v else "bad", "set" if v else "missing") for k, v in health())
    dry = M.qset("mkt_dry_run", "1") == "1"
    body = flash() + (
        "<div class='tiles'>%s</div><div class='grid'>"
        "<div class='card'><h3>Publishing today</h3><b style='font-size:28px'>%d / %d</b><div class='note'>Posts published in the last 24 hours (cap %d).</div></div>"
        "<div class='card'><h3>Images used today</h3><b style='font-size:28px'>%s</b><div class='note'>AI illustrations made today (cap per day: Cloudflare %d, Gemini %d). Cached pictures are free.</div></div>"
        "<div class='card'><h3>Connection</h3>%s<div style='margin-top:12px'><button type='button' onclick='testConn()'>Test connection</button> "
        "<span id='tc' class='note'></span></div>"
        "<form method='post' action='/marketing/dry-run' style='margin-top:12px'><span class='chip %s'>Dry run %s</span> "
        "<button class='grey'>Switch %s</button></form><div class='note'>Dry run does everything except the final publish.</div></div></div>"
        "<div class='card'><h3>Last 5 posts</h3><table>%s</table></div>"
        "<script>function testConn(){var t=document.getElementById('tc');t.textContent='Testing...';"
        "fetch('/api/marketing/test',{method:'POST'}).then(function(r){return r.json()}).then(function(j){t.textContent=j.ok?('Connected as @'+j.username):j.error;})"
        ".catch(function(){t.textContent='Request failed'});}</script>") % (
        tiles, n24, CAP, CAP, esc(", ".join("%s %d" % kv for kv in iu.items()) or "0"), imagegen.cap("cloudflare", M.qset), imagegen.cap("gemini", M.qset), hl, "on" if dry else "bad", "ON" if dry else "OFF", "off" if dry else "on", rows)
    return page("/marketing", body)


@bp.route("/marketing/dry-run", methods=["POST"])
def dry_run_toggle():
    M.qset_save("mkt_dry_run", "0" if M.qset("mkt_dry_run", "1") == "1" else "1")
    return redirect("/marketing")


@bp.route("/api/marketing/test", methods=["POST"])
def api_test():
    try:
        from . import ig_publish
    except Exception:
        return jsonify(ok=False, error="Publisher is not installed yet.")
    return jsonify(ig_publish.test_connection(ig_publish.Config(M.env_val)))


# ---------------- brand kit ----------------
def save_upload(f, dest_dir, stem):
    if not f or not f.filename:
        return None
    ext = pathlib.Path(f.filename).suffix.lower()
    if ext not in mdb.IMG_EXT:
        raise ValueError("Use a PNG, JPG or WEBP image.")
    data = f.read()
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("Image is over 10 MB.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob(stem + ".*"):
        if old.suffix.lower() in mdb.IMG_EXT:
            old.unlink()
    p = dest_dir / (stem + ext)
    p.write_bytes(data)
    return p


def hexcol(v, default):
    v = (v or "").strip()
    return v if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else default


@bp.route("/marketing/brands")
def brands_page():
    from . import shots
    c = db()
    brands = c.execute("SELECT * FROM brands ORDER BY id").fetchall()
    prods = c.execute("SELECT * FROM products ORDER BY id").fetchall()
    cm = {}
    for m in c.execute("SELECT * FROM cast_members ORDER BY id"):
        cm.setdefault(m["brand_id"], []).append(m)
    c.close()
    out = [flash()]
    for b in brands:
        lf = mdb.logo_file(b)
        logo = ("<img src='/marketing/file/%s/%s' style='height:48px'>" % (mdb.slug(b["name"]), esc(lf.name))) if lf else \
            "<span style='font-weight:700;font-size:22px;color:%s'>%s</span> <span class='note'>(text wordmark until a logo is uploaded)</span>" % (esc(b["accent"]), esc(b["name"]))
        pr = ""
        for p in [x for x in prods if x["brand_id"] == b["id"]]:
            ph = mdb.product_photo(b, p)
            pr += ("<details class='card' style='margin:8px 0'><summary>%s <span class='note'>%s</span></summary>"
                   "<form method='post' action='/marketing/product/%d' enctype='multipart/form-data'>"
                   "<label>Name</label><input name='name' value='%s'><label>Description</label><textarea name='description' rows='2'>%s</textarea>"
                   "<label>Key points (one per line)</label><textarea name='key_points' rows='3'>%s</textarea>"
                   "<label>Price note</label><input name='price_note' value='%s'>"
                   "<label>Product photo (optional, picked up automatically)</label><input type='file' name='photo' accept='image/*'>"
                   "<div style='margin-top:10px'><button>Save product</button> <button class='grey' name='delete' value='1' formnovalidate>Delete</button></div></form>"
                   "<h4 style='margin:14px 0 4px'>Screenshots</h4><div class='note'>Demo data only. Each needs a one-line note; they are used in screenshot slides.</div>%s</details>") % (
                esc(p["name"]), "photo uploaded" if ph else "no photo - text-only templates", p["id"], esc(p["name"]), esc(p["description"]),
                esc(p["key_points"]), esc(p["price_note"]), shots.widget(p["id"]))
        cast = ""
        for m in cm.get(b["id"], []):
            cast += ("<div class='card' style='margin:8px 0'><b>%s</b> <span class='note'>(%s)</span><div class='row'><div><label>Name</label><input name='name_%s' value='%s'></div>"
                     "<div style='flex:0 0 120px;min-width:100px'><label>Seed</label><input name='seed_%s' type='number' value='%d'></div></div>"
                     "<label>Fixed visual description (age, face, moustache/glasses, clothes, colours)</label><textarea name='desc_%s' rows='3'>%s</textarea></div>") % (
                esc(m["name"]), esc(m["role"]), m["role"], esc(m["name"]), m["role"], m["seed"], m["role"], esc(m["description"]))
        cast = ("<details style='margin-top:14px'><summary>Cast for cartoon stories</summary><div class='note'>Original characters only. Every illustration prompt includes this "
                "description and uses the seed, so the same person looks the same in every slide.</div><form method='post' action='/marketing/cast/%d'>%s"
                "<button>Save cast</button></form></details>") % (b["id"], cast)
        tpl = (
            "<div class='card'><h3>%s %s</h3><div>%s</div><form method='post' action='/marketing/brand/%d' enctype='multipart/form-data'>"
            "<div class='row'><div><label>Name</label><input name='name' value='%s'></div><div><label>Instagram handle</label><input name='ig_handle' value='%s'></div></div>"
            "<div class='row'><div><label>Accent colour <span class='sw' style='background:%s'></span></label><input name='accent' value='%s'></div>"
            "<div><label>Background colour <span class='sw' style='background:%s'></span></label><input name='bg' value='%s'></div></div>"
            "<div class='row'><div><label>Footer left</label><input name='footer_left' value='%s'></div><div><label>Footer right</label><input name='footer_right' value='%s'></div></div>"
            "<label>Notes</label><textarea name='notes' rows='2'>%s</textarea><label>Logo (PNG, JPG or WEBP)</label><input type='file' name='logo' accept='image/*'>"
            "<div style='margin-top:10px'><button>Save brand</button></div></form>"
            "%s<h3 style='margin-top:18px'>Products</h3>%s"
            "<form method='post' action='/marketing/product/0' style='margin-top:10px'><input type='hidden' name='brand_id' value='%d'>"
            "<div class='row'><div><input name='name' placeholder='New product name'></div><div style='flex:0'><button>Add product</button></div></div></form></div>")
        out.append(tpl % (
            esc(b["name"]), "<span class='chip'>demo</span>" if "demo" in (b["notes"] or "").lower() else "", logo, b["id"],
            esc(b["name"]), esc(b["ig_handle"]), esc(b["accent"]), esc(b["accent"]), esc(b["bg"]), esc(b["bg"]),
            esc(b["footer_left"]), esc(b["footer_right"]), esc(b["notes"]), cast, pr, b["id"]))
    out.append("<div class='card'><h3>New brand</h3><form method='post' action='/marketing/brand/0'><div class='row'><div><input name='name' placeholder='Brand name'></div>"
               "<div style='flex:0'><button>Add brand</button></div></div></form></div>")
    return page("/marketing/brands", shots.STYLE + "".join(out) + shots.js(), "Brand kit")


@bp.route("/marketing/cast/<int:bid>", methods=["POST"])
def cast_save(bid):
    f = request.form
    c = db()
    try:
        for m in c.execute("SELECT * FROM cast_members WHERE brand_id=?", (bid,)).fetchall():
            r = m["role"]
            try:
                seed = int(f.get("seed_" + r, m["seed"]))
            except ValueError:
                seed = m["seed"]
            c.execute("UPDATE cast_members SET name=?, description=?, seed=? WHERE id=?",
                      ((f.get("name_" + r) or m["name"]).strip()[:40], (f.get("desc_" + r) or m["description"]).strip()[:600], max(0, min(seed, 2147483000)), m["id"]))
        c.commit()
    finally:
        c.close()
    return back("Cast saved")


def back(msg):
    return redirect("/marketing/brands?m=" + re.sub(r"\s", "+", msg))


@bp.route("/marketing/brand/<int:bid>", methods=["POST"])
def brand_save(bid):
    f = request.form
    name = (f.get("name") or "").strip()
    if not name:
        return back("Brand name is required")
    c = db()
    try:
        if bid == 0:
            c.execute("INSERT INTO brands(name) VALUES(?)", (name,))
            c.commit()
            (mdb.ASSETS / mdb.slug(name) / "photos").mkdir(parents=True, exist_ok=True)
            return back("Brand added")
        old = c.execute("SELECT * FROM brands WHERE id=?", (bid,)).fetchone()
        if not old:
            return back("Brand not found")
        c.execute("UPDATE brands SET name=?,ig_handle=?,accent=?,bg=?,footer_left=?,footer_right=?,notes=? WHERE id=?",
                  (name, f.get("ig_handle", "").strip(), hexcol(f.get("accent"), old["accent"]), hexcol(f.get("bg"), old["bg"]),
                   f.get("footer_left", ""), f.get("footer_right", ""), f.get("notes", ""), bid))
        msg = "Brand saved"
        try:
            lp = save_upload(request.files.get("logo"), mdb.ASSETS / mdb.slug(name), "logo")
            if lp:
                c.execute("UPDATE brands SET logo_path=? WHERE id=?", (str(lp.relative_to(mdb.ROOT)), bid))
                msg = "Brand saved, logo uploaded"
        except ValueError as e:
            msg = str(e)
        c.commit()
        return back(msg)
    finally:
        c.close()


@bp.route("/marketing/product/<int:pid>", methods=["POST"])
def product_save(pid):
    f = request.form
    c = db()
    try:
        if pid == 0:
            name = (f.get("name") or "").strip()
            if name and f.get("brand_id"):
                c.execute("INSERT INTO products(brand_id,name) VALUES(?,?)", (int(f["brand_id"]), name))
                c.commit()
            return back("Product added")
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return back("Product not found")
        if f.get("delete"):
            c.execute("DELETE FROM products WHERE id=?", (pid,))
            c.commit()
            return back("Product deleted")
        name = (f.get("name") or p["name"]).strip()
        c.execute("UPDATE products SET name=?,description=?,key_points=?,price_note=? WHERE id=?",
                  (name, f.get("description", ""), f.get("key_points", ""), f.get("price_note", ""), pid))
        c.commit()
        msg = "Product saved"
        b = c.execute("SELECT * FROM brands WHERE id=?", (p["brand_id"],)).fetchone()
        try:
            if save_upload(request.files.get("photo"), mdb.brand_dir(b) / "photos", mdb.slug(name)):
                msg = "Product saved, photo uploaded"
        except ValueError as e:
            msg = str(e)
        return back(msg)
    finally:
        c.close()


@bp.route("/marketing/file/<brand>/<path:name>")
def asset_file(brand, name):
    base = (mdb.ASSETS / mdb.slug(brand)).resolve()
    p = (base / name).resolve()
    if base not in p.parents or not p.is_file() or p.suffix.lower() not in mdb.IMG_EXT:
        abort(404)
    return send_file(str(p))


@bp.route("/marketing/out/<pid>/<name>")
def out_file(pid, name):
    if not re.fullmatch(r"[A-Za-z0-9_]+", pid):
        abort(404)
    base = (mdb.OUT_ROOT / pid).resolve()
    p = (base / name).resolve()
    if base not in p.parents or not p.is_file() or p.suffix.lower() not in (".jpg", ".jpeg"):
        abort(404)
    return send_file(str(p), max_age=0)


def register(mod):
    global M
    M = mod
    c = mod.conn()
    mdb.ensure(c)
    c.close()
    from . import create, queue, story_routes, shots        # noqa: F401  (adds their routes to the blueprint)
    mod.app.register_blueprint(bp)
