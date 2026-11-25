import os, sys, io, sqlite3, tempfile, types, unittest, pathlib, shutil
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


if __name__ == "__main__":
    unittest.main()
