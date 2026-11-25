"""Slide layout. Every slide is ONE full 1080x1350 picture. The top 35% of the scene is an empty headroom band: speech bubbles are drawn there
(Chromium, Devanagari) with a tail pointing down at the speaking character's head. A caption / call-to-action sits in a solid band near the bottom
and a tiny brand footer under it. After layout, FIT_JS reports every box; check_layout() fails the slide if a bubble touches a face/head, another
bubble, or leaves the headroom band, and the renderer then redraws the picture (up to 2 times) before giving a clear error."""
import math, pathlib
from PIL import Image
from . import images, shotslides

W, H = 1080, 1350
HEADROOM = 472                           # 35% of the height: empty band at the top of the scene
BUB_TOP, BUB_BOTTOM = 30, 450            # bubbles must stay inside this vertical range (heads start below it)
BUB_MAX_W = 470
BUB_FS, BUB_MIN = 44, 26
BUB_TEXT_H = 300                         # max text height inside one bubble
BAND = (0, 1196, 1080, 104)              # caption / call-to-action, solid background
FOOT = (0, 1300, 1080, 50)               # tiny brand footer, solid background
CAP_FS, CAP_MIN = 40, 24
CAP_H = 88
CTA_H, CTA_FS, CTA_MIN = 80, 44, 28
FRAME_B = 6
UP_CAP = 1.6                             # screenshots are never enlarged more than this
AREA = (24, 24, 1032, 1150)              # screenshot slides: area for frames (above the caption band)
DEVBOX_W, DEVBOX_H = 960, 700            # drawn devices are laid out in a box of this size
SLOT_X = {"left": 0.27, "center": 0.5, "right": 0.73}


# ---------------- what goes on a slide ----------------
def strip_cta(text, cta):
    c = (cta or "").strip().casefold()
    return "" if c and c in (text or "").casefold() else text


def parts(slide, cta=""):
    """(bubbles, caption, cta_on). Lines of the same speaker are merged into one bubble (max 2 bubbles, left/right never overlap).
    On the call-to-action slide the CTA appears once, as the button."""
    on = bool(slide.get("is_cta") and cta)
    lines = [] if slide.get("type") == "shot" else [dict(d) for d in slide["dialogue"] if d["text"].strip()]
    cap = slide.get("narration") or ""
    if on:
        lines = [d for d in lines if strip_cta(d["text"], cta) != ""]
        cap = strip_cta(cap, cta)
    bubs = []
    for d in lines:
        if bubs and bubs[-1]["speaker"] == d["speaker"]:
            bubs[-1]["text"] += " " + d["text"]
        else:
            bubs.append(d)
    return bubs[:2], cap, on


def est_height(text, width, fs, lh=1.12):
    """Rough text height at font size fs (Devanagari averages about 0.56 em per character)."""
    cpl = max(1, int(width / (fs * 0.56)))
    return sum(max(1, math.ceil(len(seg) / cpl)) for seg in str(text).split("\n")) * fs * lh


def too_long(slide, cta=""):
    """'' if the slide's text fits at the minimum font size, else the message shown in the script editor."""
    bubs, cap, on = parts(slide, cta)
    for d in bubs:
        if est_height(d["text"], BUB_MAX_W - 52, BUB_MIN) > BUB_TEXT_H:
            return "text too long for this slide (speech bubble)"
    if cap and est_height(cap, W - 80, CAP_MIN) > CAP_H:
        return "text too long for this slide (caption)"
    if on and est_height(cta, W - 160, CTA_MIN) > CTA_H - 8:
        return "text too long for this slide (call to action)"
    return ""


# ---------------- images prepared at their exact display size (crisp) ----------------
def fit_image(src, w, h, out, up=UP_CAP):
    """Resize with LANCZOS to fit inside w x h (contain). Returns (path, width, height). Never enlarged more than `up`."""
    im = Image.open(src)
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    sc = min(w / im.width, h / im.height, up)
    nw, nh = max(1, int(im.width * sc)), max(1, int(im.height * sc))
    if (nw, nh) != im.size:
        im = im.resize((nw, nh), Image.LANCZOS)
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "PNG")
    return str(out), nw, nh


