"""Cartoon story carousel: a fixed kit of original SVG parts (scenes, characters, expressions, props) that a script can combine,
script validation, the AI prompts, and the Playwright renderer (Chromium shapes Devanagari). Nothing here is tied to one story:
the story, dialogue and claims all come from the script (written by the AI from the owner's text, then edited by the owner).
No known or copied characters: every shape below is drawn from simple primitives.
"""
import json, re, pathlib
from . import images

SCENES = {"shop_counter": "Shop counter", "shelves": "Shelves behind a counter", "shop_front": "Shop front", "plain": "Plain background"}
ROLES = {"shopkeeper": "Shopkeeper", "shopkeeper2": "Second shopkeeper", "customer": "Customer"}
EXPRESSIONS = ["neutral", "happy", "worried", "thinking", "surprised", "angry"]
POSITIONS = ["left", "right", "center"]
PROPS = {"tablet": "Tablet", "phone": "Phone", "notebook": "Notebook", "boxes": "Stock boxes", "calendar": "Calendar", "receipt": "Receipt",
         "bag": "Shopping bag", "coin": "Coin", "lightbulb": "Idea bulb", "clock": "Clock", "question": "Question mark", "tick": "Tick", "cross": "Cross"}
SURFACE = ("tablet", "phone", "notebook", "boxes", "calendar", "receipt", "bag", "coin")      # sit on the counter
FLOAT = ("lightbulb", "clock", "question", "tick", "cross")                                    # float above the scene
LANGS = {"Hindi": "Hindi in Devanagari script", "English": "English", "Hinglish": "Hinglish (Hindi written in Roman letters)"}
MIN_SLIDES, MAX_SLIDES = 3, 8
CLAIM_WORDS = re.compile(r"guarantee|100\s?%|\bbest\b|no\.?\s?1\b|#1|\bfree\b|lifetime|never fail|zero loss|\bonly\b", re.I)

# ---------------- SVG parts ----------------
SKIN = {"shopkeeper": "#e0ac7e", "shopkeeper2": "#c68b59", "customer": "#f0c8a0"}
SHIRT = {"shopkeeper": "#f5a524", "shopkeeper2": "#3f8f6b", "customer": "#8a6bb5"}
HAIR = {"shopkeeper": "#2b2118", "shopkeeper2": "#1f1a16", "customer": "#4a3322"}
INK = "#1a1410"


def _brows(expr):
    L = {"neutral": ((-42, 222), (-14, 222)), "happy": ((-42, 220), (-14, 216)), "worried": ((-42, 228), (-14, 214)),
         "thinking": ((-42, 222), (-14, 222)), "surprised": ((-42, 206), (-14, 204)), "angry": ((-42, 214), (-14, 228))}[expr]
    R = {"neutral": ((14, 222), (42, 222)), "happy": ((14, 216), (42, 220)), "worried": ((14, 214), (42, 228)),
         "thinking": ((14, 214), (42, 204)), "surprised": ((14, 204), (42, 206)), "angry": ((14, 228), (42, 214))}[expr]
    return "".join("<line x1='%d' y1='%d' x2='%d' y2='%d' stroke='%s' stroke-width='6' stroke-linecap='round'/>" % (a[0], a[1], b[0], b[1], INK) for a, b in (L, R))


def _mouth(expr, speaking):
    if speaking and expr in ("neutral", "happy", "thinking"):
        return "<path d='M-20 282 Q0 316 20 282 Z' fill='#7a2a2a' stroke='%s' stroke-width='4' stroke-linejoin='round'/>" % INK
    d = {"neutral": "M-18 288 L18 288", "happy": "M-26 280 Q0 308 26 280", "worried": "M-20 294 Q0 280 20 294",
         "thinking": "M-14 288 L14 284", "angry": "M-22 296 Q0 278 22 296"}
    if expr == "surprised":
        return "<ellipse cx='0' cy='290' rx='10' ry='15' fill='#7a2a2a' stroke='%s' stroke-width='4'/>" % INK
    return "<path d='%s' fill='none' stroke='%s' stroke-width='5' stroke-linecap='round'/>" % (d[expr], INK)


