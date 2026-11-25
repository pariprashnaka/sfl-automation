"""Create page: pick brand/product/platform/type, generate an image preview (and, from stage 3, caption + hashtags)."""
import json, pathlib, re, shutil, time, uuid
from flask import request, jsonify
from . import db as mdb, images
from . import routes
from .routes import bp, page, db, esc, flash

CREATE_JS = r"""
<script>
var BR=__BRANDS__,TOKEN='',LAST=null;
function $(i){return document.getElementById(i)}
function fillProducts(){var b=$('brand').value,s=$('product');s.innerHTML='';BR.products.filter(function(p){return p.brand_id==b}).forEach(function(p){var o=document.createElement('option');o.value=p.id;o.textContent=p.name;s.appendChild(o);});}
function plats(){return Array.prototype.slice.call(document.querySelectorAll('input[name=plat]:checked')).map(function(x){return x.value});}
function say(t,bad){var m=$('gmsg');m.textContent=t;m.style.color=bad?'var(--bad)':'var(--muted)';}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function dropTag(i){var t=$('hashtags').value.split(/\s+/).filter(Boolean);t.splice(i,1);$('hashtags').value=t.join(' ');drawTags();}
function drawTags(){var t=$('hashtags').value.split(/\s+/).filter(Boolean),tiers=(LAST&&LAST.tiers)||{};
 $('tagchips').innerHTML=t.map(function(x,i){var k=tiers[x.toLowerCase()]||'';return "<span class='chip tier-"+k+"' title='"+k+"'>"+esc(x)+" <a href='#' onclick='dropTag("+i+");return false' aria-label='Drop tag'>x</a></span>";}).join(' ');}
function gen(){say('Working... this can take up to a minute on this laptop.');$('gen').disabled=true;
 fetch('/api/marketing/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({brand_id:$('brand').value,product_id:$('product').value,
  platforms:plats(),type:$('type').value,template:$('template').value,topic:$('topic').value,tone:$('tone').value,cta:$('cta').value,variant:Date.now()})})
 .then(function(r){return r.json()}).then(function(j){$('gen').disabled=false;if(!j.ok){say(j.error,true);return;}
  TOKEN=j.token;LAST=j;say(j.warning||'',!!j.warning);$('result').style.display='block';
  $('imgs').innerHTML=j.images.map(function(u){return "<img class='pv' style='max-width:260px;margin:0 10px 10px 0' src='"+u+"?t="+Date.now()+"'>"}).join('');
  $('caption').value=j.caption||'';$('hashtags').value=j.hashtags||'';$('alt').value=j.alt||'';$('notes').textContent=j.notes||'';drawTags();
 }).catch(function(){$('gen').disabled=false;say('Request failed',true);});}
function saveDraft(){fetch('/api/marketing/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:TOKEN,brand_id:$('brand').value,product_id:$('product').value,
  platforms:plats(),type:$('type').value,template:$('template').value,topic:$('topic').value,caption:$('caption').value,hashtags:$('hashtags').value,alt:$('alt').value,spec:LAST&&LAST.spec})})
 .then(function(r){return r.json()}).then(function(j){if(j.ok){location.href='/marketing/queue?m=Saved+as+draft+%23'+j.id;}else{say(j.error,true);}});}
$('brand').onchange=fillProducts;fillProducts();$('hashtags').oninput=drawTags;
</script>"""

STYLE = """<style>.mk .chip.tier-broad{color:#c9c9ce;border-color:#6b6b73}.mk .chip.tier-niche{color:var(--accent);border-color:var(--accent)}
.mk .chip.tier-local{color:var(--ok);border-color:var(--ok)}.mk .chip a{color:inherit;margin-left:4px}.mk textarea{resize:vertical}</style>"""


