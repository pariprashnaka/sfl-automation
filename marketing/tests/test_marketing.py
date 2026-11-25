import json, os, sys, io, sqlite3, tempfile, types, unittest, pathlib, shutil
sys.path.insert(0, r"D:\vault\automation")
from flask import Flask
from marketing import db as mdb
from marketing import routes


def make_app(tmp):
    path = os.path.join(tmp, "t.db")
    m = types.SimpleNamespace()
    m.app = Flask(__name__)
    m.HEAD = "<html><body><main>"
    m.settings = {}

    def conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c
    m.conn = conn
    m.qset = lambda k, d: m.settings.get(k, d)
    m.qset_save = lambda k, v: m.settings.__setitem__(k, str(v))
    m.env_val = lambda k: ""
    m.RUNNING = set()
    routes.register(m)
    return m


class Stage1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_assets = mdb.ASSETS
        mdb.ASSETS = pathlib.Path(self.tmp) / "assets"
        self.m = make_app(self.tmp)
        self.cl = self.m.app.test_client()

    def tearDown(self):
        mdb.ASSETS = self.old_assets
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migration_rerun_and_seed(self):
        c = self.m.conn()
        mdb.ensure(c)
        mdb.ensure(c)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM brands").fetchone()[0], 2)
        self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM products").fetchone()[0], 4)
        cols = {r[1] for r in c.execute("PRAGMA table_info(posts)")}
        self.assertTrue({"platforms", "ig_media_id", "ig_permalink", "published_at"} <= cols)

    def test_pages_ok(self):
        for u in ("/marketing", "/marketing/brands"):
            r = self.cl.get(u)
            self.assertEqual(r.status_code, 200, u)
        self.assertIn(b"Test connection", self.cl.get("/marketing").data)

    def test_logo_and_photo_picked_up(self):
        c = self.m.conn()
        b = c.execute("SELECT * FROM brands WHERE name='SFL'").fetchone()
        p = c.execute("SELECT * FROM products WHERE name='InventoryCloud'").fetchone()
        self.assertIsNone(mdb.logo_file(b))
        self.assertIsNone(mdb.product_photo(b, p))
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        r = self.cl.post("/marketing/brand/%d" % b["id"], data={"name": "SFL", "logo": (io.BytesIO(png), "x.png")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        b = c.execute("SELECT * FROM brands WHERE name='SFL'").fetchone()
        self.assertIsNotNone(mdb.logo_file(b))
        self.cl.post("/marketing/product/%d" % p["id"], data={"name": "InventoryCloud", "photo": (io.BytesIO(png), "p.png")}, content_type="multipart/form-data")
        self.assertIsNotNone(mdb.product_photo(b, p))

    def test_bad_upload_rejected(self):
        c = self.m.conn()
        b = c.execute("SELECT * FROM brands WHERE name='SFL'").fetchone()
        self.cl.post("/marketing/brand/%d" % b["id"], data={"name": "SFL", "logo": (io.BytesIO(b"x"), "evil.exe")}, content_type="multipart/form-data")
        self.assertIsNone(mdb.logo_file(c.execute("SELECT * FROM brands WHERE name='SFL'").fetchone()))


from marketing import images
from PIL import Image


class Stage2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (mdb.ASSETS, mdb.OUT_ROOT)
        mdb.ASSETS = pathlib.Path(self.tmp) / "assets"
        mdb.OUT_ROOT = pathlib.Path(self.tmp) / "out"
        self.m = make_app(self.tmp)
        c = self.m.conn()
        self.b = c.execute("SELECT * FROM brands WHERE name='SFL'").fetchone()
        self.p = c.execute("SELECT * FROM products WHERE name='InventoryCloud'").fetchone()

    def tearDown(self):
        mdb.ASSETS, mdb.OUT_ROOT = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_contrast(self):
        self.assertGreater(images.contrast_ratio("#ffffff", "#000000"), 20)
        self.assertEqual(images.best_text("#0b0b0c"), "#f4f4f5")
        self.assertEqual(images.best_text("#f5a524"), "#111111")
        self.assertEqual(images.readable("#222222", "#0b0b0c"), "#f4f4f5")      # unreadable colour is replaced
        self.assertEqual(images.readable("#f5a524", "#0b0b0c"), "#f5a524")      # amber on black is fine

    def test_spec_without_assets_is_text_only(self):
        s = images.build_spec("post", "highlight", self.b, self.p, "Stock at a glance")
        self.assertEqual(s["logo"], "")
        self.assertEqual(s["photo"], "")
        self.assertEqual(s["size"], (1080, 1350))
        self.assertIn("class='mark'", images.brandmark(s))               # text wordmark
        self.assertEqual(images.photo_html(s), "")

    def test_render_all_types_and_size_limit(self):
        for t, size, n in (("post", (1080, 1350), 1), ("story", (1080, 1920), 1), ("carousel", (1080, 1350), 5)):
            s = images.build_spec(t, None, self.b, self.p, "Know your stock", cta="Message us")
            r = images.render(s, mdb.OUT_ROOT / ("t_" + t))
            self.assertEqual(len(r["files"]), n if t != "carousel" else len(r["files"]))
            for f in r["files"]:
                self.assertLess(os.path.getsize(f), 8 * 1024 * 1024)
                self.assertEqual(Image.open(f).size, size)
                self.assertEqual(Image.open(f).format, "JPEG")

    def test_pillow_fallback(self):
        s = images.build_spec("post", "highlight", self.b, self.p, "Fallback layout")
        out = os.path.join(self.tmp, "fb.jpg")
        images._pillow_slide(s, out)
        self.assertEqual(Image.open(out).size, (1080, 1350))

    def test_ram_guard(self):
        old = images.free_ram_mb
        images.free_ram_mb = lambda: 300
        try:
            with self.assertRaises(images.RenderError):
                images.render(images.build_spec("post", None, self.b, self.p, "x"), mdb.OUT_ROOT / "t_ram")
        finally:
            images.free_ram_mb = old

    def test_generate_and_save_endpoints(self):
        cl = self.m.app.test_client()
        r = cl.post("/api/marketing/generate", json={"brand_id": self.b["id"], "product_id": self.p["id"], "type": "post", "topic": ""}).get_json()
        self.assertFalse(r["ok"])
        r = cl.post("/api/marketing/generate", json={"brand_id": self.b["id"], "product_id": self.p["id"], "type": "post", "topic": "Stock at a glance"}).get_json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(cl.get(r["images"][0]).status_code, 200)
        s = cl.post("/api/marketing/save", json={"token": r["token"], "brand_id": self.b["id"], "product_id": self.p["id"], "type": "post", "topic": "t", "caption": "c"}).get_json()
        self.assertTrue(s["ok"])
        row = self.m.conn().execute("SELECT * FROM posts WHERE id=?", (s["id"],)).fetchone()
        self.assertEqual(row["status"], "draft")
        self.assertEqual(len(json.loads(row["image_paths"])), 1)


if __name__ == "__main__":
    unittest.main()