def character(role, expr, cx, speaking=False, look=0):
    role = role if role in ROLES else "shopkeeper"
    expr = expr if expr in EXPRESSIONS else "neutral"
    skin, shirt, hair = SKIN[role], SHIRT[role], HAIR[role]
    g = ["<g transform='translate(%d,0)'>" % cx]
    g.append("<rect x='-130' y='352' width='44' height='190' rx='22' fill='%s' transform='rotate(8 -108 352)'/>" % shirt)
    g.append("<rect x='86' y='352' width='44' height='190' rx='22' fill='%s' transform='rotate(-8 108 352)'/>" % shirt)
    g.append("<rect x='-95' y='318' width='190' height='280' rx='64' fill='%s'/>" % shirt)
    g.append("<rect x='-20' y='296' width='40' height='36' rx='12' fill='%s'/>" % skin)
    if role == "customer":
        g.append("<ellipse cx='0' cy='252' rx='80' ry='84' fill='%s'/>" % hair)
    g.append("<circle cx='-70' cy='258' r='14' fill='%s'/><circle cx='70' cy='258' r='14' fill='%s'/>" % (skin, skin))
    g.append("<circle cx='0' cy='250' r='70' fill='%s'/>" % skin)
    if role == "shopkeeper":                                                       # cap
        g.append("<path d='M-72 232 A72 72 0 0 1 72 232 Z' fill='%s'/><rect x='-80' y='226' width='160' height='14' rx='7' fill='%s'/>" % (hair, hair))
    elif role == "shopkeeper2":                                                    # hair and moustache
        g.append("<path d='M-70 240 A70 70 0 0 1 70 240 Q40 214 0 220 Q-40 214 -70 240 Z' fill='%s'/>" % hair)
        g.append("<path d='M-30 276 Q-14 262 0 272 Q14 262 30 276 Q14 284 0 278 Q-14 284 -30 276 Z' fill='%s'/>" % hair)
    else:                                                                          # customer fringe
        g.append("<path d='M-70 244 A70 70 0 0 1 70 244 Q36 216 -10 230 Q-46 238 -70 244 Z' fill='%s'/>" % hair)
    for ex in (-26, 26):
        g.append("<circle cx='%d' cy='248' r='11' fill='#fff'/><circle cx='%d' cy='249' r='5.5' fill='%s'/>" % (ex, ex + look, INK))
    g.append(_brows(expr))
    g.append(_mouth(expr, speaking))
    if expr == "worried":
        g.append("<path d='M64 206 q10 18 0 26 q-10 -8 0 -26z' fill='#8ec5ff'/>")
    g.append("</g>")
    return "".join(g)


def scene_bg(scene):
    wall = "<rect width='1000' height='560' rx='36' fill='#2b2b33'/>"
    if scene == "shelves":
        s = [wall]
        cols = ["#c9a063", "#8ab0a0", "#d98a6a", "#b9a3d6", "#e0c26a", "#7aa0c8"]
        for row, y in enumerate((120, 215, 310)):
            s.append("<rect x='40' y='%d' width='920' height='10' rx='4' fill='#6b4a26'/>" % (y + 52))
            x = 60
            for i in range(11):
                w = 56 + ((i * 7 + row * 5) % 4) * 10
                h = 40 + ((i * 3 + row) % 3) * 10
                s.append("<rect x='%d' y='%d' width='%d' height='%d' rx='5' fill='%s'/>" % (x, y + 52 - h, w, h, cols[(i + row * 2) % 6]))
                x += w + 12
                if x > 900:
                    break
        return "".join(s)
    if scene == "shop_front":
        aw = "".join("<rect x='%d' y='20' width='84' height='80' fill='%s'/>" % (i * 84 + 80, "#f5a524" if i % 2 == 0 else "#3a3a42") for i in range(10))
        return (wall + "<rect x='60' y='20' width='880' height='12' fill='#1a1a1e'/>" + aw +
                "<rect x='640' y='130' width='240' height='330' rx='16' fill='#3a3a42'/><rect x='664' y='154' width='192' height='282' rx='10' fill='#55555e'/>"
                "<rect x='90' y='150' width='400' height='240' rx='12' fill='#34343c'/><rect x='108' y='168' width='364' height='204' rx='8' fill='#46464f'/>")
    if scene == "plain":
        return wall + "<circle cx='860' cy='120' r='140' fill='#f5a524' opacity='.08'/><circle cx='140' cy='460' r='180' fill='#f5a524' opacity='.06'/>"
    return wall + "<rect x='60' y='60' width='880' height='260' rx='18' fill='#33333b'/><rect x='80' y='80' width='840' height='220' rx='10' fill='#3b3b44'/>"


