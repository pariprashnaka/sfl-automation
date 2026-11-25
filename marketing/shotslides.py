"""Screenshot slide types for the script editor: layouts, devices, highlight boxes, AI insertion into a story, captions with claim check.
Slide types: scene (cartoon), shot (1-4 screenshots in a frame), device (one screenshot inside a drawn device)."""
import json, re

TYPES = {"scene": "Cartoon scene", "shot": "Screenshot (full slide)", "device": "Screenshot on device"}
LAYOUTS = {"single": "Single large", "2up": "Side by side", "stack": "Stacked", "grid": "Grid 2x2"}
DEVICES = {"tablet": "Tablet", "laptop": "Laptop", "tv": "Big screen / TV", "phone": "Phone"}
BGS = {"gradient": "Brand gradient", "ai": "AI shop/office background"}
LAYOUT_COUNTS = {"single": (1, 1), "2up": (2, 2), "stack": (2, 4), "grid": (2, 4)}
MAX_SHOTS = {"shot": 4, "device": 1, "scene": 0}


def pick_layout(n, aspects=None):
    """Layout for n screenshots. aspects = width/height of each (optional): two portrait shots go side by side, two wide ones stack."""
    n = max(1, min(4, int(n or 1)))
    if n == 1:
        return "single"
    if n == 2:
        a = aspects or []
        return "stack" if a and all(x >= 1.3 for x in a[:2]) else "2up"
    return "grid" if n == 4 else "stack"


def valid_layout(layout, n, aspects=None):
    lo, hi = LAYOUT_COUNTS.get(layout, (0, 0))
    return layout if lo <= n <= hi else pick_layout(n, aspects)


def pick_device(aspect):
    if not aspect:
        return "tablet"
    return "phone" if aspect < 0.8 else ("tv" if aspect > 1.7 else ("laptop" if aspect > 1.4 else "tablet"))


def clean_hl(h):
    if not isinstance(h, dict):
        return None
    try:
        x, y, w, hh = (min(1.0, max(0.0, float(h.get(k)))) for k in ("x", "y", "w", "h"))
    except (TypeError, ValueError):
        return None
    w, hh = min(w, 1 - x), min(hh, 1 - y)
    if w < 0.02 or hh < 0.02:
        return None
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(hh, 4), "arrow": bool(h.get("arrow"))}


def clean_shots(raw, typ):
    out = []
    for s in (raw or [])[:MAX_SHOTS.get(typ, 0)]:
        sid = s.get("id") if isinstance(s, dict) else s
        if str(sid).isdigit() and int(sid) not in [o["id"] for o in out]:
            out.append({"id": int(sid), "hl": clean_hl(s.get("hl")) if isinstance(s, dict) else None})
    return out


def slide_fields(s, typ):
    """The screenshot-related fields of a cleaned slide."""
    shots = clean_shots(s.get("shots"), typ)
    return {"type": typ, "shots": shots, "layout": valid_layout(s.get("layout"), len(shots)) if typ == "shot" else "single",
            "device": s.get("device") if s.get("device") in DEVICES else "tablet", "bg": s.get("bg") if s.get("bg") in BGS else "gradient"}


def slide_notes(slide, notes):
    return "\n".join(notes.get(x["id"], "") for x in slide.get("shots", []) if notes)


# ---------------- AI: insert screenshot slides / write captions ----------------
def insert_prompt(script, shots, facts, language):
    outline = [{"slide": i + 1, "type": s.get("type", "scene"), "text": " / ".join([d["text"] for d in s["dialogue"]] + ([s["narration"]] if s["narration"] else []))[:140]}
               for i, s in enumerate(script["slides"])]
    lib = [{"id": k, "title": v.get("title", ""), "note": v["note"]} for k, v in shots.items()]
    return (
        "You place product screenshot slides inside a short cartoon story for an Instagram carousel.\n"
        "STORY SLIDES (numbered from 1):\n%s\n\nSCREENSHOTS TO ADD (id, title, note saying what each shows):\n%s\n\n"
        "PRODUCT FACTS (the only product claims you may state):\n%s\n\n"
        "Rules:\n- Use each screenshot at most once. You may put 1 to 4 screenshots on one slide (type \"shot\") or show one inside a device (type \"device\").\n"
        "- Place them at sensible points: usually right after the problem and the solution dialogue, never before slide 1 unless the story starts with it, "
        "and before a final call-to-action slide. \"after\" = the story slide number it follows (0 = at the very start).\n"
        "- For type shot choose layout: single | 2up | stack | grid (single for 1; 2up or stack for 2; stack for 3; grid for 4). "
        "For type device choose device: tablet | laptop | tv | phone (phone for portrait screenshots, tv or laptop for wide ones).\n"
        "- Write caption in %s: one short line (max 14 words) made ONLY from that screenshot's note and the product facts. No numbers, prices, guarantees or "
        "results that are not in the note or facts.\n"
        "Return ONLY JSON: {\"inserts\":[{\"after\":0,\"type\":\"shot\",\"shot_ids\":[1],\"layout\":\"single\",\"device\":\"\",\"caption\":\"\"}]}"
    ) % (json.dumps(outline, ensure_ascii=False), json.dumps(lib, ensure_ascii=False), facts or "(none)", language)


def caption_prompt(slide, shots, facts, language, instruction=""):
    lib = [{"title": v.get("title", ""), "note": v["note"]} for v in shots]
    return (
        "Write ONE short caption in %s (max 14 words) for a product screenshot slide. Use ONLY the screenshot notes and the product facts. "
        "No numbers, prices, guarantees or results that are not in them.\nSCREENSHOTS:\n%s\nPRODUCT FACTS:\n%s\nExtra instruction: %s\n"
        "Return ONLY JSON: {\"caption\":\"\"}" % (language, json.dumps(lib, ensure_ascii=False), facts or "(none)", instruction or "make it clear and simple"))


def plan_inserts(raw, ids, n_slides, has_cta_last, aspects, room):
    """Validate the AI answer. Returns a list of dicts (sorted by position) with safe values; unknown ids and reused screenshots are dropped."""
    used, out = set(), []
    limit = n_slides - (1 if has_cta_last else 0)
    for it in (raw.get("inserts") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict):
            continue
        sid = [int(x) for x in (it.get("shot_ids") or []) if str(x).isdigit() and int(x) in ids and int(x) not in used]
        typ = it.get("type") if it.get("type") in ("shot", "device") else "shot"
        sid = sid[:MAX_SHOTS[typ]]
        if not sid:
            continue
        used.update(sid)
        asp = [aspects.get(i) for i in sid if aspects.get(i)]
        d = {"after": max(0, min(limit, int(it["after"]) if str(it.get("after", "")).lstrip("-").isdigit() else limit)), "type": typ, "shots": [{"id": i, "hl": None} for i in sid],
             "caption": str(it.get("caption") or "").strip()}
        if typ == "shot":
            d["layout"] = valid_layout(it.get("layout"), len(sid), asp)
        else:
            d["device"] = it.get("device") if it.get("device") in DEVICES else pick_device(asp[0] if asp else None)
        out.append(d)
    for i in ids:                                   # screenshots the AI forgot: one slide each, just before the end
        if i not in used:
            out.append({"after": limit, "type": "shot", "shots": [{"id": i, "hl": None}], "caption": "", "layout": "single"})
    out.sort(key=lambda d: d["after"])
    return out[:max(0, room)]
