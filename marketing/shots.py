"""Screenshot library: per-product demo screenshots with a required note. Files stay local (marketing/assets/<brand>/screenshots/);
they leave the machine only through the normal publish step. Uploads need the 'no real customer data' confirmation."""
import io, json, pathlib, re, time
from flask import request, jsonify
from PIL import Image, ImageOps
from . import db as mdb
from .routes import bp, db, esc

MAX_W = 2400
MAX_BYTES = 15 * 1024 * 1024
MAX_FILES = 10
WARNING = "Use demo data only. Make sure no real customer names, phone numbers or amounts are visible."
CONFIRM = "I confirm no real customer data"
FORMATS = {"PNG": ".png", "JPEG": ".jpg", "MPO": ".jpg", "WEBP": ".png"}      # MPO = JPEG from many cameras/phones      # webp is stored as lossless png


class ShotError(ValueError):
    pass


def one_line(s, n):
    return " ".join(str(s or "").split())[:n]


def clean_meta(title, note):
    note = one_line(note, 200)
    if not note:
        raise ShotError("Write one line about what this screenshot shows (required).")
    return one_line(title, 80), note


def check_image(data, filename, by_content=False):
    """by_content=True ignores the file name and trusts only what Pillow can open (JPEG/MPO, PNG, WEBP)."""
    ext = pathlib.Path(filename or "").suffix.lower()
    if not by_content and ext not in mdb.IMG_EXT:
        raise ShotError("%s: only PNG, JPG or WEBP files are accepted." % (filename or "file"))
    if not data or len(data) > MAX_BYTES:
        raise ShotError("%s: file is empty or larger than 15 MB." % filename)
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
        im = Image.open(io.BytesIO(data))
    except Exception:
        raise ShotError("%s: this is not a readable image." % filename)
    if im.format not in FORMATS:
        raise ShotError("%s: only PNG, JPG or WEBP files are accepted." % filename)
    return im


def downscale(im):
    """Max 2400 px wide (never upscaled). EXIF rotation applied. Returns (image, format)."""
    fmt = "JPEG" if im.format == "MPO" else im.format
    im = ImageOps.exif_transpose(im)
    if im.width > MAX_W:
        im = im.resize((MAX_W, max(1, round(im.height * MAX_W / im.width))), Image.LANCZOS)
    return im, fmt


def save_image(im, fmt, folder, stem):
    folder.mkdir(parents=True, exist_ok=True)
    ext = FORMATS[fmt]
    p = folder / (stem + ext)
    if ext == ".jpg":
        im.convert("RGB").save(p, "JPEG", quality=92, optimize=True)
    else:
        im.save(p, "PNG", optimize=True)
    return p