def counter():
    return ("<rect x='0' y='430' width='1000' height='130' fill='#a06a2c'/><rect x='0' y='418' width='1000' height='26' rx='6' fill='#c88a42'/>"
            "<rect x='40' y='470' width='200' height='70' rx='8' fill='#8f5c24'/><rect x='400' y='470' width='200' height='70' rx='8' fill='#8f5c24'/>"
            "<rect x='760' y='470' width='200' height='70' rx='8' fill='#8f5c24'/>")


def prop(name, x):
    t = "<g transform='translate(%d,418)'>" % x
    if name == "tablet":
        t += ("<rect x='-58' y='-104' width='116' height='104' rx='10' fill='#1d1d22' stroke='#666' stroke-width='3'/><rect x='-48' y='-94' width='96' height='84' rx='4' fill='#2e3d4f'/>"
              "<rect x='-38' y='-82' width='52' height='8' rx='3' fill='#f5a524'/><rect x='-38' y='-64' width='76' height='6' rx='3' fill='#8aa'/><rect x='-38' y='-50' width='60' height='6' rx='3' fill='#8aa'/>"
              "<rect x='-38' y='-34' width='30' height='16' rx='3' fill='#4caf7d'/>")
    elif name == "phone":
        t += "<rect x='-24' y='-92' width='48' height='92' rx='9' fill='#1d1d22' stroke='#666' stroke-width='3'/><rect x='-19' y='-84' width='38' height='72' rx='4' fill='#2e3d4f'/><circle cx='0' cy='-66' r='9' fill='#4caf7d'/>"
    elif name == "notebook":
        t += ("<rect x='-46' y='-96' width='92' height='96' rx='6' fill='#c0553a'/><rect x='-38' y='-88' width='78' height='80' rx='3' fill='#f3ecd8'/>" +
              "".join("<line x1='-30' y1='%d' x2='32' y2='%d' stroke='#9aa' stroke-width='2'/>" % (y, y) for y in range(-72, -12, 14)) +
              "".join("<circle cx='-46' cy='%d' r='4' fill='#222'/>" % y for y in range(-84, -8, 16)))
    elif name == "boxes":
        t += ("<rect x='-64' y='-60' width='72' height='60' rx='4' fill='#c9a063'/><rect x='-64' y='-60' width='72' height='12' fill='#b58a4a'/>"
              "<rect x='6' y='-48' width='60' height='48' rx='4' fill='#d8b374'/><rect x='-40' y='-110' width='64' height='50' rx='4' fill='#b98d4f'/><rect x='-8' y='-110' width='8' height='50' fill='#e8d5a8'/>")
    elif name == "calendar":
        t += ("<rect x='-48' y='-100' width='96' height='100' rx='8' fill='#f4f1e8'/><rect x='-48' y='-100' width='96' height='26' rx='8' fill='#d64545'/>" +
              "".join("<rect x='%d' y='%d' width='14' height='10' rx='2' fill='#9a9a9a'/>" % (-36 + c * 22, -62 + r * 18) for r in range(3) for c in range(4)))
    elif name == "receipt":
        t += ("<path d='M-34 -110 H34 V0 L26 -8 L18 0 L10 -8 L2 0 L-6 -8 L-14 0 L-22 -8 L-30 0 L-34 -6 Z' fill='#f7f4ea'/>" +
              "".join("<line x1='-22' y1='%d' x2='22' y2='%d' stroke='#999' stroke-width='3'/>" % (y, y) for y in range(-92, -24, 14)))
    elif name == "bag":
        t += "<path d='M-44 -86 H44 L54 0 H-54 Z' fill='#4caf7d'/><path d='M-24 -86 Q-24 -122 0 -122 Q24 -122 24 -86' fill='none' stroke='#2f7f57' stroke-width='7'/>"
    elif name == "coin":
        t += "<circle cx='0' cy='-34' r='34' fill='#f5c542' stroke='#c99a1d' stroke-width='5'/><text x='0' y='-22' font-size='38' font-weight='700' text-anchor='middle' fill='#8a6410'>&#8377;</text>"
    t += "</g>"
    return t


