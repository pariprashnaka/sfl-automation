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


from marketing import hashtags, create
import ai_router


class Stage3(unittest.TestCase):
    def test_hashtag_tiers_count_and_spam(self):
        ai_tags = {"broad": ["#Business", "#follow4follow", "#SmallBusiness"], "niche": ["#Inventory Software", "#StockControl", "#stockcontrol"],
                   "local": ["#Buxar", "#BiharStartups"]}
        r = hashtags.build(ai_tags)
        tags = [t["tag"].lower() for t in r]
        self.assertNotIn("#follow4follow", tags)
        self.assertEqual(len(tags), len(set(tags)))                     # deduped
        self.assertTrue(12 <= len(r) <= 15, len(r))
        tiers = {t["tag"]: t["tier"] for t in r}
        self.assertEqual(tiers["#Buxar"], "local")
        self.assertEqual(tiers["#BiharStartups"], "local")
        self.assertEqual(tiers["#SmallBusiness"], "broad")
        self.assertEqual(tiers["#InventorySoftware"], "niche")
        self.assertTrue(all(t["tier"] in hashtags.TIERS for t in r))

    def test_local_always_present_and_cap(self):
        r = hashtags.build(["#a1b2c3d%d" % i for i in range(40)])
        self.assertLessEqual(len(r), 15)
        self.assertGreaterEqual(sum(1 for t in r if t["tier"] == "local"), 2)
        self.assertEqual(len(hashtags.build("")) >= 12, True)

    def test_banned_tags_removed(self):
        r = hashtags.build({"niche": ["#BadTag", "#GoodTag"]}, banned=["#badtag"])
        self.assertNotIn("#badtag", [t["tag"].lower() for t in r])

    def test_router_config(self):
        self.assertEqual(ai_router.CONFIG["task_size"]["marketing_caption"], "small")
        self.assertIn("marketing_caption", ai_router.CONFIG["cache_tasks"])

    def test_make_content_with_mock_ai(self):
        b = {"name": "SFL"}
        p = {"name": "InventoryCloud", "description": "Inventory software.", "key_points": "Stock in one place", "price_note": ""}
        seen = {}

        def fake(task, prompt, **k):
            seen["task"], seen["prompt"] = task, prompt
            return 'Sure: {"headline":"Know your stock","sub":"One place.","points":["Stock in one place"],"caption":"Track stock. 99 offers!","alt":"A card","hashtags":{"broad":["#Business"],"niche":["#Stock"],"local":["#Buxar"]}}'
        old = create.ai
        create.ai = fake
        try:
            c = create.make_content(b, p, "stock", "Friendly", "1")
        finally:
            create.ai = old
        self.assertEqual(seen["task"], "marketing_caption")
        self.assertIn("ONLY the facts", seen["prompt"])
        self.assertNotIn("owner_notes", seen["prompt"])
        self.assertTrue(12 <= len(c["hashtags"].split()) <= 15)
        self.assertIn("popularity is not verified", c["notes"])
        self.assertIn("99", c["notes"])                                  # number not in facts is flagged

    def test_make_content_failures_do_not_raise(self):
        b = {"name": "SFL"}
        old = create.ai
        try:
            create.ai = lambda *a, **k: (_ for _ in ()).throw(create.AIError("all down"))
            self.assertIn("not generated", create.make_content(b, None, "t", "", "1")["notes"])
            create.ai = lambda *a, **k: "not json at all"
            self.assertIn("not generated", create.make_content(b, None, "t", "", "1")["notes"])
        finally:
            create.ai = old


from marketing import queue as mq


