"""Image engine: HTML/CSS templates -> JPEG via Playwright (Pillow fallback). No AI-generated images, ever.
Images come only from templates, real uploaded assets, rembg cut-outs (u2netp) and Pillow.
Missing logo -> text wordmark in the accent colour. Missing product photo -> text/shape-only layout.
"""
import os, io, re, json, html, pathlib, threading, ctypes, subprocess, shutil, sys, uuid, time
from . import db as mdb

os.environ.setdefault("U2NET_HOME", r"D:\vault\models\rembg")
SIZES = {"post": (1080, 1350), "story": (1080, 1920), "carousel": (1080, 1350)}
TEMPLATES = {"highlight": "Product highlight", "offer": "Offer / announcement", "tip": "Tip / explainer", "story_card": "Story card"}
MIN_FREE_MB = 800
MAX_BYTES = 8 * 1024 * 1024
LOCK = threading.Lock()
FONT_R, FONT_B = r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"


class RenderError(Exception):
    pass


# ---------- colour / contrast ----------
def _rgb(h):
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (0, 0, 0)


def _lum(h):
    def f(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast_ratio(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def best_text(bg):
    """Near-white or near-black, whichever has the better contrast on bg."""
    return "#f4f4f5" if contrast_ratio("#f4f4f5", bg) >= contrast_ratio("#111111", bg) else "#111111"


def readable(fg, bg, minimum=4.5):
    """fg if it already reads on bg, otherwise the better of white/black."""
    return fg if contrast_ratio(fg, bg) >= minimum else best_text(bg)


def palette(brand):
    bg = brand["bg"] or "#0b0b0c"
    tx = best_text(bg)
    ac = brand["accent"] or "#f5a524"
    ac_text = readable(ac, bg, 3.0)               # accent used for large text on the background
    on_ac = best_text(ac)                          # text placed on an accent-coloured shape
    muted = tx if contrast_ratio("#9a9aa2", bg) < 4.5 else "#9a9aa2"
    return {"bg": bg, "tx": tx, "ac": ac, "ac_text": ac_text, "on_ac": on_ac, "mu": muted}


# ---------- memory guard ----------
def free_ram_mb():
    class MS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                    ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                    ("ex", ctypes.c_ulonglong)]
    m = MS()
    m.l = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return int(m.ap / 1048576)


def ram_message():
    f = free_ram_mb()
    if f < MIN_FREE_MB:
        return "Not enough free memory to render (%d MB free, %d MB needed). Close some programs and try again." % (f, MIN_FREE_MB)
    return ""


# ---------- assets ----------
def cutout(photo):
    """Background-removed copy of a product photo (rembg u2netp), cached in assets/<brand>/cutouts/. Falls back to the photo."""
    photo = pathlib.Path(photo)
    dst = photo.parent.parent / "cutouts" / (photo.stem + ".png")
    if dst.exists() and dst.stat().st_mtime >= photo.stat().st_mtime:
        return dst
    try:
        from PIL import Image
        from rembg import remove, new_session
        im = Image.open(photo).convert("RGB")
        im.thumbnail((1600, 1600))
        out = remove(im, session=new_session("u2netp"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst)
        return dst
    except Exception:
        return photo


def uri(p):
    return pathlib.Path(p).as_uri()


def split_lines(s):
    return [x.strip() for x in re.split(r"[\r\n]+", s or "") if x.strip()]


def first_sentence(s):
    m = re.match(r"(.+?[.!?])(\s|$)", (s or "").strip())
    return m.group(1) if m else (s or "").strip()


# ---------- spec ----------
def build_spec(post_type, template, brand, product, topic, headline="", sub="", points=None, cta="", label="", use_cutout=True):
    """Everything the templates need. Plain data (also stored as spec_json)."""
    post_type = post_type if post_type in SIZES else "post"
    template = template if template in TEMPLATES else ("story_card" if post_type == "story" else "highlight")
    pts = [p for p in (points if points is not None else split_lines(product["key_points"] if product else "")) if p][:5]
    logo = mdb.logo_file(brand)
    photo = mdb.product_photo(brand, product) if product else None
    return {
        "type": post_type, "template": template, "size": SIZES[post_type],
        "brand": brand["name"], "accent": brand["accent"], "bg": brand["bg"],
        "footer_left": brand["footer_left"] or "", "footer_right": brand["footer_right"] or brand["ig_handle"] or "",
        "logo": str(logo) if logo else "", "photo": str(photo) if photo else "", "use_cutout": bool(use_cutout),
        "product": product["name"] if product else "", "headline": (headline or topic or (product["name"] if product else "")).strip(),
        "sub": (sub or first_sentence(product["description"] if product else "")).strip(), "points": pts,
        "cta": cta.strip(), "label": label or TEMPLATES[template], "price_note": (product["price_note"] if product else "") or "",
    }


# ---------- HTML ----------
CSS = """
@font-face{font-family:'Seg';src:url('__FR__');font-weight:400}@font-face{font-family:'Seg';src:url('__FB__');font-weight:700}
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:__W__px;height:__H__px;overflow:hidden;background:__BG__;color:__TX__;font-family:'Seg','Segoe UI',sans-serif}
.cv{position:relative;width:__W__px;height:__H__px;padding:80px;display:flex;flex-direction:column;gap:36px;overflow:hidden}
.top{display:flex;justify-content:space-between;align-items:center;height:72px;flex:none;z-index:2}
.mark{font-weight:700;font-size:44px;letter-spacing:-.01em;color:__ACT__}.logo{height:72px;max-width:420px;object-fit:contain;object-position:left}
.tag{font-size:26px;letter-spacing:.1em;text-transform:uppercase;color:__MU__}
.body{flex:1;min-height:0;display:flex;flex-direction:column;gap:32px;z-index:2}
.fit{overflow:hidden;flex:none}
h1{font-size:104px;line-height:1.05;font-weight:700;letter-spacing:-.025em}
.sub{font-size:42px;line-height:1.35;color:__MU__}
.pill{align-self:flex-start;background:__AC__;color:__ONAC__;font-weight:700;font-size:28px;letter-spacing:.08em;text-transform:uppercase;padding:12px 28px;border-radius:99px}
.pts{display:flex;flex-direction:column;gap:20px}
.pt{display:flex;gap:24px;align-items:center;font-size:44px;line-height:1.2;border:2px solid __LN__;border-radius:28px;padding:28px 32px;background:__SF__}
.pt i{width:20px;height:20px;border-radius:50%;background:__AC__;flex:none}
.pt b{width:64px;height:64px;border-radius:50%;background:__AC__;color:__ONAC__;display:flex;align-items:center;justify-content:center;flex:none;font-size:36px}
.cta{align-self:flex-start;background:__AC__;color:__ONAC__;font-weight:700;font-size:40px;padding:26px 52px;border-radius:99px;flex:none}
.foot{display:flex;justify-content:space-between;border-top:2px solid __LN__;padding-top:28px;font-size:28px;color:__MU__;flex:none;z-index:2}
.deco{position:absolute;right:-220px;bottom:-220px;width:760px;height:760px;border-radius:50%;border:2px solid __AC__;opacity:.35;z-index:1}
.deco.b{right:-120px;bottom:-120px;width:560px;height:560px;opacity:.55}.deco.c{right:-20px;bottom:-20px;width:360px;height:360px;background:__AC__;opacity:.16;border:0}
.bar{position:absolute;left:0;top:0;bottom:0;width:18px;background:__AC__;z-index:1}
.photo{flex:1;min-height:0;display:flex;align-items:center;justify-content:center}.photo img{max-width:100%;max-height:100%;object-fit:contain}
.big{display:flex;flex-direction:column;justify-content:center;flex:1;gap:36px}
.center h1{font-size:120px}
"""


def page_html(spec, inner, w, h):
    pal = palette({"bg": spec["bg"], "accent": spec["accent"]})
    card = "#1a1a1d" if _lum(pal["bg"]) < 0.4 else "#ffffff"
    ln = "#2a2a2f" if _lum(pal["bg"]) < 0.4 else "#d4d4d8"
    css = CSS
    for k, v in {"__FR__": uri(FONT_R), "__FB__": uri(FONT_B), "__W__": str(w), "__H__": str(h), "__BG__": pal["bg"], "__TX__": pal["tx"],
                 "__AC__": pal["ac"], "__ACT__": pal["ac_text"], "__ONAC__": pal["on_ac"], "__MU__": pal["mu"], "__SF__": card, "__LN__": ln}.items():
        css = css.replace(k, v)
    js = ("<script>document.fonts.ready.then(function(){document.querySelectorAll('[data-fit]').forEach(function(e){var s=parseFloat(getComputedStyle(e).fontSize);"
          "var mh=parseFloat(getComputedStyle(e).maxHeight)||e.clientHeight;var n=0;while(e.scrollHeight>mh+1&&n<60){s*=0.95;e.style.fontSize=s+'px';n++;}});document.body.setAttribute('data-ready','1');});</script>")
    return "<!doctype html><html><head><meta charset='utf-8'><style>%s</style></head><body>%s%s</body></html>" % (css, inner, js)


def e(s):
    return html.escape(str(s or ""))


def brandmark(spec):
    if spec["logo"] and pathlib.Path(spec["logo"]).exists():
        return "<img class='logo' src='%s'>" % uri(spec["logo"])
    return "<div class='mark'>%s</div>" % e(spec["brand"])


def frame(spec, body, tag="", deco=True, bar=False):
    foot = ""
    if spec["footer_left"] or spec["footer_right"]:
        foot = "<div class='foot'><span>%s</span><span>%s</span></div>" % (e(spec["footer_left"]), e(spec["footer_right"]))
    d = "<div class='deco'></div><div class='deco b'></div><div class='deco c'></div>" if deco else ""
    return "<div class='cv'>%s<div class='top'>%s<span class='tag'>%s</span></div><div class='body'>%s</div>%s</div>" % (
        d + ("<div class='bar'></div>" if bar else ""), brandmark(spec), e(tag), body, foot)


def photo_html(spec):
    p = spec.get("photo")
    if not p or not pathlib.Path(p).exists():
        return ""
    src = cutout(p) if spec.get("use_cutout") else p
    return "<div class='photo'><img src='%s'></div>" % uri(src)


def cta_html(spec):
    return "<div class='cta'>%s</div>" % e(spec["cta"]) if spec["cta"] else ""


def pts_html(spec, numbered=False, n=None):
    items = spec["points"][:n] if n else spec["points"]
    if not items:
        return ""
    return "<div class='pts'>%s</div>" % "".join(
        "<div class='pt'>%s<span>%s</span></div>" % (("<b>%d</b>" % (i + 1)) if numbered else "<i></i>", e(t)) for i, t in enumerate(items))


def t_highlight(spec):
    ph = photo_html(spec)
    body = ("<div class='pill'>%s</div><h1 class='fit' data-fit style='max-height:%dpx'>%s</h1><div class='sub fit' data-fit style='max-height:200px'>%s</div>%s%s%s") % (
        e(spec["product"] or spec["label"]), 380 if spec["type"] != "story" else 520, e(spec["headline"]), e(spec["sub"]), ph or pts_html(spec, n=3), "", cta_html(spec))
    return frame(spec, body, tag="", deco=not ph)


def t_offer(spec):
    extra = "<div class='sub'>%s</div>" % e(spec["price_note"]) if spec["price_note"] else ""
    body = ("<div class='big'><div class='pill'>%s</div><h1 class='fit' data-fit style='max-height:560px'>%s</h1><div class='sub fit' data-fit style='max-height:260px'>%s</div>%s%s</div>") % (
        e(spec["label"] if spec["label"] != TEMPLATES["offer"] else "Announcement"), e(spec["headline"]), e(spec["sub"]), extra, cta_html(spec))
    return frame(spec, body, tag=spec["product"], bar=True)


def t_tip(spec):
    body = "<div class='pill'>Tip</div><h1 class='fit' data-fit style='max-height:340px'>%s</h1>%s%s" % (e(spec["headline"]), pts_html(spec, numbered=True, n=4), cta_html(spec))
    return frame(spec, body, tag=spec["product"], deco=False, bar=True)


def t_story(spec):
    ph = photo_html(spec)
    body = "<div class='big center'><div class='pill'>%s</div><h1 class='fit' data-fit style='max-height:760px'>%s</h1><div class='sub fit' data-fit style='max-height:300px'>%s</div></div>%s%s" % (
        e(spec["product"] or spec["label"]), e(spec["headline"]), e(spec["sub"]), ph, cta_html(spec))
    return frame(spec, body, tag="")


def t_point(spec):
    body = "<div class='big'><div class='pill'>%s</div><h1 class='fit' data-fit style='max-height:700px'>%s</h1></div>" % (e(spec["label"]), e(spec["headline"]))
    return frame(spec, body, tag=spec["product"], deco=False, bar=True)


TPL = {"highlight": t_highlight, "offer": t_offer, "tip": t_tip, "story_card": t_story, "point": t_point}


def slides_for(spec):
    """Carousel: cover + one slide per key point (max 3) + closing slide."""
    out = [dict(spec, template="story_card", points=[], cta="")]
    pts = spec["points"][:3]
    for i, p in enumerate(pts):
        out.append(dict(spec, template="point", headline=p, sub="", points=[], cta="", label="%d / %d" % (i + 2, len(pts) + 2)))
    out.append(dict(spec, template="offer", headline=spec["cta"] or spec["product"] or spec["headline"], sub=spec["sub"], label="Next step"))
    return out


# ---------- rendering ----------
def _save_jpeg_from_png(png_bytes, path):
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    q = 90
    while True:
        im.save(path, "JPEG", quality=q, optimize=True)
        if os.path.getsize(path) <= MAX_BYTES or q <= 50:
            return
        q -= 10


def _pillow_slide(s, path):
    """Fallback layout: brand wordmark, headline, key points, footer. Used only when Playwright fails."""
    from PIL import Image, ImageDraw, ImageFont
    w, h = s["size"]
    pal = palette({"bg": s["bg"], "accent": s["accent"]})
    im = Image.new("RGB", (w, h), pal["bg"])
    d = ImageDraw.Draw(im)
    fb = lambda sz: ImageFont.truetype(FONT_B, sz) if os.path.exists(FONT_B) else ImageFont.load_default()
    fr = lambda sz: ImageFont.truetype(FONT_R, sz) if os.path.exists(FONT_R) else ImageFont.load_default()

    def wrap(text, font, maxw):
        lines, cur = [], ""
        for word in text.split():
            t = (cur + " " + word).strip()
            if d.textlength(t, font=font) <= maxw:
                cur = t
            else:
                lines.append(cur)
                cur = word
        return lines + ([cur] if cur else [])
    d.rectangle([0, 0, 18, h], fill=pal["ac"])
    d.text((80, 80), s["brand"], font=fb(44), fill=pal["ac_text"])
    y = 240
    size = 96
    while size > 40:
        lines = wrap(s["headline"], fb(size), w - 160)
        if len(lines) * size * 1.1 <= h * 0.4:
            break
        size -= 6
    for ln in lines:
        d.text((80, y), ln, font=fb(size), fill=pal["tx"])
        y += int(size * 1.1)
    y += 40
    for ptxt in s["points"][:4]:
        for ln in wrap(ptxt, fr(42), w - 240):
            d.text((120, y), ln, font=fr(42), fill=pal["tx"])
            y += 56
        y += 20
    if s["cta"]:
        d.rounded_rectangle([80, h - 280, 80 + int(d.textlength(s["cta"], font=fb(40))) + 100, h - 190], radius=45, fill=pal["ac"])
        d.text((130, h - 262), s["cta"], font=fb(40), fill=pal["on_ac"])
    d.text((80, h - 100), s["footer_left"], font=fr(28), fill=pal["mu"])
    rt = s["footer_right"]
    d.text((w - 80 - d.textlength(rt, font=fr(28)), h - 100), rt, font=fr(28), fill=pal["mu"])
    im.save(path, "JPEG", quality=90)


def cartoon_available():
    base = pathlib.Path(__file__).resolve().parent
    return (base / "render_story.py").exists() and (base / "sample_story.json").exists()


def render_cartoon(spec, out_dir):
    """Optional cartoon story carousel: only when marketing/render_story.py and sample_story.json exist."""
    base = pathlib.Path(__file__).resolve().parent
    data = json.loads((base / "sample_story.json").read_text(encoding="utf-8"))
    for k in ("title", "topic", "headline"):
        if k in data:
            data[k] = spec["headline"]
    sp = pathlib.Path(out_dir) / "story_spec.json"
    sp.write_text(json.dumps(data), encoding="utf-8")
    py = pathlib.Path(sys.executable).with_name("python.exe")
    r = subprocess.run([str(py if py.exists() else sys.executable), str(base / "render_story.py"), str(sp), str(out_dir)],
                       capture_output=True, text=True, timeout=300, creationflags=0x08000000)
    files = sorted(str(p) for p in pathlib.Path(out_dir).glob("*.jpg"))
    if r.returncode != 0 or not files:
        raise RenderError("Cartoon story renderer failed.")
    return files


def render(spec, out_dir):
    """Render the spec to JPEG file(s) in out_dir. Returns {"files":[...], "warning":""}. Raises RenderError with a plain message."""
    msg = ram_message()
    if msg:
        raise RenderError(msg)
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in list(out_dir.glob("*.jpg")):
        old.unlink()
    items = slides_for(spec) if spec["type"] == "carousel" else [spec]
    w, h = spec["size"]
    files, warning = [], ""
    with LOCK:                                   # one image at a time
        for i, s in enumerate(items):
            name = ("slide_%d.jpg" % (i + 1)) if spec["type"] == "carousel" else "post.jpg"
            path = out_dir / name
            try:
                from playwright.sync_api import sync_playwright
                hp = out_dir / ("_t%d.html" % i)
                hp.write_text(page_html(s, TPL[s["template"]](s), w, h), encoding="utf-8")
                with sync_playwright() as p:
                    br = p.chromium.launch()
                    try:
                        pg = br.new_page(viewport={"width": w, "height": h})
                        pg.goto(uri(hp))
                        pg.wait_for_selector("body[data-ready='1']", timeout=15000)
                        png = pg.screenshot(type="png")
                    finally:
                        br.close()
                hp.unlink()
                _save_jpeg_from_png(png, str(path))
            except Exception as ex:
                try:
                    _pillow_slide(s, str(path))
                    warning = "The template renderer failed (%s), so a simple fallback layout was used." % str(ex).splitlines()[0][:120]
                except Exception as ex2:
                    raise RenderError("Image could not be rendered: %s" % str(ex2)[:120])
            if not path.exists() or path.stat().st_size < 2000:
                raise RenderError("Rendered image was empty.")
            files.append(str(path))
    return {"files": files, "warning": warning}
