"""Picture checks that run on this CPU-only laptop (no GPU, 8 GB RAM): face/head regions (OpenCV), identity against the locked master (small CLIP
ViT-B/32 image model in ONNX, loaded lazily from D:\\vault\\models), and a quick 'is there writing in the background' check.
All three are best-effort filters, not guarantees. Missing OpenCV or model => the check is skipped and the UI says so."""
import pathlib
import numpy as np
from PIL import Image

MODEL_DIR = pathlib.Path(r"D:\vault\models\clip")
MODEL_FILE = "vision_model_quantized.onnx"
W, H = 1080, 1350
SLOT_X = {"left": 0.27, "center": 0.5, "right": 0.73}
IDENTITY_MIN = 0.45          # deliberately low: with a PHOTO master, same-person and other-person scores overlap (0.56 vs 0.50-0.65). Raise it on the Characters page once masters are caricatures.
_sess = [None, None]         # session, error text
_cv = [None]


def cv():
    if _cv[0] is None:
        try:
            import cv2
            _cv[0] = cv2
        except Exception:
            _cv[0] = False
    return _cv[0] or None


# ---------------- face / head regions ----------------
def estimate_head(position, svg=False):
    """Where a head probably is when nothing is detected: from the character's left/centre/right slot (characters sit in the lower 65%).
    svg=True: the drawn-parts fallback, whose characters sit lower in the frame."""
    cx = SLOT_X.get(position, 0.5) * W
    if svg:
        return {"x": cx - 0.12 * W, "y": 0.57 * H, "r": cx + 0.12 * W, "b": 0.70 * H, "est": True}
    return {"x": cx - 0.13 * W, "y": 0.37 * H, "r": cx + 0.13 * W, "b": 0.56 * H, "est": True}


def detect_faces(img):
    """Haar frontal+profile detector on the slide picture. Returns [{x,y,r,b}] in slide coordinates (1080x1350). May miss caricature faces."""
    c = cv()
    if not c:
        return []
    im = np.array(img.convert("L").resize((W, H)))
    out = []
    for name, nb in (("haarcascade_frontalface_default.xml", 4), ("haarcascade_profileface.xml", 5)):
        det = c.CascadeClassifier(c.data.haarcascades + name)
        for (x, y, w, h) in det.detectMultiScale(im, scaleFactor=1.1, minNeighbors=nb, minSize=(70, 70)):
            if not any(abs((x + w / 2) - (o["x"] + o["r"]) / 2) < w * 0.6 and abs((y + h / 2) - (o["y"] + o["b"]) / 2) < h * 0.6 for o in out):
                out.append({"x": float(x), "y": float(y), "r": float(x + w), "b": float(y + h), "est": False})
    return out


def head_boxes(img, characters, svg=False):
    """One head box per character in the slide: a detected face near the character's slot, else the estimate from the slot."""
    faces = detect_faces(img) if img is not None else []
    boxes = []
    for ch in characters:
        est = estimate_head(ch.get("position"), svg)
        ecx = (est["x"] + est["r"]) / 2
        near = [f for f in faces if abs((f["x"] + f["r"]) / 2 - ecx) < 0.2 * W and f["b"] - f["y"] < 0.4 * H]
        if near:
            f = min(near, key=lambda f: abs((f["x"] + f["r"]) / 2 - ecx))
            pad = (f["r"] - f["x"]) * 0.25                     # hair and chin
            boxes.append({"x": f["x"] - pad, "y": f["y"] - pad * 1.6, "r": f["r"] + pad, "b": f["b"] + pad * 0.6, "est": False})
        else:
            boxes.append(est)
    return boxes


# ---------------- identity (CLIP) ----------------
def _session():
    if _sess[0] is None and _sess[1] is None:
        f = MODEL_DIR / MODEL_FILE
        try:
            import onnxruntime as ort
            if not f.is_file():
                raise FileNotFoundError("model file not found: %s" % f)
            so = ort.SessionOptions()
            so.intra_op_num_threads = 2                         # keep the laptop responsive
            _sess[0] = ort.InferenceSession(str(f), so, providers=["CPUExecutionProvider"])
        except Exception as ex:
            _sess[1] = str(ex)[:160]
    return _sess[0]


def available():
    """(ok, message) for the identity check."""
    if not cv():
        return False, "OpenCV is not installed"
    if _session() is None:
        return False, "identity model not available (%s)" % _sess[1]
    return True, ""


MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype="float32")
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype="float32")


def embed(img):
    """512-d unit vector from the CLIP image model; the image is centre-padded to a square, resized to 224."""
    s = _session()
    if s is None:
        raise RuntimeError("identity model not available: %s" % _sess[1])
    im = img.convert("RGB")
    side = max(im.size)
    sq = Image.new("RGB", (side, side), (255, 255, 255))
    sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    a = (np.asarray(sq.resize((224, 224), Image.BICUBIC), dtype="float32") / 255.0 - MEAN) / STD
    v = s.run(None, {"pixel_values": a.transpose(2, 0, 1)[None]})[0][0]
    return v / (np.linalg.norm(v) + 1e-9)


def person_crop(img, head):
    """Crop around a head box: head plus shoulders, the same framing as a master's upper-body portrait."""
    w, h = head["r"] - head["x"], head["b"] - head["y"]
    cx = (head["x"] + head["r"]) / 2
    half = max(w, h) * 1.5
    box = (int(max(0, cx - half)), int(max(0, head["y"] - h * 0.5)), int(min(img.width, cx + half)), int(min(img.height, head["y"] + h * 2.4)))
    return img.crop(box)


def master_embedding(master_path):
    im = Image.open(master_path).convert("RGB")
    heads = detect_faces(im.resize((W, H)))
    if heads:
        sx, sy = im.width / W, im.height / H
        f = max(heads, key=lambda f: (f["r"] - f["x"]) * (f["b"] - f["y"]))
        im = person_crop(im, {"x": f["x"] * sx, "y": f["y"] * sy, "r": f["r"] * sx, "b": f["b"] * sy})
    return embed(im)


def identity_scores(img, characters, masters):
    """Similarity (cosine, 0-1 scale) of each character's region in the picture with that character's master. masters: list of paths (same order).
    Returns a list of floats, or None if the check is unavailable."""
    ok, _ = available()
    if not ok:
        return None
    img = img.convert("RGB").resize((W, H))
    boxes = head_boxes(img, characters)
    out = []
    for box, m in zip(boxes, masters):
        out.append(round(float(np.dot(embed(person_crop(img, box)), master_embedding(m))), 3))
    return out


# ---------------- writing in the background ----------------
def text_regions(img, skip=()):
    """Count text-like LINES outside the `skip` boxes: runs of 4+ glyph-sized dark blobs of similar height standing on one row.
    Blank signs, paper and screens should give 0. Heuristic only."""
    c = cv()
    if not c:
        return 0
    g = np.array(img.convert("L").resize((W, H)))
    for b in skip:
        g[int(max(0, b["y"])):int(b["b"]), int(max(0, b["x"])):int(b["r"])] = 128
    th = c.adaptiveThreshold(g, 255, c.ADAPTIVE_THRESH_GAUSSIAN_C, c.THRESH_BINARY_INV, 31, 12)
    n, _, st, _ = c.connectedComponentsWithStats(th, 8)
    blobs = []
    for k in range(1, n):
        x, y, w, h, area = st[k]
        if 12 <= h <= 70 and 5 <= w <= 70 and 0.18 <= area / float(w * h) <= 0.9:
            blobs.append((x, y, w, h))
    blobs.sort(key=lambda b: b[0])
    used, lines = set(), 0
    for i, a in enumerate(blobs):
        if i in used:
            continue
        run, last = [i], a
        for k in range(i + 1, len(blobs)):
            b = blobs[k]
            if k in used:
                continue
            same_row = abs((b[1] + b[3] / 2) - (last[1] + last[3] / 2)) < 0.5 * last[3] and 0.65 <= b[3] / float(last[3]) <= 1.55
            if same_row and 0 <= b[0] - (last[0] + last[2]) < 1.6 * last[3]:
                run.append(k)
                last = b
        if len(run) >= 4:
            lines += 1
            used.update(run)
    return lines


def suggest_threshold(same_scores, other_scores):
    """Calibration helper: scores of pictures that ARE the character and of pictures that are other people.
    Returns (threshold, note). The threshold is the midpoint of the gap; if the groups overlap there is no safe value."""
    if not same_scores or not other_scores:
        return None, "need pictures of the character and of other people"
    lo, hi = min(same_scores), max(other_scores)
    if lo > hi:
        return round((lo + hi) / 2, 3), "groups are separable"
    return None, "scores overlap (same person min %.2f, other people max %.2f): the check cannot separate them for this master" % (lo, hi)