def add(c, brand, product_id, items, confirm):
    """items: [(filename, bytes, title, note)]. All or nothing: any invalid file rejects the whole request."""
    if not confirm:
        raise ShotError("Tick \"%s\" before saving." % CONFIRM)
    if not items:
        raise ShotError("Choose at least one screenshot.")
    if len(items) > MAX_FILES:
        raise ShotError("At most %d screenshots at a time." % MAX_FILES)
    prepared = []
    for fn, data, title, note in items:
        im = check_image(data, fn)
        title, note = clean_meta(title, note)
        prepared.append((fn, downscale(im), title, note))
    out = []
    base = mdb.brand_dir(brand) / "screenshots"
    for k, (fn, (im, fmt), title, note) in enumerate(prepared):
        stem = "%s-%s-%d" % (mdb.slug(product_name(c, product_id)), time.strftime("%Y%m%d%H%M%S"), k)
        p = save_image(im, fmt, base, stem)
        rel = p.relative_to(mdb.ASSETS).as_posix()
        cur = c.execute("INSERT INTO screenshots(brand_id,product_id,path,title,note,width,height,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (brand["id"], product_id, rel, title, note, im.width, im.height, mdb.now()))
        out.append(cur.lastrowid)
    c.commit()
    return out


def product_name(c, pid):
    r = c.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
    return r[0] if r else "product"


def file_of(row):
    p = (mdb.ASSETS / row["path"]).resolve()
    return p if mdb.ASSETS.resolve() in p.parents and p.is_file() else None


def public(row):
    parts = row["path"].split("/", 1)
    return {"id": row["id"], "title": row["title"], "note": row["note"], "w": row["width"], "h": row["height"],
            "url": "/marketing/file/%s/%s" % (parts[0], parts[1] if len(parts) > 1 else "")}


def get_many(c, ids):
    """id -> {path, title, note, w, h} for the ids that exist and whose file is present."""
    out = {}
    ids = [int(i) for i in ids if str(i).isdigit()]
    for i in set(ids):
        r = c.execute("SELECT * FROM screenshots WHERE id=?", (i,)).fetchone()
        f = file_of(r) if r else None
        if f:
            out[i] = {"path": f, "title": r["title"], "note": r["note"], "w": r["width"], "h": r["height"]}
    return out


# ---------------- routes ----------------
@bp.route("/api/marketing/shots")
def api_shots():
    pid = request.args.get("product_id", type=int)
    c = db()
    rows = c.execute("SELECT * FROM screenshots WHERE product_id=? ORDER BY id DESC", (pid,)).fetchall()
    c.close()
    return jsonify(ok=True, shots=[public(r) for r in rows if file_of(r)], warning=WARNING, confirm=CONFIRM)


@bp.route("/api/marketing/shots/<int:pid>", methods=["POST"])
def api_shots_add(pid):
    c = db()
    try:
        prod = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        brand = c.execute("SELECT * FROM brands WHERE id=?", (prod["brand_id"],)).fetchone() if prod else None
        if not brand:
            return jsonify(ok=False, error="Choose a product first.")
        files = request.files.getlist("shot")
        items = [(f.filename, f.read(MAX_BYTES + 1), request.form.get("title_%d" % k, ""), request.form.get("note_%d" % k, "")) for k, f in enumerate(files)]
        try:
            ids = add(c, brand, pid, items, request.form.get("confirm") in ("1", "true", "on"))
        except ShotError as ex:
            return jsonify(ok=False, error=str(ex))
        return jsonify(ok=True, ids=ids)
    finally:
        c.close()


@bp.route("/api/marketing/shots/delete/<int:sid>", methods=["POST"])
def api_shots_delete(sid):
    c = db()
    r = c.execute("SELECT * FROM screenshots WHERE id=?", (sid,)).fetchone()
    if r:
        f = file_of(r)
        c.execute("DELETE FROM screenshots WHERE id=?", (sid,))
        c.commit()
        if f:
            f.unlink()
    c.close()
    return jsonify(ok=True)


@bp.route("/api/marketing/shots/<int:sid>", methods=["PUT"])
def api_shots_edit(sid):
    j = request.get_json(force=True, silent=True) or {}
    try:
        title, note = clean_meta(j.get("title"), j.get("note"))
    except ShotError as ex:
        return jsonify(ok=False, error=str(ex))
    c = db()
    c.execute("UPDATE screenshots SET title=?, note=? WHERE id=?", (title, note, sid))
    c.commit()
    c.close()
    return jsonify(ok=True)


# ---------------- widget (Brand kit per product, and inline on the Create page) ----------------
STYLE = """<style>
.mk .shotbox{border:1px dashed var(--border);border-radius:12px;padding:12px;margin:8px 0}
.mk .shotbox .warn{background:rgba(245,165,36,.16);border:1px solid #f5c542;color:#ffd666;border-radius:10px;padding:8px 12px;margin:8px 0;font-size:13px}
.mk .shotbox .lib{display:flex;flex-wrap:wrap;gap:10px;margin:8px 0}
.mk .shotbox .it{width:170px;border:1px solid var(--border);border-radius:10px;padding:6px;background:var(--surface2);font-size:12px}
.mk .shotbox .it img{width:100%;height:100px;object-fit:cover;border-radius:6px;display:block}
.mk .shotbox .pend{display:flex;gap:10px;align-items:center;margin:6px 0}.mk .shotbox .pend img{width:70px;height:50px;object-fit:cover;border-radius:6px}
.mk .shotbox .pend input{flex:1;min-width:120px}
</style>"""

JS = r"""
<script>
var SHOTBOX={active:null};
function sbEsc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function sbInit(el){el._files=[];el.tabIndex=0;el.addEventListener('focusin',function(){SHOTBOX.active=el;});el.addEventListener('click',function(){SHOTBOX.active=el;});
 el.innerHTML="<div class='warn'>__WARN__</div><div class='lib'></div><label>Add screenshots (PNG, JPG, WEBP) - or click here and press Ctrl+V to paste from the clipboard</label>"+
  "<input type='file' multiple accept='image/png,image/jpeg,image/webp'><div class='pending'></div>"+
  "<label style='display:flex;gap:8px;align-items:center;color:var(--text)'><input type='checkbox' class='cf'> __CONF__</label>"+
  "<div style='margin-top:8px'><button type='button' class='sv'>Save screenshots</button> <span class='note msg'></span></div>";
 el.querySelector('input[type=file]').onchange=function(e){Array.prototype.forEach.call(e.target.files,function(f){sbAdd(el,f);});e.target.value='';};
 el.querySelector('.sv').onclick=function(){sbSave(el);};}
document.addEventListener('paste',function(e){var el=SHOTBOX.active;if(!el||!e.clipboardData)return;
 Array.prototype.forEach.call(e.clipboardData.items||[],function(it){if(it.kind=='file'&&/^image\/(png|jpeg|webp)$/.test(it.type)){var f=it.getAsFile();
  sbAdd(el,new File([f],'pasted-'+Date.now()+(it.type=='image/jpeg'?'.jpg':'.png'),{type:it.type}));}});});
function sbAdd(el,f){el._files.push({f:f,title:'',note:''});sbPend(el);}
function sbPend(el){el.querySelector('.pending').innerHTML=el._files.map(function(p,i){return "<div class='pend'><img src='"+URL.createObjectURL(p.f)+"'>"+
 "<input placeholder='Title (optional)' value=\""+sbEsc(p.title)+"\" oninput='this.closest(\".shotbox\")._files["+i+"].title=this.value'>"+
 "<input placeholder='What does this screenshot show? (required, one line)' value=\""+sbEsc(p.note)+"\" oninput='this.closest(\".shotbox\")._files["+i+"].note=this.value'>"+
 "<button type='button' class='grey' onclick='this.closest(\".shotbox\")._files.splice("+i+",1);sbPend(this.closest(\".shotbox\"))'>x</button></div>";}).join('');}
function sbMsg(el,t,bad){var m=el.querySelector('.msg');m.textContent=t;m.style.color=bad?'var(--bad)':'var(--muted)';}
function sbSave(el){var pid=el.dataset.pid;if(!pid){sbMsg(el,'Choose a product first.',true);return;}
 if(!el._files.length){sbMsg(el,'Choose at least one screenshot.',true);return;}
 if(!el.querySelector('.cf').checked){sbMsg(el,'Tick "__CONF__" before saving.',true);return;}
 for(var i=0;i<el._files.length;i++){if(!el._files[i].note.trim()){sbMsg(el,'Every screenshot needs a one-line note.',true);return;}}
 var fd=new FormData();el._files.forEach(function(p,i){fd.append('shot',p.f,p.f.name);fd.append('title_'+i,p.title);fd.append('note_'+i,p.note);});fd.append('confirm','1');
 sbMsg(el,'Saving...');fetch('/api/marketing/shots/'+pid,{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(j){
  if(!j.ok){sbMsg(el,j.error,true);return;}el._files=[];el.querySelector('.cf').checked=false;sbPend(el);sbMsg(el,'Saved '+j.ids.length+' screenshot(s).');sbLoad(el,j.ids);}).catch(function(){sbMsg(el,'Request failed',true);});}
function sbLoad(el,newIds){var pid=el.dataset.pid;if(!pid){el.querySelector('.lib').innerHTML='';return;}
 fetch('/api/marketing/shots?product_id='+pid).then(function(r){return r.json()}).then(function(j){
  el._shots=j.shots;el.querySelector('.lib').innerHTML=j.shots.map(function(s){return "<div class='it'><img src='"+s.url+"'><b>"+sbEsc(s.title||'Screenshot '+s.id)+"</b><div>"+sbEsc(s.note)+"</div>"+
   "<button type='button' class='grey' onclick='sbDel(this,"+s.id+")'>Delete</button></div>";}).join('')||"<span class='note'>No screenshots yet.</span>";
  if(window.onShotsChanged)window.onShotsChanged(j.shots,newIds||[]);});}
function sbDel(b,id){if(!confirm('Delete this screenshot? Slides using it will lose the picture.'))return;fetch('/api/marketing/shots/delete/'+id,{method:'POST'}).then(function(){sbLoad(b.closest('.shotbox'));});}
function sbSetPid(el,pid){el.dataset.pid=pid||'';sbLoad(el);}
document.querySelectorAll('.shotbox').forEach(function(el){sbInit(el);sbLoad(el);});
</script>"""


def widget(pid):
    return "<div class='shotbox' data-pid='%s'></div>" % (pid if pid else "")


def js():
    return JS.replace("__WARN__", WARNING).replace("__CONF__", CONFIRM)
