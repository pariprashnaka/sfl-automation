import datetime, json, os, sys, io, sqlite3, tempfile, types, unittest, pathlib, shutil
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
        self.assertEqual(c.execute("SELECT COUNT(*) FROM brands").fetchone()[0], 1)
        self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM products").fetchone()[0], 3)
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
        self._ram = images.free_ram_mb
        images.free_ram_mb = lambda: 4000            # tests must not depend on how much RAM this laptop has free right now
        self.m = make_app(self.tmp)
        c = self.m.conn()
        self.b = c.execute("SELECT * FROM brands WHERE name='SFL'").fetchone()
        self.p = c.execute("SELECT * FROM products WHERE name='InventoryCloud'").fetchone()

    def tearDown(self):
        images.free_ram_mb = self._ram
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


import hashlib
from marketing import ig_publish as ig


class Resp:
    def __init__(self, status=200, j=None):
        self.status_code, self._j = status, j if j is not None else {}

    def json(self):
        return self._j


class FakeHttp:
    """Scripted Cloudinary + Graph API. Records (method, url, params) and never touches the network."""
    def __init__(self, statuses=("FINISHED",), fail=None, quota=0):
        self.calls, self.statuses, self.fail, self.quota, self.n = [], list(statuses), fail, quota, 0

    def _rec(self, m, url, params):
        self.calls.append((m, url, dict(params or {})))

    def names(self):
        return [c[1].split("/")[-1] if "graph" not in c[1] else "/".join(c[1].split("/")[4:]) for c in self.calls]

    def post(self, url, data=None, files=None, timeout=None):
        self._rec("POST", url, data)
        if "cloudinary" in url:
            if url.endswith("/upload"):
                self.n += 1
                return Resp(200, {"secure_url": "https://res.cloudinary.com/c/image/upload/v1/mkt_%d.jpg" % self.n, "public_id": "mkt_%d" % self.n})
            return Resp(200, {"result": "ok"})
        if self.fail == "media" and url.endswith("/media"):
            return Resp(400, {"error": {"message": "Media upload has failed", "code": 9004}})
        if url.endswith("/media_publish"):
            return Resp(200, {"id": "MEDIA1"})
        if url.endswith("/media"):
            self.n += 1
            return Resp(200, {"id": "CONT%d" % self.n})
        return Resp(404, {})

    def get(self, url, params=None, timeout=None):
        self._rec("GET", url, params)
        if url.endswith("content_publishing_limit"):
            return Resp(200, {"data": [{"quota_usage": self.quota}]})
        if self.fail == "auth":
            return Resp(400, {"error": {"message": "Invalid OAuth access token.", "code": 190}})
        if params and params.get("fields") == "status_code":
            return Resp(200, {"status_code": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]})
        if params and params.get("fields") == "permalink":
            return Resp(200, {"permalink": "https://instagram.com/p/XYZ/"})
        return Resp(200, {"username": "demo_user"})


ENV = {"IG_USER_ID": "123", "IG_ACCESS_TOKEN": "TOKEN_SECRET", "CLOUDINARY_CLOUD_NAME": "c", "CLOUDINARY_API_KEY": "k", "CLOUDINARY_API_SECRET": "s3cret"}