def float_prop(name, x, y):
    t = "<g transform='translate(%d,%d)'>" % (x, y)
    if name == "lightbulb":
        t += "<circle cx='0' cy='0' r='30' fill='#ffd84a'/><rect x='-14' y='28' width='28' height='16' rx='4' fill='#bbb'/><g stroke='#ffd84a' stroke-width='5' stroke-linecap='round'><line x1='-46' y1='-10' x2='-58' y2='-14'/><line x1='46' y1='-10' x2='58' y2='-14'/><line x1='0' y1='-46' x2='0' y2='-60'/></g>"
    elif name == "clock":
        t += "<circle cx='0' cy='0' r='32' fill='#f4f1e8' stroke='#444' stroke-width='5'/><line x1='0' y1='0' x2='0' y2='-20' stroke='#222' stroke-width='5' stroke-linecap='round'/><line x1='0' y1='0' x2='14' y2='6' stroke='#222' stroke-width='5' stroke-linecap='round'/>"
    elif name == "question":
        t += "<text x='0' y='28' font-size='96' font-weight='700' text-anchor='middle' fill='#f5a524'>?</text>"
    elif name == "tick":
        t += "<circle cx='0' cy='0' r='32' fill='#4caf7d'/><path d='M-14 0 L-4 12 L16 -12' fill='none' stroke='#fff' stroke-width='8' stroke-linecap='round' stroke-linejoin='round'/>"
    elif name == "cross":
        t += "<circle cx='0' cy='0' r='32' fill='#d64545'/><path d='M-13 -13 L13 13 M13 -13 L-13 13' stroke='#fff' stroke-width='8' stroke-linecap='round'/>"
    return t + "</g>"


def scene_svg(slide):
    """One scene: background, characters (max 2), counter, props. Everything comes from the slide dict."""
    scene = slide.get("scene") if slide.get("scene") in SCENES else "shop_counter"
    xs = {"left": 270, "right": 730, "center": 500}
    speakers = {d.get("speaker") for d in slide.get("dialogue", []) if (d.get("text") or "").strip()}
    out = ["<svg viewBox='0 0 1000 560' xmlns='http://www.w3.org/2000/svg' style='width:100%;height:100%;display:block'>"
           "<defs><clipPath id='cp'><rect width='1000' height='560' rx='36'/></clipPath></defs><g clip-path='url(#cp)'>", scene_bg(scene)]
    for ch in slide.get("characters", [])[:2]:
        cx = xs.get(ch.get("position"), 500)
        out.append(character(ch.get("role"), ch.get("expression"), cx, ch.get("role") in speakers, 5 if cx < 500 else (-5 if cx > 500 else 0)))
    if scene == "shop_front":
        out.append("<rect x='0' y='500' width='1000' height='60' fill='#3a3a42'/>")
    elif scene != "plain":
        out.append(counter())
    props = [p for p in slide.get("props", []) if p in PROPS]
    sx, fx = [150, 360, 640, 850], [(500, 120), (400, 90), (600, 90)]
    si = fi = 0
    for p in props:
        if p in SURFACE and scene not in ("shop_front", "plain") and si < len(sx):
            out.append(prop(p, sx[si]))
            si += 1
        elif fi < len(fx):
            out.append(float_prop(p, *fx[fi]) if p in FLOAT else float_prop("tick", *fx[fi]))
            fi += 1
    out.append("</g></svg>")
    return "".join(out)


# ---------------- script ----------------
def blank_slide():
    return {"scene": "shop_counter", "description": "", "characters": [{"role": "shopkeeper", "expression": "neutral", "position": "left"}],
            "props": [], "dialogue": [{"speaker": "shopkeeper", "text": ""}], "narration": "", "claim_note": ""}


def clean_slide(s):
    s = s if isinstance(s, dict) else {}
    chars = []
    for c in (s.get("characters") or [])[:2]:
        if isinstance(c, dict):
            chars.append({"role": c.get("role") if c.get("role") in ROLES else "shopkeeper",
                          "expression": c.get("expression") if c.get("expression") in EXPRESSIONS else "neutral",
                          "position": c.get("position") if c.get("position") in POSITIONS else ""})
    if not chars:
        chars = [{"role": "shopkeeper", "expression": "neutral", "position": ""}]
    if len(chars) == 1:
        chars[0]["position"] = chars[0]["position"] or "center"
    else:
        a, b = chars[0]["position"], chars[1]["position"]
        if a not in ("left", "right") or a == b:
            chars[0]["position"], chars[1]["position"] = "left", "right"
        elif b not in ("left", "right") or a == b:
            chars[1]["position"] = "right" if a == "left" else "left"
    roles = [c["role"] for c in chars]
    dlg = []
    for d in (s.get("dialogue") or [])[:2]:
        if isinstance(d, dict) and str(d.get("text") or "").strip():
            dlg.append({"speaker": d.get("speaker") if d.get("speaker") in roles else roles[0], "text": str(d["text"]).strip()})
    props = [p for p in (s.get("props") or []) if p in PROPS][:4]
    return {"scene": s.get("scene") if s.get("scene") in SCENES else "shop_counter", "description": str(s.get("description") or "").strip(),
            "characters": chars, "props": props, "dialogue": dlg, "narration": str(s.get("narration") or "").strip(),
            "claim_note": str(s.get("unsupported_claim") or s.get("claim_note") or "").strip()}


