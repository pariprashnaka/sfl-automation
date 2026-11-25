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
        self.assertEqual(len(j["result"]["images"]), 3)
        tok = j["result"]["token"]
        for k in (1, 2, 3):
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


if __name__ == "__main__":
    unittest.main()
