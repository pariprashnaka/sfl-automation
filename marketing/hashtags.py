"""Hashtag tiers (broad / niche / local). Tags are AI-suggested only; popularity is NOT verified anywhere."""
import re

SPAM = {"follow4follow", "followforfollow", "f4f", "l4l", "like4like", "likeforlike", "followback", "followme", "instalike", "instafollow",
        "tagsforlikes", "likesforlikes", "spamfollow", "sfs", "s4s", "shoutout", "followtrain", "gainwithxtiandrohan", "teamfollowback"}
LOCAL_KEYS = ("buxar", "bihar", "patna", "gaya", "muzaffarpur", "bhagalpur", "arrah", "bhojpur", "darbhanga", "purnia")
BROAD = {"business", "smallbusiness", "india", "madeinindia", "software", "technology", "tech", "startup", "entrepreneur", "digitalindia",
         "businessgrowth", "management", "productivity", "msme", "indianbusiness", "cloud", "saas"}
DEFAULT_LOCAL = ["#Buxar", "#Bihar", "#BiharBusiness"]
DEFAULT_BROAD = ["#SmallBusiness", "#MadeInIndia", "#BusinessGrowth", "#MSME", "#DigitalIndia"]
DEFAULT_NICHE = ["#InventoryManagement", "#StockControl", "#BusinessTools", "#CloudSoftware", "#InventorySoftware"]
MIN_TAGS, MAX_TAGS = 12, 15
TIERS = ("broad", "niche", "local")


def norm(tag):
    """'#Small Business!' -> '#SmallBusiness' (letters and digits only). Returns '' if nothing is left."""
    t = re.sub(r"[^0-9A-Za-z_]", "", str(tag or "").replace("#", ""))
    return ("#" + t) if len(t) >= 3 else ""


def tier_of(tag, hint=""):
    k = tag.lstrip("#").lower()
    if any(x in k for x in LOCAL_KEYS):
        return "local"
    if hint in ("broad", "niche") and k not in BROAD:
        return hint
    return "broad" if k in BROAD else (hint if hint in TIERS else "niche")


def parse_ai(data):
    """Accepts {"broad":[..],"niche":[..],"local":[..]} or a flat list/string. Returns [(tag, hint)] in order."""
    out = []
    if isinstance(data, dict):
        for h in TIERS:
            for t in data.get(h) or []:
                out.append((t, h))
    elif isinstance(data, (list, tuple)):
        out = [(t, "") for t in data]
    elif isinstance(data, str):
        out = [(t, "") for t in re.findall(r"#?\w{3,}", data)]
    return out


def build(data, banned=()):
    """Return [{"tag": "#X", "tier": "broad|niche|local"}] - deduped, spam removed, 12 to 15 tags, at least 2 local."""
    ban = {b.lstrip("#").lower() for b in banned} | SPAM
    seen, res = set(), []

    def add(raw, hint=""):
        t = norm(raw)
        k = t.lstrip("#").lower()
        if not t or k in seen or k in ban:
            return
        seen.add(k)
        res.append({"tag": t, "tier": tier_of(t, hint)})
    for raw, hint in parse_ai(data):
        add(raw, hint)
    res = res[:MAX_TAGS]
    for t in DEFAULT_LOCAL:                                   # local tier is always represented
        if sum(1 for r in res if r["tier"] == "local") >= 2:
            break
        if len(res) >= MAX_TAGS:
            for i in range(len(res) - 1, -1, -1):            # make room by dropping the last non-local tag
                if res[i]["tier"] != "local":
                    res.pop(i)
                    break
        add(t, "local")
    for pool in (DEFAULT_NICHE, DEFAULT_BROAD, DEFAULT_LOCAL):  # pad up to the minimum
        for t in pool:
            if len(res) >= MIN_TAGS:
                break
            add(t)
    return res[:MAX_TAGS]


def as_text(tags):
    return " ".join(t["tag"] for t in tags)


def tier_map(tags):
    return {t["tag"].lower(): t["tier"] for t in tags}
