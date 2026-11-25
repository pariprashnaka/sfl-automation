"""Characters page: a character library shared by all brands. Each character has a name, a short visual description and a locked master image.
Masters are made free with flux-1-schnell (3 candidates, you pick 1) or uploaded by hand. Slides then pass the locked master to flux-2-klein-4b as a reference."""
import io, json, pathlib, shutil, time
from flask import request, jsonify, send_file
from PIL import Image
from . import db as mdb, imagegen, shots
from . import routes
from .routes import bp, page, db, esc, flash
from . import story_routes as sr
from .story_routes import start_job
from .create import parse_json

MASTER_PX = 1536


def master_dir():
    return mdb.ASSETS / "characters"


def master_file(row):
    f = (mdb.ASSETS / row["master"]) if row["master"] else None
    return f if f and f.is_file() else None


def clean_name(s):
    return " ".join(str(s or "").split())[:40]


def clean_desc(s):
    return " ".join(str(s or "").split())[:600]


def set_master(c, cid, im):
    """Store an image as the locked master (JPEG, max 1536 px on the long side)."""
    if max(im.size) > MASTER_PX:
        im = im.copy()
        im.thumbnail((MASTER_PX, MASTER_PX), Image.LANCZOS)
    master_dir().mkdir(parents=True, exist_ok=True)
    f = master_dir() / ("%d.jpg" % cid)
    old = master_dir() / ("%d.png" % cid)
    im.convert("RGB").save(f, "JPEG", quality=92, optimize=True)
    if old.is_file():
        old.unlink()
    c.execute("UPDATE characters SET master=? WHERE id=?", ("characters/%d.jpg" % cid, cid))
    c.commit()
    return f


def proposal_prompt(brief):
    return (
        "From this story brief, propose up to 3 ORIGINAL characters for a cartoon carousel. Any age, gender and business that fits the story. "
        "Never a famous person, brand mascot or known cartoon character.\nSTORY BRIEF:\n<<<\n%s\n>>>\n"
        "For each give a short name and a fixed visual description in English (age, face shape, moustache or glasses, hair, clothes, main colours), one sentence, no text on clothes.\n"
        "Return ONLY JSON: {\"characters\":[{\"name\":\"\",\"description\":\"\"}]}" % brief.strip()[:3000])


def clean_proposals(d):
    out = []
    for x in ((d or {}).get("characters") or [])[:3]:
        if isinstance(x, dict) and clean_name(x.get("name")) and clean_desc(x.get("description")):
            out.append({"name": clean_name(x["name"]), "description": clean_desc(x["description"])})
    return out


@bp.route("/api/marketing/characters/propose", methods=["POST"])
def api_char_propose():
    j = request.get_json(force=True, silent=True) or {}
    if not (j.get("brief") or "").strip():
        return jsonify(ok=False, error="Write or paste the story brief first.")

    def work(st):
        st("proposing characters")
        raw = sr.ai("marketing_story", proposal_prompt(j["brief"]), big=True)
        try:
            props = clean_proposals(parse_json(raw))
        except Exception:
            props = []
        if not props:
            raise ValueError("The AI answer could not be read. Press Propose again.")
        return {"characters": props}
    return jsonify(ok=True, job=start_job(work))


@bp.route("/api/marketing/characters", methods=["POST"])
def api_char_create():
    j = request.get_json(force=True, silent=True) or {}
    name, desc = clean_name(j.get("name")), clean_desc(j.get("description"))
    if not name or not desc:
        return jsonify(ok=False, error="A name and a short visual description are required.")
    c = db()
    cur = c.execute("INSERT INTO characters(name,description,source,created_at) VALUES(?,?,?,?)", (name, desc, "brief", mdb.now()))
    c.commit()
    c.close()
    return jsonify(ok=True, id=cur.lastrowid)


@bp.route("/api/marketing/characters/<int:cid>", methods=["PUT", "DELETE"])
def api_char_edit(cid):
    c = db()
    try:
        row = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify(ok=False, error="Character not found.")
        if request.method == "DELETE":
            f = master_file(row)
            c.execute("UPDATE cast_members SET char_id=0 WHERE char_id=?", (cid,))
            c.execute("DELETE FROM characters WHERE id=?", (cid,))
            c.commit()
            if f:
                f.unlink()
            return jsonify(ok=True)
        j = request.get_json(force=True, silent=True) or {}
        name, desc = clean_name(j.get("name")), clean_desc(j.get("description"))
        if not name or not desc:
            return jsonify(ok=False, error="A name and a short visual description are required.")
        c.execute("UPDATE characters SET name=?, description=? WHERE id=?", (name, desc, cid))
        c.commit()
        return jsonify(ok=True)
    finally:
        c.close()


