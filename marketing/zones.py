"""Slide layout for every type (cartoon scene, screenshot, device, call to action).
A 1080x1350 slide is split into fixed zones that never overlap: PICTURE box (top, 1080x760), TEXT box (below it), logo/footer strip. Nothing in the text box (speech bubbles,
caption, call-to-action button) and footer strip. Text is never drawn on top of the picture. After the page is laid out in Chromium, FIT_JS shrinks
text to a readable minimum and reports every box; check_layout() fails the render if anything overlaps or does not fit."""
import math, pathlib, re
from PIL import Image
from . import images, shotslides

W, H = 1080, 1350
PIC = (0, 0, 1080, 760)                  # picture box: top of the slide, full width (56% of the height). x, y, w, h
TXT = (60, 784, 960, 466)                # text box: everything below the picture, above the footer strip
FOOT = (60, 1270, 960, 64)               # logo / footer strip, inside the bottom margin
DEVBOX = (60, 30, 960, 700)              # drawn devices are laid out in this box inside the picture box
TAIL = 22                                # tail height, sits inside the text zone
BUBBLE_MAX = 0.64                        # bubble width as a share of the zone: leaves the other side free for the second tail
BUB_FS, BUB_MIN = 40, 28
CAP_FS, CAP_MIN = 54, 28
CTA_H, CTA_FS, CTA_MIN = 118, 50, 30
CHIP_H, BUB_PAD = 34, 26
FRAME_B = 6
UP_CAP = 1.6                             # screenshots are never enlarged more than this


# ---------------- text plan (shared by the HTML builder and the editor warning) ----------------
def strip_cta(text, cta):
    c = (cta or "").strip().casefold()
    return "" if c and c in (text or "").casefold() else text


def parts(slide, cta=""):
    """What goes in the text zone. On the call-to-action slide the CTA appears once (the button), never also in a bubble or caption."""
    on = bool(slide.get("is_cta") and cta)
    bubs = [] if slide.get("type") == "shot" else [d for d in slide["dialogue"] if d["text"].strip()]
    cap = slide.get("narration") or ""
    if on:
        bubs = [d for d in bubs if not strip_cta(d["text"], cta) == ""]
        cap = strip_cta(cap, cta)
    return bubs, cap, on