class Stage5(unittest.TestCase):
    def setUp(self):
        self.cfg = ig.Config(ENV.get)
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "a.jpg")
        Image.new("RGB", (50, 50), "black").save(self.img)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_signature(self):
        sig = ig.sign({"timestamp": 1315060510, "public_id": "sample"}, "abcd")
        self.assertEqual(sig, hashlib.sha1(b"public_id=sample&timestamp=1315060510abcd").hexdigest())

    def test_host_and_test_connection(self):
        self.assertEqual(ig.IG_HOST, "https://graph.facebook.com/v21.0")
        h = FakeHttp()
        r = ig.test_connection(self.cfg, h)
        self.assertEqual(r, {"ok": True, "username": "demo_user"})
        m, url, params = h.calls[0]
        self.assertTrue(url.startswith("https://graph.facebook.com/v21.0/123"))
        self.assertEqual(params["fields"], "username")                   # never account_type
        self.assertNotIn("graph.instagram.com", url)

    def test_connection_errors_are_plain_and_hide_secrets(self):
        r = ig.test_connection(self.cfg, FakeHttp(fail="auth"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "auth")
        self.assertNotIn("TOKEN_SECRET", r["error"])
        r = ig.test_connection(ig.Config({}.get), FakeHttp())
        self.assertIn("Missing settings", r["error"])

    def test_error_mapping(self):
        self.assertEqual(ig.map_error(400, {"error": {"code": 10, "message": "x"}}).kind, "permission")
        self.assertIn("app review", str(ig.map_error(400, {"error": {"code": 10, "message": "x"}})))
        self.assertEqual(ig.map_error(400, {"error": {"code": 4, "message": "x"}}).kind, "limit")
        self.assertEqual(ig.map_error(400, {"error": {"code": 190, "message": "x"}}).kind, "auth")
        self.assertEqual(ig.map_error(400, {"error": {"code": 9004, "message": "bad image"}}).kind, "container")

    def test_single_post_flow_and_cleanup(self):
        h = FakeHttp(statuses=["IN_PROGRESS", "FINISHED"])
        waits = []
        res = ig.publish_images(self.cfg, [self.img], "post", "cap", "alt", dry_run=False, http=h, sleep=waits.append)
        self.assertEqual((res["media_id"], res["permalink"]), ("MEDIA1", "https://instagram.com/p/XYZ/"))
        self.assertEqual(waits, [60])                                    # polled once a minute
        posts = [c for c in h.calls if c[0] == "POST"]
        up = posts[0]
        self.assertTrue(up[1].endswith("/image/upload"))
        self.assertEqual(up[2]["signature"], ig.sign({"public_id": up[2]["public_id"], "timestamp": up[2]["timestamp"]}, "s3cret"))
        media = [c for c in posts if c[1].endswith("/media")][0]
        self.assertIn("/upload/f_jpg/", media[2]["image_url"])
        self.assertEqual((media[2]["caption"], media[2]["alt_text"]), ("cap", "alt"))
        self.assertTrue(any(c[1].endswith("/image/destroy") for c in posts))   # hosted image removed

    def test_dry_run_skips_media_publish_only(self):
        h = FakeHttp()
        res = ig.publish_images(self.cfg, [self.img], "post", "c", dry_run=True, http=h, sleep=lambda s: None)
        self.assertTrue(res["dry_run"])
        urls = [c[1] for c in h.calls]
        self.assertFalse(any(u.endswith("/media_publish") for u in urls))
        self.assertTrue(any(u.endswith("/image/upload") for u in urls))
        self.assertTrue(any(u.endswith("/media") for u in urls))
        self.assertTrue(any(u.endswith("/image/destroy") for u in urls))

    def test_story_and_carousel(self):
        h = FakeHttp()
        ig.publish_images(self.cfg, [self.img], "story", dry_run=True, http=h, sleep=lambda s: None)
        self.assertEqual([c[2]["media_type"] for c in h.calls if c[1].endswith("/media")], ["STORIES"])
        h = FakeHttp()
        ig.publish_images(self.cfg, [self.img, self.img, self.img], "carousel", "cap", dry_run=True, http=h, sleep=lambda s: None)
        media = [c[2] for c in h.calls if c[1].endswith("/media")]
        self.assertEqual([m.get("is_carousel_item") for m in media[:3]], ["true"] * 3)
        self.assertEqual(media[3]["media_type"], "CAROUSEL")
        self.assertEqual(len(media[3]["children"].split(",")), 3)
        with self.assertRaises(ig.IGError):
            ig.publish_images(self.cfg, [self.img], "carousel", http=FakeHttp())

    def test_poll_error_timeout_and_cleanup_on_failure(self):
        with self.assertRaises(ig.IGError) as cm:
            ig.wait_ready(self.cfg, "C1", FakeHttp(statuses=["ERROR"]), lambda s: None)
        self.assertEqual(cm.exception.kind, "container")
        waits = []
        with self.assertRaises(ig.IGError):
            ig.wait_ready(self.cfg, "C1", FakeHttp(statuses=["IN_PROGRESS"]), waits.append)
        self.assertEqual(len(waits), 4)                                  # 5 checks, 4 one-minute waits
        h = FakeHttp(fail="media")
        with self.assertRaises(ig.IGError):
            ig.publish_images(self.cfg, [self.img], "post", http=h, sleep=lambda s: None)
        self.assertTrue(any(c[1].endswith("/image/destroy") for c in h.calls))   # cleaned up even on failure


class Stage5Service(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (mdb.ASSETS, mdb.OUT_ROOT)
        mdb.ASSETS = pathlib.Path(self.tmp) / "assets"
        mdb.OUT_ROOT = pathlib.Path(self.tmp) / "out"
        self.m = make_app(self.tmp)
        self.m.env_val = ENV.get
        self.c = self.m.conn()
        self.img = os.path.join(self.tmp, "a.jpg")
        Image.new("RGB", (50, 50), "black").save(self.img)

    def tearDown(self):
        mdb.ASSETS, mdb.OUT_ROOT = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, status="approved", approved=True, published_ago_hours=None):
        cur = self.c.execute("INSERT INTO posts(brand_id,product_id,platforms,type,caption,hashtags,status,image_paths,created_at,approved_at,published_at) "
                             "VALUES(1,1,'instagram','post','cap','#a',?,?,?,?,?)",
                             (status, json.dumps([self.img]), mdb.now(), mdb.now() if approved else "",
                              (datetime.datetime.now() - datetime.timedelta(hours=published_ago_hours)).strftime("%Y-%m-%d %H:%M:%S") if published_ago_hours is not None else ""))
        self.c.commit()
        return cur.lastrowid

    def row(self, pid):
        return self.c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()

    def test_approval_gate_blocks_before_any_http(self):
        h = FakeHttp()
        ok, msg = ig.publish_post(self.m, self.post(status="draft", approved=False), h, lambda s: None)
        self.assertFalse(ok)
        self.assertEqual(h.calls, [])

    def test_dry_run_default_keeps_post_approved(self):
        h = FakeHttp()
        pid = self.post()
        ok, msg = ig.publish_post(self.m, pid, h, lambda s: None)
        self.assertTrue(ok, msg)
        self.assertIn("Nothing was published", msg)
        self.assertEqual(self.row(pid)["status"], "approved")
        self.assertFalse(any(c[1].endswith("/media_publish") for c in h.calls))

    def test_real_publish_updates_row(self):
        self.m.settings["mkt_dry_run"] = "0"
        pid = self.post()
        ok, msg = ig.publish_post(self.m, pid, FakeHttp(), lambda s: None)
        r = self.row(pid)
        self.assertTrue(ok, msg)
        self.assertEqual((r["status"], r["ig_media_id"], r["ig_permalink"]), ("published", "MEDIA1", "https://instagram.com/p/XYZ/"))
        self.assertTrue(r["published_at"])

    def test_rolling_24h_cap(self):
        self.m.settings["mkt_dry_run"] = "0"
        for _ in range(39):
            self.post(status="published", published_ago_hours=3)
        for _ in range(5):
            self.post(status="published", published_ago_hours=30)       # older than 24h: does not count
        pid = self.post()
        h = FakeHttp()
        self.assertTrue(ig.publish_post(self.m, pid, h, lambda s: None)[0])   # 39 + this one = 40 is allowed
        pid2 = self.post()
        h2 = FakeHttp()
        ok, msg = ig.publish_post(self.m, pid2, h2, lambda s: None)
        self.assertFalse(ok)
        self.assertIn("Daily limit", msg)
        self.assertEqual(h2.calls, [])                                   # blocked locally, no HTTP
        self.assertEqual(self.row(pid2)["status"], "approved")           # still waiting, not failed

    def test_instagram_side_quota_blocks(self):
        ok, msg = ig.publish_post(self.m, self.post(), FakeHttp(quota=40), lambda s: None)
        self.assertFalse(ok)
        self.assertIn("40 posts", msg)

    def test_failure_marks_failed_with_message_and_retry_gate(self):
        self.m.settings["mkt_dry_run"] = "0"
        pid = self.post()
        ok, msg = ig.publish_post(self.m, pid, FakeHttp(fail="media"), lambda s: None)
        r = self.row(pid)
        self.assertFalse(ok)
        self.assertEqual(r["status"], "failed")
        self.assertIn("could not process", r["error"])
        self.assertTrue(mq.can_publish(r))                               # a failed (previously approved) post can be retried

    def test_missing_settings(self):
        self.m.env_val = lambda k: ""
        ok, msg = ig.publish_post(self.m, self.post(), FakeHttp(), lambda s: None)
        self.assertFalse(ok)
        self.assertIn("Missing settings", msg)


from marketing import cartoon, story_routes
import time as _time

HINDI = "दुकानदार का माल ख़राब हो गया"


class Cartoon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (mdb.ASSETS, mdb.OUT_ROOT)
        mdb.ASSETS = pathlib.Path(self.tmp) / "assets"
        mdb.OUT_ROOT = pathlib.Path(self.tmp) / "out"
        self._ram = images.free_ram_mb
        images.free_ram_mb = lambda: 4000
        self.m = make_app(self.tmp)
        self.c = self.m.conn()
        self.b = self.c.execute("SELECT * FROM brands WHERE name='SFL'").fetchone()
        self.p = self.c.execute("SELECT * FROM products WHERE name='InventoryCloud'").fetchone()
        self.cl = self.m.app.test_client()
        self._ai = story_routes.ai

    def tearDown(self):
        story_routes.ai = self._ai
        images.free_ram_mb = self._ram
        mdb.ASSETS, mdb.OUT_ROOT = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self, text="Stock ka hisaab ek jagah"):
        return json.dumps({"title": "T", "caption": "cap", "alt": "alt", "hashtags": {"broad": ["#Business"], "niche": ["#Stock"], "local": ["#Buxar"]},
                           "slides": [{"scene": "shelves", "description": "d", "characters": [{"role": "shopkeeper", "expression": "worried", "position": "left"},
                                                                                              {"role": "customer", "expression": "happy", "position": "right"}],
                                       "props": ["boxes", "calendar", "question"], "dialogue": [{"speaker": "shopkeeper", "text": text}], "narration": "n"}] * 3})

    def run_job(self, jid):
        for _ in range(100):
            j = story_routes.JOBS[jid]
            if j["state"] != "running":
                return j
            _time.sleep(0.1)
        self.fail("job did not finish")

    def test_parts_combine_and_unknown_values_fall_back(self):
        s = cartoon.clean_slide({"scene": "moon", "characters": [{"role": "ghost", "expression": "??", "position": "up"}] * 3,
                                 "props": ["tablet", "nope", "phone", "boxes", "calendar", "coin"], "dialogue": [{"speaker": "x", "text": "hi"}] * 3})
        self.assertEqual(s["scene"], "shop_counter")
        self.assertEqual(len(s["characters"]), 2)
        self.assertEqual({c["position"] for c in s["characters"]}, {"left", "right"})
        self.assertEqual(len(s["props"]), 4)
        self.assertNotIn("nope", s["props"])
        self.assertEqual(len(s["dialogue"]), 2)
        svg = cartoon.scene_svg(s)
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))
        for sc in cartoon.SCENES:
            for ex in cartoon.EXPRESSIONS:
                for pr in cartoon.PROPS:
                    self.assertIn("<svg", cartoon.scene_svg({"scene": sc, "characters": [{"role": "customer", "expression": ex, "position": "left"}], "props": [pr], "dialogue": []}))

    def test_claim_note_for_claims_not_in_product(self):
        facts = cartoon.product_facts(self.p)
        ok = cartoon.clean_slide({"dialogue": [{"speaker": "shopkeeper", "text": "Stock in one place"}]})
        self.assertEqual(cartoon.claim_flags(ok, facts), "")
        for txt in ("Save 40% every month", "100% guaranteed", "Only 99 rupees"):
            bad = cartoon.clean_slide({"dialogue": [{"speaker": "shopkeeper", "text": txt}]})
            self.assertTrue(cartoon.claim_flags(bad, facts), txt)
        ai_flag = cartoon.clean_slide({"dialogue": [{"speaker": "shopkeeper", "text": "ok"}], "unsupported_claim": "expiry alerts"})
        self.assertTrue(cartoon.claim_flags(ai_flag, facts))
        s = cartoon.clean_script(json.loads(self.raw("Save 40% on stock")), 3, facts)
        self.assertTrue(all("claim not in product details" in x["claim_note"] for x in s["slides"]))

    def test_script_job_and_two_step_gate(self):
        story_routes.ai = lambda task, prompt, **k: (self.assertEqual(task, "marketing_story"), self.assertIn("PRODUCT FACTS", prompt),
                                                   self.assertIn("exactly 4 slides", prompt), self.raw())[3]
        r = self.cl.post("/api/marketing/story/script", json={"brand_id": self.b["id"], "product_id": self.p["id"], "brief": "do dukandaar baat kar rahe hain", "slides": 4, "language": "Hinglish"}).get_json()
        self.assertTrue(r["ok"])
        j = self.run_job(r["job"])
        self.assertEqual(j["state"], "done", j["error"])
        script = j["result"]
        self.assertTrue(script["token"])
        self.assertTrue(12 <= len(script["hashtags"].split()) <= 15)
        # empty brief refused; render without a server-issued script refused
        self.assertFalse(self.cl.post("/api/marketing/story/script", json={"brand_id": self.b["id"], "brief": " "}).get_json()["ok"])
        bad = self.cl.post("/api/marketing/story/render", json={"brand_id": self.b["id"], "script": {"slides": script["slides"], "token": "forged"}}).get_json()
        self.assertFalse(bad["ok"])
        self.assertFalse(self.cl.post("/api/marketing/story/render", json={"brand_id": self.b["id"], "script": {"slides": script["slides"]}}).get_json()["ok"])

    def test_bad_ai_answer_is_a_clear_error(self):
        story_routes.ai = lambda *a, **k: "not json"
        r = self.cl.post("/api/marketing/story/script", json={"brand_id": self.b["id"], "brief": "x"}).get_json()
        j = self.run_job(r["job"])
        self.assertEqual(j["state"], "error")
        self.assertIn("could not be read", j["error"])

    def test_regenerate_one_slide(self):
        story_routes.ai = lambda *a, **k: self.raw()
        script = self.run_job(self.cl.post("/api/marketing/story/script", json={"brand_id": self.b["id"], "product_id": self.p["id"], "brief": "x"}).get_json()["job"])["result"]
        story_routes.ai = lambda task, prompt, **k: json.dumps({"scene": "plain", "characters": [{"role": "customer", "expression": "surprised"}], "dialogue": [{"speaker": "customer", "text": "naya"}]})
        r = self.cl.post("/api/marketing/story/slide", json={"brand_id": self.b["id"], "product_id": self.p["id"], "script": script, "index": 1}).get_json()
        out = self.run_job(r["job"])["result"]
        self.assertEqual((out["index"], out["slide"]["scene"], out["slide"]["dialogue"][0]["text"]), (1, "plain", "naya"))

    def test_render_uses_script_as_written_hindi_and_progress(self):
        story_routes.ai = lambda *a, **k: self.raw()
        script = self.run_job(self.cl.post("/api/marketing/story/script", json={"brand_id": self.b["id"], "product_id": self.p["id"], "brief": "x", "slides": 3}).get_json()["job"])["result"]
        script["slides"][0]["dialogue"][0]["text"] = HINDI                        # the owner edits a line (Devanagari)
        r = self.cl.post("/api/marketing/story/render", json={"brand_id": self.b["id"], "product_id": self.p["id"], "script": script, "cta": "Call us"}).get_json()
        self.assertTrue(r["ok"], r)
        stages = set()
        for _ in range(600):
            j = story_routes.JOBS[r["job"]]
            stages.add(j["stage"])
            if j["state"] != "running":
                break
            _time.sleep(0.2)
        self.assertEqual(j["state"], "done", j["error"])
        self.assertTrue(any(s.startswith("drawing slide") for s in stages | {j["stage"]}))
        self.assertEqual(len(j["result"]["images"]), 4)                       # 3 story slides + the call-to-action slide
        self.assertTrue(j["result"]["added_cta"] and j["result"]["slides"][-1]["is_cta"])
        self.assertEqual(set(j["result"]["providers"]), {"svg"})                # no keys in tests: drawn parts
        tok = j["result"]["token"]
        for k in (1, 2, 3, 4):
            f = mdb.OUT_ROOT / ("p_" + tok) / ("slide_%d.jpg" % k)
            self.assertEqual(Image.open(f).size, (1080, 1350))
            self.assertLess(f.stat().st_size, 8 * 1024 * 1024)
        self.assertEqual(self.cl.get(j["result"]["images"][0]).status_code, 200)

    def test_create_page_has_cartoon_ui(self):
        h = self.cl.get("/marketing/create").get_data(as_text=True)
        for needle in ("Describe the story you want", "Load example", "Cartoon story carousel", "id='lb' hidden", ".lb[hidden]{display:none!important}"):
            self.assertIn(needle, h)
        self.assertNotIn("Expiry wastage", h)
        self.assertIn("do dukandaar baat kar rahe hain", h)                    # only inside the Load example text