def slots(layout, n, area=AREA):
    """Slot rectangles (x, y, w, h) inside `area` (x, y, w, h)."""
    ax, ay, aw, ah = area
    g = 20
    if layout == "2up" and n == 2:
        w = (aw - g) // 2
        return [(ax, ay, w, ah), (ax + w + g, ay, w, ah)]
    if layout == "stack" and n >= 2:
        h = (ah - g * (n - 1)) // n
        return [(ax, ay + k * (h + g), aw, h) for k in range(n)]
    if layout == "grid" and n >= 2:
        w, h = (aw - g) // 2, (ah - g) // 2
        return [(ax + (k % 2) * (w + g), ay + (k // 2) * (h + g), w, h) for k in range(n)]
    return [(ax, ay, aw, ah)]


DEV = {   # outer box, screen box (inside DEVBOX), css radius for outer
    "tablet": {"outer": (80, 60, 800, 580), "screen": (102, 82, 756, 536), "r": 44},
    "laptop": {"outer": (100, 50, 760, 480), "screen": (118, 68, 724, 444), "r": 28},
    "tv": {"outer": (50, 40, 860, 500), "screen": (64, 54, 832, 472), "r": 18},
    "phone": {"outer": (310, 30, 340, 640), "screen": (326, 46, 308, 608), "r": 58},
}


def arrow_svg(box, iw, ih, accent):
    """Arrow pointing at the highlight box from the roomier side of the screenshot (picture only, no text)."""
    x, y, w, h = box["x"] * iw, box["y"] * ih, box["w"] * iw, box["h"] * ih
    cx, cy = x + w / 2, y + h / 2
    ox = min(iw - 24, x + w + 110) if cx < iw / 2 else max(24, x - 110)
    oy = min(ih - 24, y + h + 110) if cy < ih / 2 else max(24, y - 110)
    tx = x + w if ox > cx else x
    ty = y + h if oy > cy else y
    ang = math.atan2(ty - oy, tx - ox)
    hx, hy = tx - 8 * math.cos(ang), ty - 8 * math.sin(ang)
    p1 = (hx - 26 * math.cos(ang - 0.45), hy - 26 * math.sin(ang - 0.45))
    p2 = (hx - 26 * math.cos(ang + 0.45), hy - 26 * math.sin(ang + 0.45))
    line = "<line x1='%.0f' y1='%.0f' x2='%.0f' y2='%.0f' stroke-width='%d' stroke-linecap='round'/>"
    head = "<polygon points='%.0f,%.0f %.0f,%.0f %.0f,%.0f'/>" % (hx, hy, p1[0], p1[1], p2[0], p2[1])
    return ("<svg class='arw' width='%d' height='%d' viewBox='0 0 %d %d'><g stroke='rgba(0,0,0,.45)' fill='rgba(0,0,0,.45)'>%s%s</g>"
            "<g stroke='%s' fill='%s'>%s%s</g></svg>") % (
        iw, ih, iw, ih, line % (ox, oy, hx, hy, 14), head, accent, accent, line % (ox, oy, hx, hy, 8), head)


def shot_img(shot, info, box_w, box_h, out_dir, name, accent):
    """HTML for one screenshot drawn at its exact size (with the optional highlight). Returns (html, w, h)."""
    p, w, h = fit_image(info["path"], box_w, box_h, pathlib.Path(out_dir) / name)
    hl = ""
    b = shot.get("hl")
    if b:
        hl = ("<div class='hl' style='left:%dpx;top:%dpx;width:%dpx;height:%dpx'></div>" % (b["x"] * w, b["y"] * h, b["w"] * w, b["h"] * h))
        if b.get("arrow"):
            hl += arrow_svg(b, w, h, accent)
    return ("<div class='sw' style='width:%dpx;height:%dpx'><img src='%s' width='%d' height='%d'>%s</div>" % (w, h, images.uri(p), w, h, hl)), w, h


def pic_shots(slide, lookup, out_dir, accent, tag):
    shots = [s for s in slide["shots"] if lookup(s["id"])]
    if not shots:
        return "<div class='empty'></div>"
    layout = shotslides.valid_layout(slide["layout"], len(shots))
    out = []
    for k, (sl, shot) in enumerate(zip(slots(layout, len(shots)), shots)):
        x, y, w, h = sl
        inner, iw, ih = shot_img(shot, lookup(shot["id"]), w - 2 * FRAME_B, h - 2 * FRAME_B, out_dir, "%s_%d.png" % (tag, k), accent)
        fw, fh = iw + 2 * FRAME_B, ih + 2 * FRAME_B
        out.append("<div class='fr' style='left:%dpx;top:%dpx;width:%dpx;height:%dpx'>%s</div>" % (x + (w - fw) // 2, y + (h - fh) // 2, fw, fh, inner))
    return "".join(out)


def devbox(has_bubble):
    """Where the device sits: in the lower part when a bubble uses the headroom, otherwise centred above the caption band."""
    top = HEADROOM - 10 if has_bubble else (BAND[1] - DEVBOX_H) // 2
    return ((W - DEVBOX_W) // 2, top, DEVBOX_W, DEVBOX_H)


def device_top(slide, has_bubble):
    """Top edge of the drawn device in slide coordinates (what a device-slide bubble points at and must not cover)."""
    box = devbox(has_bubble)
    d = DEV[slide["device"] if slide["device"] in DEV else "tablet"]
    return {"x": box[0] + d["outer"][0], "y": box[1] + d["outer"][1], "r": box[0] + d["outer"][0] + d["outer"][2], "b": box[1] + d["outer"][1] + 30}


def pic_device(slide, lookup, out_dir, accent, tag, bg_file, box):
    kind = slide["device"] if slide["device"] in DEV else "tablet"
    d = DEV[kind]
    bg = ("<img class='bgimg' src='%s'><div class='bgdim'></div>" % images.uri(bg_file)) if bg_file else ""
    shot = next((s for s in slide["shots"] if lookup(s["id"])), None)
    ox, oy, ow, oh = d["outer"]
    sx, sy, sw, sh = d["screen"]
    chrome = "<div class='dev %s' style='left:%dpx;top:%dpx;width:%dpx;height:%dpx;border-radius:%dpx'></div>" % (kind, ox, oy, ow, oh, d["r"])
    if kind == "laptop":
        chrome += "<div class='lbase' style='left:%dpx;top:%dpx;width:%dpx'></div>" % (ox - 70, oy + oh - 1, ow + 140)
    elif kind == "tv":
        chrome += ("<div class='tneck' style='left:%dpx;top:%dpx'></div><div class='tfoot' style='left:%dpx;top:%dpx'></div>" % (ox + ow // 2 - 30, oy + oh - 1, ox + ow // 2 - 150, oy + oh + 49))
    elif kind == "phone":
        chrome += "<div class='notch' style='left:%dpx;top:%dpx'></div>" % (ox + ow // 2 - 50, sy + 10)
    scr = "<div class='scr' style='left:%dpx;top:%dpx;width:%dpx;height:%dpx;border-radius:%dpx'>" % (sx, sy, sw, sh, max(6, d["r"] - 20))
    if shot:
        inner, iw, ih = shot_img(shot, lookup(shot["id"]), sw, sh, out_dir, "%s_dev.png" % tag, accent)
        scr += inner
    scr += "</div>"
    return bg + "<div style='position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx'>%s%s</div>" % (box + (chrome, scr))


# ---------------- page ----------------
CSS = """<style>
.zr{position:relative;width:1080px;height:1350px;overflow:hidden;background:__BG__;color:__TX__;font-family:'Seg','Segoe UI','Nirmala UI',sans-serif}
.pic{position:absolute;left:0;top:0;width:1080px;height:1350px;overflow:hidden;background:#1a1a1d}
.pic img.ill{width:100%;height:100%;object-fit:cover;display:block}.pic .svgw{position:absolute;left:0;right:0;bottom:154px;height:605px}
.pic.grad{background:linear-gradient(135deg,__AC__ 0%,__BG__ 95%)}
.bgimg{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.bgdim{position:absolute;inset:0;background:rgba(0,0,0,.18)}
.fr{position:absolute;border:6px solid __AC__;border-radius:24px;background:__AC__;box-shadow:0 16px 40px rgba(0,0,0,.45);overflow:hidden}
.sw{position:relative;border-radius:14px;overflow:hidden;background:#fff}.sw img{display:block}
.hl{position:absolute;border:5px solid __AC__;border-radius:12px;box-shadow:0 0 0 3px rgba(0,0,0,.4)}.arw{position:absolute;left:0;top:0;pointer-events:none}
.dev{position:absolute;background:linear-gradient(160deg,#3a3e47,#15171b);border:2px solid #4a4f5a;box-shadow:0 24px 50px rgba(0,0,0,.5)}
.scr{position:absolute;background:#0f1218;overflow:hidden;display:flex;align-items:center;justify-content:center}
.lbase{position:absolute;height:26px;background:linear-gradient(#cfd3da,#8e939c);border-radius:0 0 26px 26px;box-shadow:0 14px 30px rgba(0,0,0,.45)}
.tneck{position:absolute;width:60px;height:50px;background:linear-gradient(90deg,#20232a,#3b3f48,#20232a)}.tfoot{position:absolute;width:300px;height:16px;border-radius:10px;background:#2a2d34}
.notch{position:absolute;width:100px;height:22px;border-radius:12px;background:#0a0b0d;z-index:3}
.bub{position:absolute;top:30px;left:0;width:fit-content;max-width:470px;background:#fff;color:#111;border-radius:30px;padding:12px 24px 14px;box-shadow:0 5px 18px rgba(0,0,0,.35);z-index:4}
.nm{display:inline-block;background:__AC__;color:__ONAC__;font-weight:700;font-size:22px;line-height:28px;padding:0 14px;border-radius:99px;margin-bottom:6px}
.bt{font-size:44px;line-height:1.3;font-weight:700;overflow:hidden}
#tails{position:absolute;left:0;top:0;width:1080px;height:1350px;z-index:3;pointer-events:none}
.band{position:absolute;left:0;top:1196px;width:1080px;height:104px;background:__BG__;display:flex;align-items:center;justify-content:center;padding:0 40px}
.cap{font-size:40px;line-height:1.2;font-weight:700;overflow:hidden;text-align:center;color:__TX__;width:100%}
.cta{background:__AC__;color:__ONAC__;font-weight:700;border-radius:99px;display:flex;align-items:center;justify-content:center;text-align:center;padding:0 40px;overflow:hidden;max-width:100%}
.ft{position:absolute;left:0;top:1300px;width:1080px;height:50px;background:__BG__;border-top:2px solid __LN__;display:flex;justify-content:space-between;align-items:center;padding:0 40px;font-size:22px;color:__MU__}
.ft .logo{height:30px}.ft .mark{font-size:26px}
</style>"""

FIT_JS = r"""(function(){
 function bx(e){var r=e.getBoundingClientRect();return {x:r.left,y:r.top,r:r.right,b:r.bottom};}
 var over=[];
 document.querySelectorAll('[data-fitbox]').forEach(function(e){
  var mh=parseFloat(e.dataset.maxh),mn=parseFloat(e.dataset.min||24),fs=parseFloat(getComputedStyle(e).fontSize),lh=parseFloat(e.dataset.lh||1.3),n=0;
  e.style.maxHeight=mh+'px';e.style.lineHeight=lh;
  while(e.scrollHeight>mh+1&&fs>mn&&n<80){fs=Math.max(mn,fs*0.94);lh=Math.max(1.1,lh-0.02);e.style.fontSize=fs+'px';e.style.lineHeight=lh;n++;}
  if(e.scrollHeight>mh+1)over.push(e.dataset.kind||'text');
 });
 var bubs=Array.prototype.slice.call(document.querySelectorAll('.bub')),placed=[],svg='';
 bubs.forEach(function(b){
  var w=b.getBoundingClientRect().width,cx=parseFloat(b.dataset.cx),left=Math.max(24,Math.min(1080-24-w,cx-w/2)),top=30;
  b.style.left=left+'px';
  var h=b.getBoundingClientRect().height;
  placed.forEach(function(p){if(left<p.r+16&&left+w>p.x-16&&top<p.b+16){top=p.b+24;}});
  b.style.top=top+'px';placed.push({x:left,r:left+w,b:top+h});
 });
 bubs.forEach(function(b){
  var r=b.getBoundingClientRect(),tx=parseFloat(b.dataset.tx),ty=parseFloat(b.dataset.ty),bx=Math.max(r.left+44,Math.min(r.right-44,tx));
  svg+="<polygon points='"+(bx-22)+","+(r.bottom-4)+" "+(bx+22)+","+(r.bottom-4)+" "+tx+","+ty+"' fill='#fff' data-box='"+Math.min(bx-22,tx)+","+(r.bottom-4)+","+Math.max(bx+22,tx)+","+ty+"'/>";
 });
 document.getElementById('tails').innerHTML=svg;
 var out={texts:[],over:over};
 document.querySelectorAll('#tails polygon').forEach(function(e){var a=e.getAttribute('data-box').split(',').map(Number);out.texts.push({kind:'tail',box:{x:a[0],y:a[1],r:a[2],b:a[3]}});});
 document.querySelectorAll('[data-text]').forEach(function(e){out.texts.push({kind:e.dataset.text,box:bx(e)});});
 return out;})()"""


def overlap(a, b, tol=0.5):
    return min(a["r"], b["r"]) - max(a["x"], b["x"]) > tol and min(a["b"], b["b"]) - max(a["y"], b["y"]) > tol


def check_layout(rep, heads=()):
    """Error messages (empty = clean). A message starting with 'face:' means a bubble touches a head: the renderer redraws the picture.
    heads: boxes {x,y,r,b} of the characters' heads (detected, or estimated from their slot)."""
    errs = []
    bub = [t for t in rep["texts"] if t["kind"] == "bubble"]
    for t in bub:
        b = t["box"]
        if b["x"] < -0.5 or b["y"] < BUB_TOP - 6 or b["r"] > W + 0.5 or b["b"] > BUB_BOTTOM + 0.5:
            errs.append("bubble is outside the headroom band")
        if any(overlap(b, h) for h in heads):
            errs.append("face: a speech bubble touches a face")
    for i, a in enumerate(bub):
        for b in bub[i + 1:]:
            if overlap(a["box"], b["box"]):
                errs.append("bubble overlaps bubble")
    for t in rep["texts"]:
        if t["kind"] in ("caption", "cta") and any(overlap(t["box"], h) for h in heads):
            errs.append("face: the caption touches a face")
    if rep.get("over"):
        errs.append("text too long for this slide (%s)" % ", ".join(rep["over"]))
    return list(dict.fromkeys(errs))


def build(spec, slide, i, n, ctx):
    """Full inner HTML of one slide. ctx: lookup(id)->info, out_dir, names{role:name}, scene_svg(slide), ill (picture path or ''), bg (device background),
    heads [head box per character] (from vision.head_boxes)."""
    pal = images.palette({"bg": spec["bg"], "accent": spec["accent"]})
    typ = slide.get("type", "scene")
    bubs, cap, cta_on = parts(slide, spec["cta"])
    tag = "_s%d" % i
    box = devbox(bool(bubs))
    if typ == "shot":
        pic = "<div class='pic grad' id='pic'>%s</div>" % pic_shots(slide, ctx["lookup"], ctx["out_dir"], pal["ac"], tag)
    elif typ == "device":
        pic = "<div class='pic grad' id='pic'>%s</div>" % pic_device(slide, ctx["lookup"], ctx["out_dir"], pal["ac"], tag, ctx.get("bg"), box)
    else:
        inner = ("<img class='ill' src='%s'>" % images.uri(ctx["ill"])) if ctx.get("ill") else "<div class='svgw'>%s</div>" % ctx["scene_svg"](slide)
        pic = "<div class='pic' id='pic'>%s</div>" % inner
    pos = {c["role"]: k for k, c in enumerate(slide["characters"])}
    heads = ctx.get("heads") or []
    b_html = []
    for d in bubs:
        k = pos.get(d["speaker"], 0)
        p = slide["characters"][k]["position"] if k < len(slide["characters"]) else "center"
        if typ == "scene" and k < len(heads):
            hb = heads[k]
            tx, ty = (hb["x"] + hb["r"]) / 2, hb["y"] - 8
        else:                                                   # device slide: point at the top edge of the device
            dt = device_top(slide, True)
            tx, ty = (dt["x"] + dt["r"]) / 2 + (SLOT_X.get(p, 0.5) - 0.5) * 400, dt["y"] - 8
        name = (ctx.get("names") or {}).get(d["speaker"], "")
        b_html.append("<div class='bub' data-text='bubble' data-cx='%.0f' data-tx='%.0f' data-ty='%.0f'>%s<div class='bt' data-fitbox='1' data-kind='speech bubble' data-maxh='%d' data-min='%d'>%s</div></div>" % (
            SLOT_X.get(p, 0.5) * W, tx, ty, ("<span class='nm'>%s</span><br>" % images.e(name)) if name else "", BUB_TEXT_H, BUB_MIN, images.e(d["text"])))
    band = ""
    if cap or cta_on:
        inner = ("<div class='cta' data-text='cta' data-fitbox='1' data-kind='call to action' data-maxh='%d' data-min='%d' data-lh='1.1' style='height:%dpx;font-size:%dpx'>%s</div>" % (
            CTA_H, CTA_MIN, CTA_H, CTA_FS, images.e(spec["cta"]))) if cta_on else (
            "<div class='cap' data-text='caption' data-fitbox='1' data-kind='caption' data-maxh='%d' data-min='%d' data-lh='1.2'>%s</div>" % (CAP_H, CAP_MIN, images.e(cap)))
        band = "<div class='band'>%s</div>" % inner
    css = CSS
    for k, v in {"__BG__": pal["bg"], "__TX__": pal["tx"], "__AC__": pal["ac"], "__ONAC__": pal["on_ac"], "__MU__": pal["mu"], "__LN__": "#2a2d34"}.items():
        css = css.replace(k, v)
    foot = "<div class='ft' data-text='footer'>%s<span>%d / %d</span><span>%s</span></div>" % (
        images.brandmark(spec), i + 1, n, images.e(" - ".join(x for x in (spec["footer_left"], spec["footer_right"]) if x)))
    return "%s<div class='zr'>%s<svg id='tails' viewBox='0 0 1080 1350'></svg>%s%s%s</div>" % (css, pic, "".join(b_html), band, foot)
