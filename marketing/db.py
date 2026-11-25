"""Marketing tables (brands, products, posts). Migration-safe: every call may be re-run."""
import re, pathlib, json, datetime

ROOT = pathlib.Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
OUT_ROOT = pathlib.Path(r"D:\vault\outputs\marketing")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")

SCHEMA = """
CREATE TABLE IF NOT EXISTS brands(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, logo_path TEXT DEFAULT '', accent TEXT DEFAULT '#f5a524',
  bg TEXT DEFAULT '#0b0b0c', footer_left TEXT DEFAULT '', footer_right TEXT DEFAULT '', ig_handle TEXT DEFAULT '', notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT, brand_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '',
  key_points TEXT DEFAULT '', price_note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY AUTOINCREMENT, brand_id INTEGER, product_id INTEGER, platforms TEXT DEFAULT 'instagram',
  type TEXT DEFAULT 'post', topic TEXT DEFAULT '', caption TEXT DEFAULT '', hashtags TEXT DEFAULT '', description_alt TEXT DEFAULT '',
  image_paths TEXT DEFAULT '[]', status TEXT DEFAULT 'draft', scheduled_at TEXT DEFAULT '', ig_media_id TEXT DEFAULT '', ig_permalink TEXT DEFAULT '',
  error TEXT DEFAULT '', created_at TEXT DEFAULT '', approved_at TEXT DEFAULT '', published_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS imagegen_usage(day TEXT NOT NULL, provider TEXT NOT NULL, n INTEGER DEFAULT 0, neurons INTEGER DEFAULT 0, PRIMARY KEY(day, provider));
CREATE TABLE IF NOT EXISTS cast_members(id INTEGER PRIMARY KEY AUTOINCREMENT, brand_id INTEGER NOT NULL, role TEXT NOT NULL, name TEXT DEFAULT '',
  description TEXT DEFAULT '', seed INTEGER DEFAULT 1, UNIQUE(brand_id, role));
CREATE TABLE IF NOT EXISTS screenshots(id INTEGER PRIMARY KEY AUTOINCREMENT, brand_id INTEGER NOT NULL, product_id INTEGER NOT NULL, path TEXT NOT NULL,
  title TEXT DEFAULT '', note TEXT NOT NULL, width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, created_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS characters(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT DEFAULT '', master TEXT DEFAULT '',
  source TEXT DEFAULT '', created_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS img_queue(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, spec TEXT NOT NULL, status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT '', done_at TEXT DEFAULT '', error TEXT DEFAULT '');
"""
EXTRA_COLS = {"posts": [("template", "TEXT DEFAULT ''"), ("spec_json", "TEXT DEFAULT '{}'")], "imagegen_usage": [("neurons", "INTEGER DEFAULT 0")],
              "cast_members": [("char_id", "INTEGER DEFAULT 0")]}

# Demo data only: text comes from the owner's own pitch lines. Edit or delete in the Brand kit page.
SEED = [
    ("SFL", "#f5a524", "#0b0b0c", "SystemFriendly Labs", "Demo brand", "", "Demo brand. Replace with real details in the Brand kit.",
     [("InventoryCloud", "Inventory software that helps a business keep track of stock, purchases and sales in one place.",
       "Stock in one place\nPurchases and sales together\nBuilt for small businesses", ""),
      ("HMS", "Hospital management software that helps a hospital manage patients, billing and reports in one place.",
       "Patients in one place\nBilling and reports\nBuilt for hospitals", ""),
      ("LIMS", "Laboratory software that helps a lab manage samples, billing and reports in one place.",
       "Samples tracked\nReports in one place\nBuilt for labs", "")]),
]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "brand"


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure(c):
    c.executescript(SCHEMA)
    for t, cols in EXTRA_COLS.items():
        have = {r[1] for r in c.execute("PRAGMA table_info(%s)" % t)}
        for name, decl in cols:
            if name not in have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (t, name, decl))
    if not c.execute("SELECT 1 FROM brands LIMIT 1").fetchone():
        for name, ac, bg, fl, fr, ig, notes, prods in SEED:
            cur = c.execute("INSERT INTO brands(name,accent,bg,footer_left,footer_right,ig_handle,notes) VALUES(?,?,?,?,?,?,?)",
                            (name, ac, bg, fl, fr, ig, notes))
            for p in prods:
                c.execute("INSERT INTO products(brand_id,name,description,key_points,price_note) VALUES(?,?,?,?,?)", (cur.lastrowid,) + p)
    from . import imagegen
    for b in c.execute("SELECT id FROM brands").fetchall():
        for role, nm, desc, seed in imagegen.DEFAULT_CAST:
            c.execute("INSERT OR IGNORE INTO cast_members(brand_id,role,name,description,seed) VALUES(?,?,?,?,?)", (b[0], role, nm, desc, seed))
    if not c.execute("SELECT 1 FROM characters LIMIT 1").fetchone():             # shared character library: the 3 defaults need masters generated
        for role, nm, desc, seed in imagegen.DEFAULT_CAST:
            c.execute("INSERT INTO characters(name,description,source,created_at) VALUES(?,?,?,?)", (nm, desc, "seed", now()))
    for role, nm, desc, seed in imagegen.DEFAULT_CAST:
        c.execute("UPDATE cast_members SET char_id=(SELECT id FROM characters WHERE name=? ORDER BY id LIMIT 1) WHERE role=? AND char_id=0", (nm, role))
    c.commit()
    for name, *_ in SEED:
        (ASSETS / slug(name) / "photos").mkdir(parents=True, exist_ok=True)


def brand_dir(brand):
    return ASSETS / slug(brand["name"])


def logo_file(brand):
    """Absolute path of the brand's logo if one was uploaded (or sits in the folder), else None."""
    p = (brand["logo_path"] or "").strip()
    if p:
        q = pathlib.Path(p)
        if not q.is_absolute():
            q = ROOT / p
        if q.exists():
            return q
    d = brand_dir(brand)
    for ext in IMG_EXT:
        q = d / ("logo" + ext)
        if q.exists():
            return q
    return None


def product_photo(brand, product):
    """Photo picked up automatically: assets/<brand>/photos/<product-slug>.<ext> (or any file starting with it)."""
    if not product:
        return None
    d = brand_dir(brand) / "photos"
    s = slug(product["name"])
    if d.exists():
        for f in sorted(d.iterdir()):
            if f.suffix.lower() in IMG_EXT and f.stem.lower().startswith(s):
                return f
    return None