@bp.route("/api/marketing/characters/<int:cid>/master", methods=["POST"])
def api_char_upload(cid):
    f = request.files.get("file")
    if not f:
        return jsonify(ok=False, error="Choose an image file.")
    c = db()
    try:
        if not c.execute("SELECT 1 FROM characters WHERE id=?", (cid,)).fetchone():
            return jsonify(ok=False, error="Character not found.")
        try:
            im = shots.check_image(f.read(shots.MAX_BYTES + 1), f.filename, by_content=True)
            im, _ = shots.downscale(im)
        except shots.ShotError as ex:
            return jsonify(ok=False, error=str(ex))
        set_master(c, cid, im)
        c.execute("UPDATE characters SET source='uploaded' WHERE id=?", (cid,))
        c.commit()
        return jsonify(ok=True)
    finally:
        c.close()


@bp.route("/api/marketing/characters/<int:cid>/candidates", methods=["POST"])
def api_char_candidates(cid):
    """3 candidates with flux-1-schnell (caricature, plain light background, upper body, no text). The user picks one."""
    c = db()
    row = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
    c.close()
    if not row:
        return jsonify(ok=False, error="Character not found.")
    n = int((request.get_json(force=True, silent=True) or {}).get("round") or 0)
    M = routes.M

    def work(st):
        out, why = [], ""
        for k in range(3):
            st("drawing candidate %d of 3" % (k + 1))
            r = imagegen.generate(imagegen.candidate_prompt(row["description"], k), 500 + 10 * n + k, "cloudflare", M.env_val, M.conn, M.qset)
            if r["file"]:
                out.append({"name": r["file"].name, "url": "/marketing/img/" + r["file"].name})
            else:
                why = r.get("reason", "")
        if not out:
            raise ValueError("No candidate could be made: %s. You can upload a master image instead." % (why or "unknown error"))
        return {"candidates": out, "note": why}
    return jsonify(ok=True, job=start_job(work))


@bp.route("/api/marketing/characters/<int:cid>/pick", methods=["POST"])
def api_char_pick(cid):
    name = imagegen.safe_name((request.get_json(force=True, silent=True) or {}).get("name"))
    src = imagegen.CACHE_DIR / name if name else None
    if not src or not src.is_file():
        return jsonify(ok=False, error="That candidate is no longer available. Make new candidates.")
    c = db()
    try:
        if not c.execute("SELECT 1 FROM characters WHERE id=?", (cid,)).fetchone():
            return jsonify(ok=False, error="Character not found.")
        set_master(c, cid, Image.open(src))
        c.execute("UPDATE characters SET source='generated' WHERE id=?", (cid,))
        c.commit()
        return jsonify(ok=True)
    finally:
        c.close()


@bp.route("/marketing/char/<int:cid>")
def char_image(cid):
    c = db()
    row = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
    c.close()
    f = master_file(row) if row else None
    if not f:
        return "", 404
    return send_file(str(f), max_age=0)


STYLE = """<style>
.mk .ch{display:flex;gap:16px;flex-wrap:wrap}.mk .ch .pic{width:150px;height:150px;border:1px dashed var(--border);border-radius:12px;display:flex;align-items:center;justify-content:center;color:var(--muted);overflow:hidden;flex:none}
.mk .ch .pic img{width:100%;height:100%;object-fit:cover}.mk .ch .f{flex:1;min-width:260px}.mk .cands{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0}
.mk .cands figure{margin:0;width:150px}.mk .cands img{width:150px;height:150px;object-fit:cover;border-radius:10px;border:1px solid var(--border)}
</style>"""