def budgets(n_bub, has_cap, has_cta):
    """Heights (px) for each text element so the zone is never over-full."""
    r = TXT[3]
    cta_h = 0
    if has_cta:
        cta_h = CTA_H
        r -= CTA_H + 14
    if n_bub:
        r -= TAIL
    cap_h = 0
    if has_cap:
        cap_h = 110 if n_bub else min(r, 340)
        r -= cap_h + (14 if n_bub else 0)
    bub_h = 0
    if n_bub:
        bub_h = max(0, min(260, (r - 14 * (n_bub - 1)) // n_bub))
    return {"cta": cta_h, "cap": cap_h, "bub": bub_h}


def est_height(text, width, fs, lh=1.12):
    """Rough text height at font size fs (Devanagari averages about 0.56 em per character)."""
    cpl = max(1, int(width / (fs * 0.56)))
    lines = sum(max(1, math.ceil(len(seg) / cpl)) for seg in str(text).split("\n"))
    return lines * fs * lh


def too_long(slide, cta=""):
    """'' if the slide's text fits at the minimum font size, else the message shown in the script editor."""
    bubs, cap, on = parts(slide, cta)
    b = budgets(len(bubs), bool(cap), on)
    bw = TXT[2] * BUBBLE_MAX - 2 * 24
    if bubs and b["bub"] - CHIP_H - BUB_PAD < CAP_MIN * 1.12:
        return "text too long for this slide (no room for the speech bubbles together with the caption and button)"
    for d in bubs:
        if est_height(d["text"], bw, BUB_MIN) > b["bub"] - CHIP_H - BUB_PAD:
            return "text too long for this slide (speech bubble)"
    if cap and est_height(cap, TXT[2], CAP_MIN) > b["cap"]:
        return "text too long for this slide (caption)"
    if on and est_height(cta, TXT[2] - 80, CTA_MIN) > CTA_H - 30:
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


def slots(layout, n):
    """Slot rectangles (x, y, w, h) inside the picture zone."""
    ax, ay, aw, ah, g = 24, 24, PIC[2] - 48, PIC[3] - 48, 20
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


DEV = {   # outer box, screen box (inside the outer), css radius for outer
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


def pic_device(slide, lookup, out_dir, accent, tag, bg_file):
    d = DEV[slide["device"] if slide["device"] in DEV else "tablet"]
    bg = ("<img class='bgimg' src='%s'><div class='bgdim'></div>" % images.uri(bg_file)) if bg_file else ""
    shot = next((s for s in slide["shots"] if lookup(s["id"])), None)
    ox, oy, ow, oh = d["outer"]
    sx, sy, sw, sh = d["screen"]
    kind = slide["device"] if slide["device"] in DEV else "tablet"
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
        scr += inner.replace("class='sw' style=", "class='sw' data-c='1' style=")
    scr += "</div>"
    return bg + "<div style='position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx'>%s%s</div>" % (DEVBOX + (chrome, scr))


# ---------------- page ----------------
CSS = """<style>
.zr{position:relative;width:1080px;height:1350px;overflow:hidden;background:__BG__;color:__TX__}
.ft{position:absolute;left:60px;top:1270px;width:960px;height:64px;border-top:2px solid __LN__;display:flex;justify-content:space-between;align-items:center;font-size:26px;color:__MU__}
.ft .logo{height:44px}.ft .mark{font-size:34px}
.pic{position:absolute;left:0;top:0;width:1080px;height:760px;border-radius:0 0 36px 36px;overflow:hidden;background:#1a1a1d}
.pic img.ill{width:100%;height:100%;object-fit:cover;display:block}.pic .svgw{position:absolute;inset:0}
.pic.grad{background:linear-gradient(135deg,__AC__ 0%,__BG2__ 95%)}
.bgimg{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.bgdim{position:absolute;inset:0;background:rgba(0,0,0,.18)}
.fr{position:absolute;border:6px solid __AC__;border-radius:24px;background:__AC__;box-shadow:0 16px 40px rgba(0,0,0,.45);overflow:hidden}
.sw{position:relative;border-radius:14px;overflow:hidden;background:#fff}.sw img{display:block}
.hl{position:absolute;border:5px solid __AC__;border-radius:12px;box-shadow:0 0 0 3px rgba(0,0,0,.4)}.arw{position:absolute;left:0;top:0;pointer-events:none}
.dev{position:absolute;background:linear-gradient(160deg,#3a3e47,#15171b);border:2px solid #4a4f5a;box-shadow:0 24px 50px rgba(0,0,0,.5)}
.scr{position:absolute;background:#0f1218;overflow:hidden;display:flex;align-items:center;justify-content:center}
.lbase{position:absolute;height:26px;background:linear-gradient(#cfd3da,#8e939c);border-radius:0 0 26px 26px;box-shadow:0 14px 30px rgba(0,0,0,.45)}
.tneck{position:absolute;width:60px;height:50px;background:linear-gradient(90deg,#20232a,#3b3f48,#20232a)}.tfoot{position:absolute;width:300px;height:16px;border-radius:10px;background:#2a2d34}
.notch{position:absolute;width:100px;height:22px;border-radius:12px;background:#0a0b0d;z-index:3}
.tz{position:absolute;left:60px;top:784px;width:960px;height:466px;display:flex;flex-direction:column;gap:14px}
.bw{position:relative;width:100%;flex:none}.bw.first{margin-top:22px}
.bb{background:#fff;color:#111;border-radius:30px;padding:12px 24px 12px;width:fit-content;max-width:64%;min-width:38%;overflow:hidden;box-shadow:0 5px 18px rgba(0,0,0,.35)}
.bw.l .bb{margin-right:auto}.bw.r .bb{margin-left:auto}.bw.c .bb{margin:0 auto;max-width:76%;min-width:0}
.chip{display:inline-block;background:__AC__;color:__ONAC__;font-weight:700;font-size:22px;line-height:28px;padding:0 14px;border-radius:99px;margin-bottom:6px}
.bt{font-size:40px;line-height:1.3;font-weight:700;overflow:hidden}
.tail{position:absolute;width:0;height:0;border-left:14px solid transparent;border-right:14px solid transparent;border-bottom:22px solid #fff;transform:translateX(-50%);top:-22px}
.cap{font-size:54px;line-height:1.3;font-weight:700;overflow:hidden;color:__TX__;flex:none}
.cta{background:__AC__;color:__ONAC__;font-weight:700;border-radius:99px;display:flex;align-items:center;justify-content:center;text-align:center;padding:0 40px;flex:none;overflow:hidden;align-self:center;max-width:100%}
</style>"""

FIT_JS = r"""(function(){
 function bx(e){var r=e.getBoundingClientRect();return {x:r.left,y:r.top,r:r.right,b:r.bottom};}
 var over=[];
 document.querySelectorAll('[data-fitbox]').forEach(function(e){
  var mh=parseFloat(e.dataset.maxh),mn=parseFloat(e.dataset.min||28),fs=parseFloat(getComputedStyle(e).fontSize),lh=1.3,n=0;
  e.style.maxHeight=mh+'px';e.style.lineHeight=lh;
  while(e.scrollHeight>mh+1&&fs>mn&&n<80){fs=Math.max(mn,fs*0.94);lh=Math.max(1.12,lh-0.02);e.style.fontSize=fs+'px';e.style.lineHeight=lh;n++;}
  if(e.scrollHeight>mh+1)over.push(e.dataset.kind||'text');
 });
 var tz=document.getElementById('tz'),zt=tz.getBoundingClientRect().top;
 document.querySelectorAll('i.tail').forEach(function(t){
  var w=t.parentNode.getBoundingClientRect().top;var h=Math.max(22,w-zt);t.style.top=(-h)+'px';t.style.borderBottomWidth=h+'px';});
 var out={pic:bx(document.getElementById('pic')),texts:[],over:over,zone:bx(tz),zoneFull:tz.scrollHeight>tz.clientHeight+1};
 document.querySelectorAll('[data-text]').forEach(function(e){out.texts.push({kind:e.dataset.text,box:bx(e)});});
 return out;})()"""


def overlap(a, b, tol=0.5):
    return min(a["r"], b["r"]) - max(a["x"], b["x"]) > tol and min(a["b"], b["b"]) - max(a["y"], b["y"]) > tol


def check_layout(rep):
    """Error messages (empty = layout is clean): any text/strip box touching the picture, text boxes touching each other, text that does not fit."""
    errs = []
    pic = rep["pic"]
    for t in rep["texts"]:
        if overlap(t["box"], pic):
            errs.append("%s overlaps the picture" % t["kind"])
        b = t["box"]
        if b["x"] < -0.5 or b["y"] < -0.5 or b["r"] > W + 0.5 or b["b"] > H + 0.5:
            errs.append("%s is outside the slide" % t["kind"])
    tx = [t for t in rep["texts"] if t["kind"] in ("bubble", "caption", "cta", "tail")]
    for i, a in enumerate(tx):
        for b in tx[i + 1:]:
            if {a["kind"], b["kind"]} <= {"bubble", "caption", "cta", "tail"} and overlap(a["box"], b["box"]):
                errs.append("%s overlaps %s" % (a["kind"], b["kind"]))
    if rep.get("over") or rep.get("zoneFull"):
        errs.append("text too long for this slide (%s)" % ", ".join(rep.get("over") or ["text zone"]))
    return errs


def build(spec, slide, i, n, ctx):
    """Full inner HTML of one slide. ctx: lookup(id)->info, out_dir, names{role:name}, scene_svg(slide), ill (illustration path or ''), bg (device background path or '')."""
    pal = images.palette({"bg": spec["bg"], "accent": spec["accent"]})
    typ = slide.get("type", "scene")
    bubs, cap, cta_on = parts(slide, spec["cta"])
    b = budgets(len(bubs), bool(cap), cta_on)
    tag = "_s%d" % i
    if typ == "shot":
        pic = "<div class='pic grad' id='pic'>%s</div>" % pic_shots(slide, ctx["lookup"], ctx["out_dir"], pal["ac"], tag)
    elif typ == "device":
        pic = "<div class='pic grad' id='pic'>%s</div>" % pic_device(slide, ctx["lookup"], ctx["out_dir"], pal["ac"], tag, ctx.get("bg"))
    else:
        inner = ("<img class='ill' src='%s'>" % images.uri(ctx["ill"])) if ctx.get("ill") else "<div class='svgw'>%s</div>" % ctx["scene_svg"](slide)
        pic = "<div class='pic' id='pic'>%s</div>" % inner
    pos = {c["role"]: c["position"] for c in slide["characters"]}
    z, prev = [], None
    for k, d in enumerate(bubs):
        p = pos.get(d["speaker"], "center")
        side = {"left": "l", "right": "r"}.get(p, "c")
        x = {"l": 22, "r": 78, "c": 50}[side]
        name = (ctx.get("names") or {}).get(d["speaker"], "")
        chip = "<span class='chip'>%s</span><br>" % images.e(name) if name else ""
        long_tail = k > 0 and prev is not None and prev != side and side != "c" and prev != "c"
        tail = "" if (k > 0 and not long_tail) else "<i class='tail' data-text='tail' style='left:%d%%'></i>" % x
        z.append("<div class='bw %s%s'><div class='bb' data-text='bubble'>%s<div class='bt' data-fitbox='1' data-kind='speech bubble' data-maxh='%d' data-min='%d'>%s</div></div>%s</div>" % (
            side, " first" if k == 0 else "", chip, max(24, b["bub"] - CHIP_H * (1 if name else 0) - BUB_PAD), BUB_MIN, images.e(d["text"]), tail))
        prev = side
    if cap:
        z.append("<div class='cap' data-text='caption' data-fitbox='1' data-kind='caption' data-maxh='%d' data-min='%d' style='max-height:%dpx'>%s</div>" % (b["cap"], CAP_MIN, b["cap"], images.e(cap)))
    if cta_on:
        z.append("<div class='cta' data-text='cta' data-fitbox='1' data-kind='call to action' data-maxh='%d' data-min='%d' style='height:%dpx;font-size:%dpx'>%s</div>" % (
            CTA_H, CTA_MIN, CTA_H, CTA_FS, images.e(spec["cta"])))
    z_html = "<div class='tz' id='tz'>%s</div>" % "".join(z)
    css = CSS
    for k, v in {"__BG__": pal["bg"], "__TX__": pal["tx"], "__AC__": pal["ac"], "__ONAC__": pal["on_ac"], "__MU__": pal["mu"], "__LN__": "#2a2d34", "__BG2__": pal["bg"]}.items():
        css = css.replace(k, v)
    foot = "<div class='ft' data-text='footer'>%s<span>%d / %d</span><span>%s</span></div>" % (
        images.brandmark(spec), i + 1, n, images.e(" - ".join(x for x in (spec["footer_left"], spec["footer_right"]) if x)))
    return "%s<div class='zr'>%s%s%s</div>" % (css, pic, z_html, foot)