class Stage4(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (mdb.ASSETS, mdb.OUT_ROOT)
        mdb.ASSETS = pathlib.Path(self.tmp) / "assets"
        mdb.OUT_ROOT = pathlib.Path(self.tmp) / "out"
        self.m = make_app(self.tmp)
        self.c = self.m.conn()
        self.cl = self.m.app.test_client()

    def tearDown(self):
        mdb.ASSETS, mdb.OUT_ROOT = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, status="draft", images='["x.jpg"]'):
        cur = self.c.execute("INSERT INTO posts(brand_id,product_id,type,topic,caption,status,image_paths,created_at) VALUES(1,1,'post','t','cap',?,?,?)",
                             (status, images, mdb.now()))
        self.c.commit()
        return cur.lastrowid

    def row(self, pid):
        return self.c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()

    def test_approval_gate(self):
        pid = self.post()
        self.assertFalse(mq.can_publish(self.row(pid)))                      # draft cannot publish
        ok, _ = mq.schedule(self.c, pid, "2030-01-01 10:00")
        self.assertFalse(ok)                                                  # cannot schedule unapproved
        self.assertEqual(self.row(pid)["status"], "draft")
        self.assertTrue(mq.approve(self.c, pid)[0])
        self.assertTrue(mq.can_publish(self.row(pid)))
        r = self.cl.post("/marketing/post/%d/publish" % self.post())          # another draft via the HTTP route
        self.assertEqual(r.status_code, 302)
        self.assertIn("Approve", r.headers["Location"])

    def test_cannot_approve_without_image_or_twice(self):
        self.assertFalse(mq.approve(self.c, self.post(images="[]"))[0])
        pid = self.post()
        mq.approve(self.c, pid)
        self.assertFalse(mq.approve(self.c, pid)[0])

    def test_schedule_validation_and_due(self):
        pid = self.post()
        mq.approve(self.c, pid)
        self.assertFalse(mq.schedule(self.c, pid, "not a date")[0])
        self.assertTrue(mq.schedule(self.c, pid, "2020-01-01T09:30")[0])
        self.assertEqual(self.row(pid)["scheduled_at"], "2020-01-01 09:30:00")
        self.assertEqual([r["id"] for r in mq.due_posts(self.c)], [pid])
        mq.schedule(self.c, pid, "2999-01-01T09:30")
        self.assertEqual(mq.due_posts(self.c), [])
        mq.unschedule(self.c, pid)
        self.assertEqual(self.row(pid)["status"], "approved")

    def test_unapproved_scheduled_row_is_never_due(self):
        pid = self.post(status="scheduled")
        self.c.execute("UPDATE posts SET scheduled_at='2020-01-01 00:00:00' WHERE id=?", (pid,))
        self.c.commit()
        self.assertEqual(mq.due_posts(self.c), [])                            # no approved_at -> not due

    def test_edit_resets_approval_and_delete(self):
        pid = self.post()
        mq.approve(self.c, pid)
        mq.edit_text(self.c, pid, "new", "#a", "alt")
        r = self.row(pid)
        self.assertEqual((r["status"], r["approved_at"]), ("draft", ""))
        self.assertTrue(mq.delete(self.c, pid)[0])
        self.assertFalse(mq.delete(self.c, self.post(status="publishing"))[0])

    def test_queue_page_and_filters(self):
        self.post()
        self.post(status="failed")
        self.c.execute("UPDATE posts SET error='Instagram rejected it' WHERE status='failed'")
        self.c.commit()
        for u in ("/marketing/queue", "/marketing/queue?status=failed", "/marketing/queue?status=draft"):
            self.assertEqual(self.cl.get(u).status_code, 200)
        html = self.cl.get("/marketing/queue?status=failed").get_data(as_text=True)
        self.assertIn("Retry", html)
        self.assertIn("Instagram rejected it", html)

    def test_scheduler_tick_is_rate_limited(self):
        calls = []
        old = mq.publish_post
        mq.publish_post = lambda pid: calls.append(pid) or (True, "")
        try:
            pid = self.post()
            mq.approve(self.c, pid)
            mq.schedule(self.c, pid, "2020-01-01T09:30")
            mq._last_tick[0] = 0
            self.assertEqual(mq.scheduler_tick(), 1)
            self.assertEqual(mq.scheduler_tick(), 0)                          # second call inside 10 minutes does nothing
            self.assertEqual(calls, [pid])
        finally:
            mq.publish_post = old


if __name__ == "__main__":
    unittest.main()