class ImageGen(unittest.TestCase):
    """Providers with mocked HTTP: cloudflare, gemini (reference image), cap, retries and the SVG fallback."""
    PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 2000

    def setUp(self):
        from marketing import imagegen
        self.ig = imagegen
        self.tmp = tempfile.mkdtemp()
        self.old = (imagegen.CACHE_DIR, imagegen.http_post, imagegen.SLEEP)
        imagegen.CACHE_DIR = pathlib.Path(self.tmp) / "cache"
        imagegen.SLEEP = lambda s: None
        self.calls = []
        self.env = {"CF_ACCOUNT_ID": "acc1", "CF_API_TOKEN": "tok1", "GEMINI_API_KEY": "gk", "GEMINI_IMAGE_MODEL": "some-image-model"}
        self.settings = {}
        path = os.path.join(self.tmp, "t.db")
        c = sqlite3.connect(path)
        c.executescript(mdb.SCHEMA)
        c.commit()
        c.close()
        self.conn = lambda: sqlite3.connect(path)

    def tearDown(self):
        self.ig.CACHE_DIR, self.ig.http_post, self.ig.SLEEP = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env_val(self, k):
        return self.env.get(k, "")

    def gen(self, prompt="p", hq=False, **kw):
        return self.ig.generate_chain(prompt, 5, hq, self.env_val, self.conn, lambda k, d: self.settings.get(k, d), **kw)

    def used(self):
        c = self.conn()
        try:
            return self.ig.used_today(c)
        finally:
            c.close()

    def b64(self):
        import base64
        return base64.b64encode(self.PNG).decode()

    def test_cloudflare_request_shape_cache_and_usage(self):
        def post(url, headers, payload):
            self.calls.append((url, headers, payload))
            return {"result": {"image": self.b64()}}
        self.ig.http_post = post
        r = self.gen()
        self.assertEqual(r["provider"], "cloudflare")
        self.assertTrue(r["file"].exists())
        url, hdr, body = self.calls[0]
        self.assertEqual(url, "https://api.cloudflare.com/client/v4/accounts/acc1/ai/run/@cf/black-forest-labs/flux-1-schnell")
        self.assertEqual(hdr["Authorization"], "Bearer tok1")
        self.assertEqual((body["prompt"], body["steps"]), ("p", 6))
        self.assertEqual(self.used(), {"cloudflare": 1})
        r2 = self.gen()                                                   # same prompt+seed: cache, no call, no count
        self.assertTrue(r2["cached"])
        self.assertEqual((len(self.calls), self.used()), (1, {"cloudflare": 1}))

    def test_gemini_only_with_hq_model_from_env_and_reference(self):
        def post(url, headers, payload):
            self.calls.append((url, headers, payload))
            return {"candidates": [{"content": {"parts": [{"inlineData": {"mime_type": "image/png", "data": self.b64()}}]}}]}
        self.ig.http_post = post
        r = self.gen(hq=True, ref=self.PNG)
        self.assertEqual(r["provider"], "gemini")
        url, hdr, body = self.calls[0]
        self.assertIn("/models/some-image-model:generateContent", url)
        self.assertEqual(hdr["x-goog-api-key"], "gk")
        self.assertIn("inline_data", body["contents"][0]["parts"][0])     # reference image first
        self.calls.clear()
        self.gen(hq=False, prompt="q")                                    # switch off: gemini is never called
        self.assertTrue(all("generativelanguage" not in c[0] for c in self.calls))

    def test_gemini_unset_says_so_and_falls_back(self):
        self.env.pop("GEMINI_IMAGE_MODEL")
        self.ig.http_post = lambda *a: self.fail("no call expected for gemini")
        self.assertIn("GEMINI_IMAGE_MODEL", self.ig.status(self.env_val)["gemini_msg"])
        r = self.ig.generate("p", 1, "gemini", self.env_val, self.conn, lambda k, d: d)
        self.assertEqual((r["file"], r["provider"]), (None, "svg"))
        self.assertIn("GEMINI_IMAGE_MODEL", r["reason"])

    def test_missing_keys_quota_and_failure_fall_back_to_svg(self):
        self.ig.http_post = lambda *a: self.fail("no call expected")
        saved = dict(self.env)
        self.env.clear()
        r = self.gen()
        self.assertEqual((r["file"], r["provider"]), (None, "svg"))
        self.assertIn("CF_ACCOUNT_ID", r["reason"])
        self.env.update(saved)
        self.settings["mkt_neuron_cap"] = 0                                # quota hit
        r = self.gen()
        self.assertEqual(r["provider"], "svg")
        self.assertIn("daily limit", r["reason"])
        self.settings.clear()
        n = []

        def boom(*a):
            n.append(1)
            raise self.ig.Retryable("HTTP 503")
        self.ig.http_post = boom
        r = self.gen()
        self.assertEqual((r["file"], r["provider"], len(n)), (None, "svg", self.ig.RETRIES))   # retried, then SVG
        self.assertEqual(self.used(), {})

    def test_retry_then_success_and_hq_falls_back_to_cloudflare(self):
        ok = {"result": {"image": self.b64()}}
        seq = [self.ig.Retryable("HTTP 429"), ok]

        def post(url, h, p):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        self.ig.http_post = post
        self.assertEqual(self.gen()["provider"], "cloudflare")

        def post2(url, h, p):
            if "generativelanguage" in url:
                raise self.ig.ImageError("HTTP 403: no")
            return ok
        self.ig.http_post = post2
        r = self.gen(hq=True, prompt="other")
        self.assertEqual(r["provider"], "cloudflare")
        self.assertIn("gemini", r["note"])

    def test_prompts_have_no_text_rule_cast_and_no_labels(self):
        cast = [dict(role=r, name=n, description=d, seed=sd) for r, n, d, sd in self.ig.DEFAULT_CAST]
        slide = cartoon.clean_slide({"scene": "shop_counter", "description": "Shopkeeper2 shows a tablet to Customer, saying \"Namaste ji\"",
                                     "characters": [{"role": "shopkeeper2", "expression": "happy", "position": "left"}, {"role": "customer", "expression": "surprised", "position": "right"}],
                                     "dialogue": [{"speaker": "shopkeeper2", "text": "Shopkeeper2: नमस्ते"}], "props": ["tablet"]})
        prompt, seed = self.ig.scene_prompt(slide, cast, cartoon.SCENES)
        self.assertTrue(prompt.startswith(self.ig.STYLE))
        for w in ("No text", "no logos", "no watermarks", "Sunil", "Meena", "round glasses"):
            self.assertIn(w, prompt)
        self.assertNotIn("Shopkeeper2", prompt)
        self.assertNotIn("Namaste", prompt)
        self.assertNotIn("नमस्ते", prompt)
        self.assertEqual(seed, 2202)
        self.assertNotEqual(self.ig.seed_for(seed, 1), seed)

    def test_script_fixes_cta_language_and_labels(self):
        facts = "InventoryCloud\nStock in one place"
        d = {"title": "t", "slides": [{"description": "Shopkeeper2 smiles", "characters": [{"role": "shopkeeper", "position": "left"}],
                                         "dialogue": [{"speaker": "shopkeeper", "text": "Shopkeeper2: stock check"}], "narration": "Customer asks"}] * 3}
        s = cartoon.clean_script(d, 3, facts, "Hindi", "Call 99999")
        self.assertEqual(len(s["slides"]), 4)
        self.assertTrue(s["slides"][-1]["is_cta"])
        self.assertNotIn("hopkeeper2", json.dumps(s["slides"], ensure_ascii=False))
        self.assertIn("Devanagari", s["slides"][0]["claim_note"])
        self.assertEqual(cartoon.script_prompt("b", None, "X", "Hindi", 3, "", "Call 99999").count("Devanagari"), 1)
        s2 = cartoon.clean_script(d, 3, facts, "Hindi", "")              # no CTA field: nothing is added
        self.assertEqual(len(s2["slides"]), 3)