def claim_flags(slide, facts):
    """Yellow-note text if the slide states something that is not in the product record; '' if fine."""
    if slide.get("claim_note"):
        return slide["claim_note"]
    text = " ".join([d["text"] for d in slide["dialogue"]] + [slide["narration"]])
    f = (facts or "").lower()
    nums = set(re.findall(r"\d[\d,.]*", text)) - set(re.findall(r"\d[\d,.]*", facts or ""))
    words = {m.group(0).lower() for m in CLAIM_WORDS.finditer(text) if m.group(0).lower() not in f}
    if nums or words:
        return "claim not in product details - confirm before posting"
    return ""


def product_facts(product):
    if not product:
        return ""
    return "\n".join(x for x in (product["name"], product["description"] or "", product["key_points"] or "", product["price_note"] or "") if x)


def clean_script(d, n, facts, lang=""):
    d = d if isinstance(d, dict) else {}
    slides = [clean_slide(s) for s in (d.get("slides") or [])][:MAX_SLIDES]
    if not slides:
        raise ValueError("The AI returned no slides.")
    for s in slides:
        note = claim_flags(s, facts)
        s["claim_note"] = "claim not in product details - confirm before posting" if note else ""
    return {"title": str(d.get("title") or "").strip(), "caption": str(d.get("caption") or "").strip(), "alt": str(d.get("alt") or "").strip(),
            "hashtags_raw": d.get("hashtags") or [], "slides": slides, "language": lang}


def parts_text():
    return ("scene: %s\nrole: %s\nexpression: %s\nposition: %s\nprops: %s" % (
        " | ".join(SCENES), " | ".join(ROLES), " | ".join(EXPRESSIONS), " | ".join(POSITIONS), " | ".join(PROPS)))


SHAPE = ('{"title":"", "caption":"", "alt":"", "hashtags":{"broad":[],"niche":[],"local":[]}, "slides":[{"scene":"", "description":"what the scene shows", '
         '"characters":[{"role":"","expression":"","position":""}], "props":[], "dialogue":[{"speaker":"role","text":""}], "narration":"", "unsupported_claim":""}]}')


def script_prompt(brief, product, brand_name, language, n, tone, cta):
    facts = product_facts(product) or "(no product selected: make NO product claims)"
    return (
        "You write short cartoon story scripts for Instagram carousels for a small Indian software company.\n"
        "The owner describes the story in their own words (Hindi, English or Hinglish):\n<<<\n%s\n>>>\n\n"
        "PRODUCT FACTS (the only product claims you may state):\n%s\n\n"
        "Rules:\n- Write exactly %d slides telling the story in order. Each slide has 1 or 2 short dialogue lines (max 14 words each), spoken by the characters in it.\n"
        "- Dialogue and narration language: %s. Tone: %s.\n"
        "- You may ONLY state product claims that appear in PRODUCT FACTS. If the story asks for a claim that is not there (a number, price, guarantee, feature), "
        "write the slide without it and put a short note of the missing claim in that slide's unsupported_claim. Never invent numbers, prices or results.\n"
        "- Choose every visual ONLY from this list of drawn parts:\n%s\n"
        "- Max 2 characters per slide; props max 4. The last slide ends with this call to action (in the dialogue or narration): %s\n"
        "- Brand: %s. Also write caption (2-4 short sentences), alt (one plain sentence describing the carousel), and hashtags "
        "(object with arrays broad, niche, local; 12-15 in total; local tags relate to Buxar and Bihar; no spam tags).\n"
        "Return ONLY JSON in this shape:\n%s"
    ) % (brief.strip(), facts, n, LANGS.get(language, LANGS["Hinglish"]), tone or "Friendly", parts_text(), cta or "(none)", brand_name, SHAPE)