JS = r"""
<script>
function $(i){return document.getElementById(i)}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function J(u,m,b){return fetch(u,{method:m||'POST',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined}).then(function(r){return r.json()});}
function poll(id,say,done){var t=setInterval(function(){fetch('/api/marketing/story/job/'+id).then(function(r){return r.json()}).then(function(j){
 if(!j.ok){clearInterval(t);say(j.error,true);return;}if(j.state=='running'){say(j.stage);return;}clearInterval(t);if(j.state=='error')say(j.error,true);else done(j.result);}).catch(function(){});},1500);}
function say(id){return function(t,bad){var e=$(id);e.textContent=t;e.style.color=bad?'var(--bad)':'var(--muted)';};}
function saveChar(id){J('/api/marketing/characters/'+id,'PUT',{name:$('n'+id).value,description:$('d'+id).value}).then(function(j){say('m'+id)(j.ok?'Saved.':j.error,!j.ok);});}
function delChar(id){if(!confirm('Delete this character and its master image?'))return;J('/api/marketing/characters/'+id,'DELETE').then(function(){location.reload();});}
function upMaster(id){var f=$('f'+id).files[0];if(!f){say('m'+id)('Choose an image first.',true);return;}var fd=new FormData();fd.append('file',f);
 fetch('/api/marketing/characters/'+id+'/master',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(j){if(j.ok)location.reload();else say('m'+id)(j.error,true);});}
var ROUND={};
function cands(id){ROUND[id]=(ROUND[id]||0)+1;var s=say('m'+id);s('starting');J('/api/marketing/characters/'+id+'/candidates','POST',{round:ROUND[id]}).then(function(j){if(!j.ok){s(j.error,true);return;}
 poll(j.job,s,function(r){s('Pick the one you like'+(r.note?' ('+r.note+')':'')+'.');$('c'+id).innerHTML=r.candidates.map(function(c){return "<figure><img src='"+c.url+"'><button type='button' onclick=\"pick("+id+",'"+c.name+"')\">Use this one</button></figure>";}).join('');});});}
function pick(id,name){J('/api/marketing/characters/'+id+'/pick','POST',{name:name}).then(function(j){if(j.ok)location.reload();else say('m'+id)(j.error,true);});}
function propose(){var s=say('pm');s('thinking');J('/api/marketing/characters/propose','POST',{brief:$('pbrief').value}).then(function(j){if(!j.ok){s(j.error,true);return;}
 poll(j.job,s,function(r){s('Add the ones you want to the library.');$('pout').innerHTML=r.characters.map(function(c,i){return "<div class='card' style='margin:6px 0'><b>"+esc(c.name)+"</b><div class='note'>"+esc(c.description)+"</div><button type='button' onclick='addChar("+i+")'>Add to library</button></div>";}).join('');window.PROPS=r.characters;});});}
function addChar(i){var c=window.PROPS[i];J('/api/marketing/characters','POST',c).then(function(j){if(j.ok)location.reload();else say('pm')(j.error,true);});}
function newChar(){J('/api/marketing/characters','POST',{name:$('nn').value,description:$('nd').value}).then(function(j){if(j.ok)location.reload();else say('nm')(j.error,true);});}
</script>"""


@bp.route("/marketing/characters")
def characters_page():
    c = db()
    rows = c.execute("SELECT * FROM characters ORDER BY id").fetchall()
    c.close()
    out = [flash(), "<div class='card'><h3>Character library</h3><div class='note'>Shared by all brands. Each character needs a locked master image: slides pass it to the image model so the "
           "same person appears in every slide. Making masters is free (Cloudflare). Original characters only, never real people, brands or known cartoon characters.</div></div>"]
    out.append("<div class='card'><h3>Propose characters from a story</h3><textarea id='pbrief' rows='4' placeholder='Paste the story brief'></textarea>"
               "<div style='margin-top:8px'><button type='button' onclick='propose()'>Propose characters</button> <span id='pm' class='note'></span></div><div id='pout'></div></div>")
    for r in rows:
        has = bool(master_file(r))
        out.append(
            "<div class='card'><div class='ch'><div class='pic'>%s</div><div class='f'><label>Name</label><input id='n%d' value='%s'>"
            "<label>Visual description (age, face, moustache/glasses, hair, clothes, colours)</label><textarea id='d%d' rows='3'>%s</textarea>"
            "<div style='margin-top:8px'><button type='button' onclick='saveChar(%d)'>Save</button> <button type='button' class='grey' onclick='cands(%d)'>%s</button> "
            "<button type='button' class='grey' onclick='delChar(%d)'>Delete</button> <span id='m%d' class='note'></span></div>"
            "<label>Or upload a master image (PNG, JPG, WEBP)</label><div class='row'><div><input type='file' id='f%d' accept='image/*'></div>"
            "<div style='flex:0'><button type='button' class='grey' onclick='upMaster(%d)'>Upload</button></div></div>"
            "<div class='cands' id='c%d'></div></div></div></div>" % (
            ("<img src='/marketing/char/%d?t=%d'>" % (r["id"], int(time.time()))) if has else "no master yet", r["id"], esc(r["name"]), r["id"], esc(r["description"]),
            r["id"], r["id"], "Make 3 new candidates" if has else "Make 3 candidates", r["id"], r["id"], r["id"], r["id"], r["id"]))
    out.append("<div class='card'><h3>New character</h3><div class='row'><div><input id='nn' placeholder='Name'></div><div style='flex:3'><input id='nd' placeholder='Short visual description'></div>"
               "<div style='flex:0'><button type='button' onclick='newChar()'>Add</button></div></div><span id='nm' class='note'></span></div>")
    return page("/marketing/characters", STYLE + "".join(out) + JS, "Characters")