class Screenshots(unittest.TestCase):
    """Library upload rules, layout selection, caption claims, mocked AI insertion, zone layout check."""

    def setUp(self):
        from marketing import shots, shotslides, zones, story_routes as sr
        self.shots, self.ss, self.zones, self.sr = shots, shotslides, zones, sr
        self.tmp = tempfile.mkdtemp()
        self.old = (mdb.ASSETS, mdb.OUT_ROOT, sr.ai)
        mdb.ASSETS = pathlib.Path(self.tmp) / "assets"
        mdb.OUT_ROOT = pathlib.Path(self.tmp) / "out"
        self.m = make_app(self.tmp)
        self.cl = self.m.app.test_client()
        c = self.m.conn()
        mdb.ensure(c)
        self.b = c.execute("SELECT * FROM brands WHERE name='SFL'").fetchone()
        self.p = c.execute("SELECT * FROM products WHERE brand_id=?", (self.b["id"],)).fetchone()
        c.close()

    def tearDown(self):
        mdb.ASSETS, mdb.OUT_ROOT, self.sr.ai = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def png(self, w=300, h=200, fmt="PNG"):
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (30, 90, 200)).save(buf, fmt)
        return buf.getvalue()

    def upload(self, files, notes, confirm=True):
        data = {"shot": [(io.BytesIO(b), n) for n, b in files]}
        for k, note in enumerate(notes):
            data["note_%d" % k] = note
            data["title_%d" % k] = "T%d" % k
        if confirm:
            data["confirm"] = "1"
        return self.cl.post("/api/marketing/shots/%d" % self.p["id"], data=data, content_type="multipart/form-data").get_json()

    def count(self):
        c = self.m.conn()
        try:
            return c.execute("SELECT COUNT(*) FROM screenshots").fetchone()[0]
        finally:
            c.close()

    # ---- Job A ----
    def test_upload_requires_confirmation_note_and_valid_image(self):
        r = self.upload([("a.png", self.png())], ["Stock list"], confirm=False)
        self.assertFalse(r["ok"])
        self.assertIn("I confirm no real customer data", r["error"])
        self.assertFalse(self.upload([("a.png", self.png())], ["   "])["ok"])                        # note is required
        self.assertFalse(self.upload([("a.exe", self.png())], ["x"])["ok"])                          # wrong type
        self.assertFalse(self.upload([("a.png", b"not an image")], ["x"])["ok"])                     # not an image
        self.assertEqual(self.count(), 0)
        r = self.upload([("a.png", self.png()), ("b.png", self.png())], ["ok", ""])                  # one bad row: nothing saved
        self.assertFalse(r["ok"])
        self.assertEqual(self.count(), 0)
        self.assertIn("customer", self.shots.WARNING)

    def test_upload_saves_downscales_and_lists(self):
        r = self.upload([("big.png", self.png(3000, 1000)), ("p.jpg", self.png(400, 800, "JPEG")), ("w.webp", self.png(200, 100, "WEBP"))], ["Stock list", "Phone view", "Small"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.count(), 3)
        c = self.m.conn()
        rows = c.execute("SELECT * FROM screenshots ORDER BY id").fetchall()
        c.close()
        self.assertEqual(rows[0]["width"], 2400)                                                     # max 2400 px wide
        self.assertEqual(rows[1]["width"], 400)                                                      # never upscaled
        for row in rows:
            f = mdb.ASSETS / row["path"]
            self.assertTrue(f.is_file())
            self.assertIn("/screenshots/", f.as_posix())
            self.assertLessEqual(Image.open(f).width, 2400)
        lst = self.cl.get("/api/marketing/shots?product_id=%d" % self.p["id"]).get_json()
        self.assertEqual([s["note"] for s in lst["shots"]], ["Small", "Phone view", "Stock list"])
        self.assertEqual(self.cl.get(lst["shots"][0]["url"]).status_code, 200)
        self.assertEqual(self.cl.post("/api/marketing/shots/delete/%d" % rows[0]["id"]).get_json()["ok"], True)
        self.assertEqual(self.count(), 2)

    def test_brand_kit_and_create_pages_have_library_ui(self):
        h = self.cl.get("/marketing/brands").get_data(as_text=True)
        self.assertIn("shotbox", h)
        self.assertIn("I confirm no real customer data", h)
        self.assertIn("Use demo data only", h)
        h = self.cl.get("/marketing/create").get_data(as_text=True)
        for needle in ("Add screenshots to story", "Screenshot library", "Start screenshot slides", "Regenerate caption", "Screenshot on device", "Upload new..."):
            self.assertIn(needle, h)

    # ---- Job B ----
    def test_layout_selection(self):
        ss = self.ss
        self.assertEqual([ss.pick_layout(n) for n in (1, 2, 3, 4)], ["single", "2up", "stack", "grid"])
        self.assertEqual(ss.pick_layout(2, [1.8, 1.7]), "stack")                                      # two wide screenshots stack
        self.assertEqual(ss.pick_layout(2, [0.5, 0.6]), "2up")
        self.assertEqual(ss.valid_layout("grid", 1), "single")                                        # grid needs 2-4
        self.assertEqual(ss.valid_layout("single", 3), "stack")
        self.assertEqual(ss.valid_layout("2up", 2), "2up")
        self.assertEqual([ss.pick_device(a) for a in (0.5, 1.2, 1.5, 1.9, None)], ["phone", "tablet", "laptop", "tv", "tablet"])
        for lay, n in (("single", 1), ("2up", 2), ("stack", 3), ("grid", 4)):
            sl = self.zones.slots(lay, n)
            self.assertEqual(len(sl), n)
            for x, y, w, h in sl:
                self.assertTrue(0 <= x and 0 <= y and x + w <= self.zones.PIC[2] and y + h <= self.zones.PIC[3])
        s = cartoon.clean_slide({"type": "shot", "shots": [{"id": i} for i in (1, 2, 3, 4, 5, 6)], "layout": "single"})
        self.assertEqual((len(s["shots"]), s["layout"]), (4, "grid"))
        d = cartoon.clean_slide({"type": "device", "shots": [{"id": 1}, {"id": 2}], "device": "crt"})
        self.assertEqual((len(d["shots"]), d["device"]), (1, "tablet"))
        self.assertIsNone(self.ss.clean_hl({"x": 0.5, "y": 0.5, "w": 0.001, "h": 0.4}))
        self.assertEqual(self.ss.clean_hl({"x": 0.8, "y": 0.1, "w": 0.5, "h": 0.2, "arrow": 1})["w"], 0.2)   # clipped to the image

    def test_caption_claims_only_from_product_or_screenshot_note(self):
        facts = "InventoryCloud\nStock in one place"
        notes = {7: "Stock list with low stock marked in red"}
        ok = cartoon.clean_slide({"type": "shot", "shots": [{"id": 7}], "narration": "Low stock marked in red"})
        self.assertEqual(cartoon.claim_flags(ok, cartoon.slide_facts(ok, facts, notes)), "")
        bad = cartoon.clean_slide({"type": "shot", "shots": [{"id": 7}], "narration": "Save 40% and get a free guarantee"})
        self.assertIn("confirm before posting", cartoon.claim_flags(bad, cartoon.slide_facts(bad, facts, notes)))
        num = cartoon.clean_slide({"type": "shot", "shots": [{"id": 7}], "narration": "Track 500 items"})
        self.assertTrue(cartoon.claim_flags(num, cartoon.slide_facts(num, facts, notes)))
        self.assertEqual(cartoon.claim_flags(num, cartoon.slide_facts(num, facts + "\n500 items", notes)), "")

    def make_shots(self, n=3):
        c = self.m.conn()
        try:
            got = {}
            for k in range(n):
                p = self.shots.save_image(Image.new("RGB", (800, 500 + 100 * k), (10, 90, 200)), "PNG", mdb.ASSETS / "sfl" / "screenshots", "t%d" % k)
                cur = c.execute("INSERT INTO screenshots(brand_id,product_id,path,title,note,width,height) VALUES(?,?,?,?,?,800,500)",
                                (self.b["id"], self.p["id"], p.relative_to(mdb.ASSETS).as_posix(), "S%d" % k, "Stock list shows low stock %d" % k))
                got[cur.lastrowid] = {"path": p, "title": "S%d" % k, "note": "Stock list shows low stock %d" % k, "w": 800, "h": 500}
            c.commit()
            return got
        finally:
            c.close()

    def test_mocked_ai_insertion_places_validates_and_keeps_cta_last(self):
        got = self.make_shots(3)
        ids = list(got)
        story = [cartoon.clean_slide({"characters": [{"role": "shopkeeper", "position": "left"}], "dialogue": [{"speaker": "shopkeeper", "text": "t%d" % k}]}) for k in range(3)]
        story.append(cartoon.clean_slide({"is_cta": True}))
        ai_answer = {"inserts": [
            {"after": 2, "type": "shot", "shot_ids": [ids[0], 9999], "layout": "grid", "caption": "Stock list shows low stock 0"},
            {"after": 1, "type": "device", "shot_ids": [ids[1]], "device": "hologram", "caption": "Save 90% with a guarantee"},
            {"after": 3, "type": "shot", "shot_ids": [ids[1]], "caption": "reused id is dropped"},                 # ids[1] already used
            {"after": 99, "type": "shot", "shot_ids": [ids[2]], "layout": "single", "caption": ""}]}
        out = self.sr.apply_inserts(story, ai_answer, got, "InventoryCloud", "Call us")
        self.assertEqual(len(out), 4 + 3)
        self.assertTrue(out[-1]["is_cta"])                                                                      # CTA slide stays last
        dev = next(s for s in out if s["type"] == "device")
        self.assertEqual(len(dev["shots"]), 1)
        self.assertIn(dev["device"], self.ss.DEVICES)
        self.assertIn("confirm before posting", dev["claim_note"])                                              # unsupported caption flagged
        first = next(s for s in out if s["type"] == "shot")
        self.assertEqual((first["layout"], len(first["shots"])), ("single", 1))                                 # unknown id dropped, grid invalid for 1
        self.assertEqual(first["claim_note"], "")
        order = [s["type"] for s in out]
        self.assertEqual(order.index("device"), 1)                                                              # right after story slide 1
        self.assertLess(order.index("device"), order.index("shot"))
        last_shot = [s for s in out if s["type"] == "shot"][-1]
        self.assertEqual(last_shot["shots"][0]["id"], ids[2])                                                    # after=99 clamps before the CTA slide
        self.assertEqual(out.index(last_shot), len(out) - 2)

    def test_addshots_endpoint_with_mocked_ai(self):
        got = self.make_shots(2)
        ids = list(got)
        sr = self.sr
        sr.TOKENS["tk"] = _time.time()
        story = [cartoon.clean_slide({"dialogue": [{"speaker": "shopkeeper", "text": "t%d" % k}]}) for k in range(3)]
        seen = {}

        def fake_ai(task, prompt, **k):
            seen["task"], seen["big"], seen["prompt"] = task, k.get("big"), prompt
            return json.dumps({"inserts": [{"after": 1, "type": "shot", "shot_ids": ids, "layout": "2up", "caption": "Stock list shows low stock 0"}]})
        sr.ai = fake_ai
        r = self.cl.post("/api/marketing/story/addshots", json={"brand_id": self.b["id"], "product_id": self.p["id"], "shot_ids": ids,
                                                                "script": {"token": "tk", "slides": story, "language": "Hindi"}}).get_json()
        self.assertTrue(r["ok"], r)
        for _ in range(100):
            j = sr.JOBS[r["job"]]
            if j["state"] != "running":
                break
            _time.sleep(0.1)
        self.assertEqual(j["state"], "done", j["error"])
        self.assertEqual((seen["task"], seen["big"]), ("marketing_caption", True))
        self.assertIn("Stock list shows low stock 0", seen["prompt"])
        sl = j["result"]["slides"]
        self.assertEqual([s["type"] for s in sl], ["scene", "shot", "scene", "scene"])
        self.assertEqual(sl[1]["layout"], "2up")
        r = self.cl.post("/api/marketing/story/addshots", json={"brand_id": self.b["id"], "shot_ids": [], "script": {"token": "tk", "slides": story}}).get_json()
        self.assertFalse(r["ok"])
        sr.ai = lambda *a, **k: "no json at all"
        r = self.cl.post("/api/marketing/story/addshots", json={"brand_id": self.b["id"], "product_id": self.p["id"], "shot_ids": ids, "script": {"token": "tk", "slides": story}}).get_json()
        for _ in range(100):
            j = sr.JOBS[r["job"]]
            if j["state"] != "running":
                break
            _time.sleep(0.1)
        self.assertEqual(j["state"], "error")

    # ---- Job C / D ----
    def test_flux_prompts_say_blank_signs_and_no_text(self):
        from marketing import imagegen
        cast = [dict(role=r, name=n, description=d, seed=sd) for r, n, d, sd in imagegen.DEFAULT_CAST]
        prompt, _ = imagegen.scene_prompt(cartoon.clean_slide({"characters": [{"role": "shopkeeper", "position": "left"}]}), cast, cartoon.SCENES)
        for text in (prompt, imagegen.empty_bg_prompt("shop"), imagegen.empty_bg_prompt("office")):
            for w in ("blank signboards", "no signs", "no readable text", "no letters", "no logos"):
                self.assertIn(w, text)
        for w in ("no people", "no devices", "no screens"):
            self.assertIn(w, imagegen.empty_bg_prompt())

    def spec(self):
        return images.build_spec("carousel", "highlight", dict(self.b), self.p, "t", cta="Call 99999 now")

    def ctx(self, **k):
        return dict({"lookup": lambda i: None, "out_dir": pathlib.Path(self.tmp), "names": {"shopkeeper": "Ramesh"}, "ill": "", "scene_svg": cartoon.scene_svg}, **k)

    def test_cta_shown_once_as_button(self):
        sp = self.spec()
        sl = cartoon.clean_slide({"is_cta": True, "dialogue": [{"speaker": "shopkeeper", "text": "Call 99999 now"}, {"speaker": "shopkeeper", "text": "Aaiye"}],
                                  "narration": "Call 99999 now"})
        html = self.zones.build(sp, sl, 4, 5, self.ctx())
        self.assertEqual(html.count("Call 99999 now"), 1)
        self.assertIn("class='cta'", html)
        self.assertIn("Aaiye", html)                                                                            # other lines stay
        s2 = cartoon.clean_script({"slides": [{"dialogue": [{"speaker": "shopkeeper", "text": "Call 99999 now"}]}] * 3}, 3, "", "Hindi", "Call 99999 now")
        self.assertTrue(s2["slides"][-1]["is_cta"])
        self.assertNotIn("Call 99999 now", json.dumps(s2["slides"][-1]["dialogue"]))

    def test_zones_never_overlap_by_construction(self):
        z = self.zones
        pic_bottom = z.PIC[1] + z.PIC[3]
        self.assertGreater(z.TXT[1], pic_bottom)                                                                # text zone starts below the picture
        self.assertGreater(z.TXT[1] + z.TXT[3], z.TXT[1])
        self.assertLessEqual(z.TXT[1] + z.TXT[3], z.FOOT[1])                                                    # footer strip is its own band
        self.assertEqual((z.PIC[0], z.PIC[1], z.PIC[2], z.PIC[3]), (0, 0, 1080, 760))                         # picture box on top, full width
        self.assertEqual(z.TXT[1], z.PIC[3] + 24)
        self.assertLess(z.TXT[1] + z.TAIL, z.TXT[1] + z.TXT[3])
        for nb, cap, cta in ((0, False, True), (1, True, False), (2, False, False), (2, True, True)):
            b = z.budgets(nb, cap, cta)
            used = b["cta"] + (14 if cta else 0) + b["cap"] + (z.TAIL + b["bub"] * nb + 14 * (nb - 1) if nb else 0) + (14 if nb and cap else 0)
            self.assertLessEqual(used, z.TXT[3])

    def test_check_layout_detects_overlap_and_overflow(self):
        z = self.zones
        box = lambda x, y, r, b: {"x": x, "y": y, "r": r, "b": b}
        pic = box(60, 130, 1020, 830)
        clean = {"pic": pic, "texts": [{"kind": "bubble", "box": box(60, 878, 600, 1000)}, {"kind": "tail", "box": box(200, 856, 228, 878)},
                                       {"kind": "footer", "box": box(60, 1250, 1020, 1314)}], "over": [], "zoneFull": False}
        self.assertEqual(z.check_layout(clean), [])
        for kind, bad in (("bubble", box(60, 800, 600, 900)), ("tail", box(200, 820, 228, 870)), ("cta", box(100, 700, 900, 820)), ("header", box(60, 100, 1020, 140))):
            rep = dict(clean, texts=clean["texts"] + [{"kind": kind, "box": bad}])
            errs = z.check_layout(rep)
            self.assertTrue(any("overlaps the picture" in e and kind in e for e in errs), (kind, errs))
        self.assertTrue(z.check_layout(dict(clean, over=["caption"])))
        self.assertIn("text too long", z.check_layout(dict(clean, over=["caption"]))[0])
        two = dict(clean, texts=[{"kind": "bubble", "box": box(60, 880, 600, 1000)}, {"kind": "bubble", "box": box(300, 990, 1000, 1100)}])
        self.assertTrue(any("bubble overlaps bubble" in e for e in z.check_layout(two)))
        self.assertTrue(z.check_layout(dict(clean, texts=[{"kind": "cta", "box": box(60, 1200, 1200, 1300)}])))   # outside the slide

    def test_too_long_warning_in_editor(self):
        z = self.zones
        short = cartoon.clean_slide({"dialogue": [{"speaker": "shopkeeper", "text": "ठीक है"}, {"speaker": "shopkeeper", "text": "ok"}]})
        self.assertEqual(z.too_long(short), "")
        long2 = cartoon.clean_slide({"characters": [{"role": "shopkeeper", "position": "left"}, {"role": "customer", "position": "right"}],
                                     "dialogue": [{"speaker": "shopkeeper", "text": "बहुत लंबी बात " * 20}, {"speaker": "customer", "text": "छोटा"}]})
        self.assertIn("text too long", z.too_long(long2))
        cap = cartoon.clean_slide({"type": "shot", "shots": [{"id": 1}], "narration": "लंबा कैप्शन " * 60})
        self.assertIn("caption", z.too_long(cap))
        r = self.cl.post("/api/marketing/story/check", json={"script": {"slides": [short, long2, cap]}}).get_json()
        self.assertEqual([bool(x) for x in r["notes"]], [False, True, True])

    def test_render_fails_clearly_when_text_does_not_fit_and_mixed_types_render(self):
        got = self.make_shots(2)
        b = self.b
        too = {"title": "t", "slides": [cartoon.clean_slide({"type": "shot", "shots": [{"id": list(got)[0]}], "narration": "लंबा कैप्शन " * 80})]}
        with self.assertRaises(images.RenderError) as cm:
            cartoon.render_script(too, b, self.p, pathlib.Path(self.tmp) / "o1", cta="", lookup=got.get)
        self.assertIn("Slide 1", str(cm.exception))
        self.assertIn("text too long", str(cm.exception))
        miss = {"title": "t", "slides": [cartoon.clean_slide({"type": "shot", "shots": [{"id": 12345}], "narration": "x"})]}
        with self.assertRaises(images.RenderError) as cm:
            cartoon.render_script(miss, b, self.p, pathlib.Path(self.tmp) / "o2", lookup=got.get)
        self.assertIn("no screenshot", str(cm.exception))
        ids = list(got)
        mixed = {"title": "t", "slides": [
            cartoon.clean_slide({"dialogue": [{"speaker": "shopkeeper", "text": "नमस्ते"}]}),
            cartoon.clean_slide({"type": "shot", "shots": [{"id": ids[0], "hl": {"x": .1, "y": .1, "w": .4, "h": .2, "arrow": True}}, {"id": ids[1]}], "layout": "2up", "narration": "दो स्क्रीन"}),
            cartoon.clean_slide({"type": "device", "device": "phone", "shots": [{"id": ids[0]}], "narration": "फ़ोन", "dialogue": [{"speaker": "shopkeeper", "text": "देखो"}]}),
            cartoon.clean_slide({"is_cta": True})]}
        files = cartoon.render_script(mixed, b, self.p, pathlib.Path(self.tmp) / "o3", cta="Call 99999", lookup=got.get, names={"shopkeeper": "Ramesh"})
        self.assertEqual(len(files), 4)
        for f in files:
            self.assertEqual(Image.open(f).size, (1080, 1350))


class CharacterLibrary(unittest.TestCase):
    """Reference-image calls (mocked), neuron budget and the queue at the cap, character library endpoints, picture-box rules."""
    PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 2000

    def setUp(self):
        from marketing import imagegen, characters, story_routes as sr
        self.ig, self.ch, self.sr = imagegen, characters, sr
        self.tmp = tempfile.mkdtemp()
        self.old = (mdb.ASSETS, mdb.OUT_ROOT, imagegen.CACHE_DIR, imagegen.http_post, imagegen.http_post_multipart, imagegen.SLEEP, sr.ai)
        mdb.ASSETS = pathlib.Path(self.tmp) / "assets"
        mdb.OUT_ROOT = pathlib.Path(self.tmp) / "out"
        imagegen.CACHE_DIR = pathlib.Path(self.tmp) / "cache"
        imagegen.SLEEP = lambda s: None
        self.m = make_app(self.tmp)
        self.m.env_val = lambda k: {"CF_ACCOUNT_ID": "acc1", "CF_API_TOKEN": "tok1"}.get(k, "")
        self.cl = self.m.app.test_client()
        self.calls = []
        c = self.m.conn()
        mdb.ensure(c)
        c.close()

    def tearDown(self):
        (mdb.ASSETS, mdb.OUT_ROOT, self.ig.CACHE_DIR, self.ig.http_post, self.ig.http_post_multipart, self.ig.SLEEP, self.sr.ai) = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def b64(self):
        import base64
        return base64.b64encode(self.PNG).decode()

    def masters(self, n=2):
        d = pathlib.Path(self.tmp) / "m"
        d.mkdir(exist_ok=True)
        out = []
        for k in range(n):
            f = d / ("m%d.png" % k)
            Image.new("RGB", (64, 64), (k * 80, 50, 90)).save(f)
            out.append(f)
        return out

    def conn(self):
        return self.m.conn

    def cast(self, with_master=True):
        mm = self.masters(2) if with_master else [None, None]
        return [dict(role="shopkeeper", name="Ramesh", description="d1", seed=1, master=str(mm[0]) if mm[0] else None),
                dict(role="customer", name="Meena", description="d2", seed=2, master=str(mm[1]) if mm[1] else None)]

    def slide(self, two=True):
        ch = [{"role": "shopkeeper", "expression": "happy", "position": "left"}] + ([{"role": "customer", "expression": "worried", "position": "right"}] if two else [])
        return cartoon.clean_slide({"description": "Shopkeeper2 shows a tablet to Customer", "characters": ch, "scene": "shop_counter"})

    def settings(self):
        return lambda k, d: self.m.settings.get(k, d)

    # ---- Job C: reference call ----
    def test_klein_multipart_call_and_prompt(self):
        def post(url, headers, fields, files):
            self.calls.append((url, headers, fields, files))
            return {"result": {"image": self.b64()}}
        self.ig.http_post_multipart = post
        prompt, masters = self.ig.ref_scene_prompt(self.slide(), self.cast(), cartoon.SCENES)
        self.assertEqual(len(masters), 2)
        self.assertTrue(prompt.startswith("The first person is the same as in reference image 1"))
        one, m1 = self.ig.ref_scene_prompt(self.slide(False), self.cast(), cartoon.SCENES)
        self.assertTrue(one.startswith("Same person as in the reference image, same face, hair, clothes."))
        self.assertEqual(len(m1), 1)
        for text in (prompt, one):
            for w in ("blank signboards", "no readable text", "no letters", "no logos", "70 percent", "Medium shot"):
                self.assertIn(w, text)
            self.assertNotIn("Shopkeeper2", text)
        r = self.ig.generate_refs(prompt, masters, self.m.env_val, self.conn(), self.settings())
        self.assertEqual(r["provider"], "cloudflare-ref")
        url, hdr, fields, files = self.calls[0]
        self.assertEqual(url, "https://api.cloudflare.com/client/v4/accounts/acc1/ai/run/@cf/black-forest-labs/flux-2-klein-4b")
        self.assertEqual(hdr["Authorization"], "Bearer tok1")
        self.assertEqual(sorted(files), ["input_image_0", "input_image_1"])                 # first character = input_image_0
        self.assertEqual(files["input_image_0"], pathlib.Path(masters[0]).read_bytes())
        self.assertEqual((fields["width"], fields["height"], fields["prompt"]), (str(self.ig.SCENE_W), str(self.ig.SCENE_H), prompt))
        self.assertAlmostEqual(self.ig.SCENE_W / self.ig.SCENE_H, 1080 / 760, delta=0.03)   # generated at picture-box proportions
        self.assertEqual(self.ig.SCENE_W % 16 + self.ig.SCENE_H % 16, 0)
        again = self.ig.generate_refs(prompt, masters, self.m.env_val, self.conn(), self.settings())
        self.assertTrue(again["cached"])
        self.assertEqual(len(self.calls), 1)

    def test_neuron_budget_images_left_and_queue_at_cap(self):
        self.ig.http_post_multipart = lambda *a: {"result": {"image": self.b64()}}
        c = self.m.conn()
        self.assertEqual(self.ig.images_left(c, self.settings()), 10000 // 125)
        c.close()
        prompt, masters = self.ig.ref_scene_prompt(self.slide(), self.cast(), cartoon.SCENES)
        self.assertTrue(self.ig.generate_refs(prompt, masters, self.m.env_val, self.conn(), self.settings())["file"])
        c = self.m.conn()
        self.assertEqual(self.ig.neurons_today(c), self.ig.neurons_for("klein", 2))
        self.assertLess(self.ig.images_left(c, self.settings()), 10000 // 125)
        c.close()
        self.m.settings["mkt_neuron_cap"] = 50                                              # budget used up
        p2 = prompt + " other"
        r = self.ig.generate_refs(p2, masters, self.m.env_val, self.conn(), self.settings())
        self.assertTrue(r.get("queued"))
        self.assertIn("queued for tomorrow", r["reason"])
        r = self.ig.generate_refs(p2, masters, self.m.env_val, self.conn(), self.settings())      # no duplicate queue rows
        c = self.m.conn()
        self.assertEqual(self.ig.queued_count(c), 1)
        c.close()
        self.assertEqual(self.ig.drain_queue(self.m.env_val, self.conn(), self.settings()), 0)    # still no budget
        self.m.settings["mkt_neuron_cap"] = 10000                                           # "tomorrow"
        c = self.m.conn()
        c.execute("DELETE FROM imagegen_usage")
        c.commit()
        c.close()
        self.assertEqual(self.ig.drain_queue(self.m.env_val, self.conn(), self.settings()), 1)
        c = self.m.conn()
        self.assertEqual(self.ig.queued_count(c), 0)
        c.close()
        self.assertTrue(self.ig.generate_refs(p2, masters, self.m.env_val, self.conn(), self.settings())["cached"])   # next render is free
        h = self.cl.get("/marketing").get_data(as_text=True)
        self.assertIn("Images left today", h)

    def test_scene_chain_order_masters_then_text_then_svg(self):
        self.ig.http_post_multipart = lambda *a: {"result": {"image": self.b64()}}
        self.ig.http_post = lambda *a: {"result": {"image": self.b64()}}
        r = self.ig.scene_chain(self.slide(), self.cast(), cartoon.SCENES, False, self.m.env_val, self.conn(), self.settings())
        self.assertEqual(r["provider"], "cloudflare-ref")
        r = self.ig.scene_chain(self.slide(), self.cast(False), cartoon.SCENES, False, self.m.env_val, self.conn(), self.settings())      # no masters: text-only schnell
        self.assertEqual(r["provider"], "cloudflare")
        self.assertIn("master", r["note"])
        r = self.ig.scene_chain(self.slide(), self.cast(), cartoon.SCENES, False, self.m.env_val, self.conn(), self.settings(), off=True)
        self.assertEqual(r["provider"], "svg")
        def boom(*a):
            raise self.ig.ImageError("HTTP 403: no")
        self.ig.http_post_multipart, self.ig.http_post = boom, boom
        other = self.slide()
        other["description"] = "a different scene that was never cached"
        r = self.ig.scene_chain(other, self.cast(), cartoon.SCENES, False, self.m.env_val, self.conn(), self.settings())
        self.assertEqual((r["file"], r["provider"]), (None, "svg"))

    # ---- Job C: library ----
    def test_defaults_seeded_and_mapped_to_roles(self):
        c = self.m.conn()
        names = [r[0] for r in c.execute("SELECT name FROM characters ORDER BY id")]
        self.assertEqual(names[:3], ["Ramesh", "Sunil", "Meena"])
        rows = c.execute("SELECT m.role, ch.name FROM cast_members m JOIN characters ch ON ch.id=m.char_id").fetchall()
        c.close()
        self.assertEqual({r[0]: r[1] for r in rows}, {"shopkeeper": "Ramesh", "shopkeeper2": "Sunil", "customer": "Meena"})
        h = self.cl.get("/marketing/characters").get_data(as_text=True)
        for w in ("Ramesh", "Sunil", "Meena", "Make 3 candidates", "Propose characters", "no master yet"):
            self.assertIn(w, h)
        c = self.m.conn()
        brand = c.execute("SELECT * FROM brands LIMIT 1").fetchone()
        c.close()
        cast = self.sr._cast(brand)
        self.assertTrue(all(x["master"] is None for x in cast))

    def wait(self, jid):
        for _ in range(100):
            j = self.sr.JOBS[jid]
            if j["state"] != "running":
                return j
            _time.sleep(0.1)
        self.fail("job did not finish")

    def test_candidates_pick_lock_master_and_slides_use_it(self):
        self.ig.http_post = lambda url, h, p: {"result": {"image": __import__("base64").b64encode(self.png_big()).decode()}}
        r = self.cl.post("/api/marketing/characters/1/candidates", json={"round": 1}).get_json()
        j = self.wait(r["job"])
        self.assertEqual(j["state"], "done", j["error"])
        cands = j["result"]["candidates"]
        self.assertEqual(len(cands), 3)
        prompts = set()
        self.assertTrue(all(self.ig.safe_name(c["name"]) for c in cands))
        self.assertEqual(self.cl.get(cands[0]["url"]).status_code, 200)
        self.assertFalse(self.cl.post("/api/marketing/characters/1/pick", json={"name": "../../x.png"}).get_json()["ok"])
        self.assertTrue(self.cl.post("/api/marketing/characters/1/pick", json={"name": cands[1]["name"]}).get_json()["ok"])
        self.assertEqual(self.cl.get("/marketing/char/1").status_code, 200)
        self.assertTrue((mdb.ASSETS / "characters" / "1.jpg").is_file())
        self.assertLessEqual(max(Image.open(mdb.ASSETS / "characters" / "1.jpg").size), 1536)
        c = self.m.conn()
        brand = c.execute("SELECT * FROM brands LIMIT 1").fetchone()
        c.close()
        cast = {x["role"]: x for x in self.sr._cast(brand)}
        self.assertTrue(cast["shopkeeper"]["master"])                                        # locked master is what slides pass as reference
        self.assertIsNone(cast["customer"]["master"])

    def png_big(self):
        buf = io.BytesIO()
        Image.new("RGB", (1400, 1400), (120, 80, 60)).save(buf, "PNG")
        return buf.getvalue()

    def test_manage_characters_create_rename_upload_delete(self):
        r = self.cl.post("/api/marketing/characters", json={"name": "Lakshmi", "description": "elderly woman, silver hair, green saree"}).get_json()
        self.assertTrue(r["ok"])
        cid = r["id"]
        self.assertFalse(self.cl.post("/api/marketing/characters", json={"name": "", "description": "x"}).get_json()["ok"])
        self.assertTrue(self.cl.put("/api/marketing/characters/%d" % cid, json={"name": "Lakshmi ji", "description": "elderly woman"}).get_json()["ok"])
        up = self.cl.post("/api/marketing/characters/%d/master" % cid, data={"file": (io.BytesIO(self.png_big()), "m.png")}, content_type="multipart/form-data").get_json()
        self.assertTrue(up["ok"], up)
        self.assertFalse(self.cl.post("/api/marketing/characters/%d/master" % cid, data={"file": (io.BytesIO(b"nope"), "m.png")}, content_type="multipart/form-data").get_json()["ok"])
        self.assertIn("Lakshmi ji", self.cl.get("/marketing/characters").get_data(as_text=True))
        f = mdb.ASSETS / "characters" / ("%d.jpg" % cid)
        self.assertTrue(f.is_file())
        c = self.m.conn()
        c.execute("UPDATE cast_members SET char_id=? WHERE role='customer'", (cid,))
        c.commit()
        c.close()
        self.assertTrue(self.cl.delete("/api/marketing/characters/%d" % cid).get_json()["ok"])
        self.assertFalse(f.exists())
        c = self.m.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM cast_members WHERE char_id=?", (cid,)).fetchone()[0], 0)
        c.close()

    def test_master_upload_accepts_uppercase_names_and_any_name_by_content(self):
        def up(name, data):
            return self.cl.post("/api/marketing/characters/1/master", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data").get_json()
        buf = io.BytesIO()
        Image.new("RGB", (2400, 1800), (90, 60, 40)).save(buf, "JPEG")
        jpg = buf.getvalue()
        for name in ("VIK_0280.JPG", "a.JPEG", "b.jpeg", "photo.dat", "noext"):                # name does not matter, content does
            r = up(name, jpg)
            self.assertTrue(r["ok"], (name, r))
            im = Image.open(mdb.ASSETS / "characters" / "1.jpg")
            self.assertEqual(im.format, "JPEG")
            self.assertEqual(max(im.size), 1536)                                                 # downscaled, converted to JPEG
        for name, fmt in (("X.PNG", "PNG"), ("Y.WEBP", "WEBP")):
            b = io.BytesIO()
            Image.new("RGBA", (300, 200), (1, 2, 3, 255)).save(b, fmt)
            self.assertTrue(up(name, b.getvalue())["ok"], name)
        self.assertFalse(up("VIK.JPG", b"not an image")["ok"])
        self.assertFalse((mdb.ASSETS / "characters" / "1.png").exists())

    def test_propose_characters_from_brief_mocked_ai(self):
        self.sr.ai = lambda task, prompt, **k: json.dumps({"characters": [{"name": "Gopal", "description": "young tea seller, thin, cap"}, {"name": "", "description": "x"},
                                                                          {"name": "Asha", "description": "middle-aged tailor, glasses"}]})
        r = self.cl.post("/api/marketing/characters/propose", json={"brief": "do dukaan wale"}).get_json()
        j = self.wait(r["job"])
        self.assertEqual([x["name"] for x in j["result"]["characters"]], ["Gopal", "Asha"])
        self.assertFalse(self.cl.post("/api/marketing/characters/propose", json={"brief": " "}).get_json()["ok"])

    def test_brand_kit_picks_library_character_per_role(self):
        c = self.m.conn()
        bid = c.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
        c.close()
        self.assertIn("char_customer", self.cl.get("/marketing/brands").get_data(as_text=True))
        self.cl.post("/marketing/cast/%d" % bid, data={"char_customer": "1"})
        c = self.m.conn()
        self.assertEqual(c.execute("SELECT char_id FROM cast_members WHERE brand_id=? AND role='customer'", (bid,)).fetchone()[0], 1)
        c.close()


if __name__ == "__main__":
    unittest.main()