def slide_prompt(script, index, brief, product, language, tone, instruction=""):
    facts = product_facts(product) or "(no product selected: make NO product claims)"
    keep = {"slides": [{k: s[k] for k in ("scene", "characters", "props", "dialogue", "narration")} for s in script["slides"]]}
    return (
        "Rewrite ONLY slide number %d of this cartoon story script (slides are numbered from 1), keeping the story flow. Same language (%s), tone %s.\n"
        "Story idea: %s\nExtra instruction: %s\nPRODUCT FACTS (only product claims allowed):\n%s\nIf a claim is missing from the facts, leave it out and note it in unsupported_claim.\n"
        "Choose visuals only from:\n%s\nCurrent script:\n%s\nReturn ONLY JSON for that one slide: "
        '{"scene":"","description":"","characters":[{"role":"","expression":"","position":""}],"props":[],"dialogue":[{"speaker":"role","text":""}],"narration":"","unsupported_claim":""}'
    ) % (index + 1, LANGS.get(language, language), tone or "Friendly", brief.strip() or "(see script)", instruction or "make it better and different", facts, parts_text(),
         json.dumps(keep, ensure_ascii=False))


# ---------------- slide HTML + render ----------------
CSS = """<style>
body{font-family:'Seg','Segoe UI','Nirmala UI',sans-serif}
.cart{flex:1;min-height:0;display:flex;flex-direction:column;gap:20px}
.bbs{display:flex;flex-direction:column;gap:16px;flex:none}
.bb{background:#f4f4f5;color:#111;border-radius:34px;padding:22px 30px;font-size:46px;line-height:1.3;max-width:80%;overflow:hidden;position:relative;font-weight:700}
.bb.l{align-self:flex-start;border-bottom-left-radius:8px}.bb.r{align-self:flex-end;border-bottom-right-radius:8px}
.scn{flex:1;min-height:0;border-radius:36px;overflow:hidden}
.nar{font-size:36px;line-height:1.3;color:__MU__;flex:none;overflow:hidden}
</style>"""


def slide_html(spec, script, i):
    s = script["slides"][i]
    n = len(script["slides"])
    pos = {c["role"]: c["position"] for c in s["characters"]}
    bubbles = "".join("<div class='bb %s' data-fit style='max-height:210px'>%s</div>" % ("r" if pos.get(d["speaker"]) == "right" else "l", images.e(d["text"]))
                      for d in s["dialogue"])
    nar = "<div class='nar' data-fit style='max-height:140px'>%s</div>" % images.e(s["narration"]) if s["narration"] else ""
    cta = images.cta_html(spec) if i == n - 1 else ""
    body = "%s<div class='cart'><div class='bbs'>%s</div><div class='scn'>%s</div></div>%s%s" % (CSS.replace("__MU__", "#b4b4bc"), bubbles, scene_svg(s), nar, cta)
    return images.frame(spec, body, tag="%d / %d" % (i + 1, n), deco=False)


def render_script(script, brand, product, out_dir, cta="", progress=None):
    """Render the script exactly as written to slide_N.jpg files (1080x1350). One slide at a time, browser closed after each."""
    msg = images.ram_message()
    if msg:
        raise images.RenderError(msg)
    spec = images.build_spec("carousel", "highlight", brand, product, script.get("title") or "", cta=cta or "")
    w, h = spec["size"]
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    n = len(script["slides"])
    files = []
    from playwright.sync_api import sync_playwright
    for i in range(n):
        if progress:
            progress("drawing slide %d of %d" % (i + 1, n))
        hp = out_dir / ("_c%d.html" % i)
        hp.write_text(images.page_html(spec, slide_html(spec, script, i), w, h), encoding="utf-8")
        path = out_dir / ("slide_%d.jpg" % (i + 1))
        with images.LOCK:
            with sync_playwright() as p:
                br = p.chromium.launch()
                try:
                    pg = br.new_page(viewport={"width": w, "height": h})
                    pg.goto(images.uri(hp))
                    pg.wait_for_selector("body[data-ready='1']", timeout=30000)
                    png = pg.screenshot(type="png")
                finally:
                    br.close()
        hp.unlink()
        images._save_jpeg_from_png(png, str(path))
        if not path.exists() or path.stat().st_size < 2000:
            raise images.RenderError("Slide %d came out empty." % (i + 1))
        files.append(str(path))
    return files