@bp.route("/marketing/create")
def create_page():
    c = db()
    brands = [dict(r) for r in c.execute("SELECT id,name FROM brands ORDER BY id")]
    prods = [dict(r) for r in c.execute("SELECT id,brand_id,name FROM products ORDER BY id")]
    c.close()
    bo = "".join("<option value='%d'>%s</option>" % (b["id"], esc(b["name"])) for b in brands)
    to = "".join("<option value='%s'>%s</option>" % (k, esc(v)) for k, v in images.TEMPLATES.items())
    body = flash() + (
        "<div class='card'><div class='row'><div><label>Brand</label><select id='brand'>%s</select></div><div><label>Product</label><select id='product'></select></div>"
        "<div><label>Type</label><select id='type'><option value='post'>Post 1080x1350</option><option value='carousel'>Carousel slides 1080x1350</option>"
        "<option value='story'>Story 1080x1920</option></select></div><div><label>Template</label><select id='template'>%s</select></div></div>"
        "<div class='row'><div style='flex:2'><label>Topic</label><input id='topic' placeholder='e.g. Know your stock at a glance'></div>"
        "<div><label>Tone</label><select id='tone'><option>Friendly</option><option>Professional</option><option>Hinglish friendly</option></select></div>"
        "<div><label>Call to action (optional)</label><input id='cta' placeholder='e.g. Message us for a demo'></div></div>"
        "<label>Platforms</label><div class='row' style='flex-wrap:wrap;gap:18px'><label style='margin:0'><input type='checkbox' name='plat' value='instagram' checked>Instagram</label>"
        "<label style='margin:0'><input type='checkbox' name='plat' value='whatsapp'>WhatsApp status</label><label style='margin:0'><input type='checkbox' name='plat' value='facebook'>Facebook</label></div>"
        "<div style='margin-top:14px'><button id='gen' type='button' onclick='gen()'>Generate</button> <span id='gmsg' class='note'></span></div></div>"
        "<div class='card' id='result' style='display:none'><h3>Preview</h3><div id='imgs'></div>"
        "<label>Caption</label><textarea id='caption' rows='5'></textarea>"
        "<label>Hashtags <span class='note'>(suggested, popularity not verified)</span></label><div id='tagchips' style='margin:6px 0'></div><textarea id='hashtags' rows='2'></textarea>"
        "<label>Alt-text description</label><textarea id='alt' rows='2'></textarea><div id='notes' class='note'></div>"
        "<div style='margin-top:12px'><button type='button' onclick='saveDraft()'>Save as draft</button> <button type='button' class='grey' onclick='gen()'>Regenerate</button></div></div>") % (bo, to)
    return page("/marketing/create", STYLE + body + CREATE_JS.replace("__BRANDS__", json.dumps({"products": prods})), "Create")


def cleanup_previews():
    try:
        for d in mdb.OUT_ROOT.glob("p_*"):
            if time.time() - d.stat().st_mtime > 86400:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def make_content(b, p, topic, tone, variant):
    """Hook for caption/hashtag generation (added in stage 3). Returns dict with caption, hashtags, alt, headline, sub, points, notes."""
    return {}


@bp.route("/api/marketing/generate", methods=["POST"])
def api_generate():
    j = request.get_json(force=True, silent=True) or {}
    c = db()
    b = c.execute("SELECT * FROM brands WHERE id=?", (j.get("brand_id"),)).fetchone()
    p = c.execute("SELECT * FROM products WHERE id=?", (j.get("product_id"),)).fetchone()
    c.close()
    if not b:
        return jsonify(ok=False, error="Choose a brand first.")
    topic = (j.get("topic") or "").strip()
    if not topic:
        return jsonify(ok=False, error="Write a topic first.")
    ptype = j.get("type") if j.get("type") in images.SIZES else "post"
    content = make_content(b, p, topic, j.get("tone") or "", j.get("variant"))
    spec = images.build_spec(ptype, j.get("template"), b, p, topic, headline=content.get("headline", ""), sub=content.get("sub", ""),
                             points=content.get("points"), cta=j.get("cta") or "")
    token = uuid.uuid4().hex[:10]
    try:
        res = images.render(spec, mdb.OUT_ROOT / ("p_" + token))
    except images.RenderError as ex:
        return jsonify(ok=False, error=str(ex))
    cleanup_previews()
    out = {"caption": "", "hashtags": "", "alt": "", "notes": "", "tiers": {}}
    out.update({k: v for k, v in content.items() if k in out})
    out.update(ok=True, token=token, spec=spec, warning=res["warning"],
               images=["/marketing/out/p_%s/%s" % (token, pathlib.Path(f).name) for f in res["files"]])
    return jsonify(out)


@bp.route("/api/marketing/save", methods=["POST"])
def api_save():
    j = request.get_json(force=True, silent=True) or {}
    tok = re.sub(r"[^A-Za-z0-9]", "", j.get("token") or "")
    src = mdb.OUT_ROOT / ("p_" + tok)
    if not tok or not src.exists():
        return jsonify(ok=False, error="Generate an image first.")
    c = db()
    cur = c.execute("INSERT INTO posts(brand_id,product_id,platforms,type,topic,caption,hashtags,description_alt,status,created_at,template,spec_json) "
                    "VALUES(?,?,?,?,?,?,?,?,'draft',?,?,?)",
                    (j.get("brand_id"), j.get("product_id"), ",".join(j.get("platforms") or ["instagram"]), j.get("type") or "post", j.get("topic") or "",
                     j.get("caption") or "", j.get("hashtags") or "", j.get("alt") or "", mdb.now(), j.get("template") or "", json.dumps(j.get("spec") or {})))
    pid = cur.lastrowid
    dst = mdb.OUT_ROOT / str(pid)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    files = [str(f) for f in sorted(dst.glob("*.jpg"))]
    c.execute("UPDATE posts SET image_paths=? WHERE id=?", (json.dumps(files), pid))
    c.commit()
    c.close()
    return jsonify(ok=True, id=pid)
