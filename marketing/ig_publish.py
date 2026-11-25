"""Instagram publisher (Graph API) with Cloudinary as the temporary image host.

Flow: signed Cloudinary upload -> /{ig}/media container(s) -> poll status once a minute (max 5 min) -> /{ig}/media_publish
      -> read permalink -> delete the Cloudinary image.
Rules: approval gate, rolling 24h cap of 40 (plus Instagram's own content_publishing_limit), DRY_RUN runs everything except media_publish.
Tokens and secrets are never logged, never returned and never put in error text.
TODO(Instagram publisher): this module is the publisher. Facebook and WhatsApp status are manual (download the images from the Queue).
"""
import hashlib, json, time, threading, pathlib, uuid, datetime
import requests

IG_HOST = "https://graph.facebook.com/v21.0"          # no graph.instagram.com fallback (verified with this token)
CLOUD_HOST = "https://api.cloudinary.com/v1_1"
CAP = 40
POLL_ATTEMPTS = 5
POLL_SECS = 60
TIMEOUT = 60
_PUBLISH_LOCK = threading.Lock()


class IGError(Exception):
    """kind: config | auth | permission | limit | container | network | other. Message is safe to show in the UI."""
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class Config:
    KEYS = ("IG_USER_ID", "IG_ACCESS_TOKEN", "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")

    def __init__(self, getenv):
        self._v = {k: (getenv(k) or "").strip() for k in self.KEYS}

    def __getattr__(self, name):
        v = self.__dict__.get("_v", {})
        if name.lower() in {k.lower() for k in v}:
            return v[name.upper()]
        raise AttributeError(name)

    def missing(self):
        return [k for k, v in self._v.items() if not v]

    def require(self, keys=None):
        miss = [k for k in (keys or self.KEYS) if not self._v.get(k)]
        if miss:
            raise IGError("config", "Missing settings: " + ", ".join(miss) + ". Add them to .env and restart.")


# ---------- Cloudinary ----------
def sign(params, secret):
    """Cloudinary signature: SHA1 of the sorted 'k=v' pairs joined by '&', with the API secret appended."""
    s = "&".join("%s=%s" % (k, params[k]) for k in sorted(params) if params[k] not in (None, ""))
    return hashlib.sha1((s + secret).encode("utf-8")).hexdigest()


def _cloud_url(cfg, action):
    return "%s/%s/image/%s" % (CLOUD_HOST, cfg.CLOUDINARY_CLOUD_NAME, action)


def _net(fn, what):
    try:
        return fn()
    except requests.RequestException:
        raise IGError("network", "Could not reach %s. Check the internet connection and try again." % what)


def cloud_upload(cfg, path, http=requests, now=time.time):
    """Signed upload. Returns (delivery_url as JPEG, public_id)."""
    cfg.require(("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"))
    public_id = "mkt_" + uuid.uuid4().hex[:12]
    ts = int(now())
    params = {"public_id": public_id, "timestamp": ts}
    data = dict(params, api_key=cfg.CLOUDINARY_API_KEY, signature=sign(params, cfg.CLOUDINARY_API_SECRET))
    with open(path, "rb") as f:
        r = _net(lambda: http.post(_cloud_url(cfg, "upload"), data=data, files={"file": f}, timeout=TIMEOUT), "Cloudinary")
    j = _json(r)
    if r.status_code != 200 or "secure_url" not in j:
        msg = (j.get("error") or {}).get("message", "") if isinstance(j.get("error"), dict) else ""
        raise IGError("auth" if r.status_code in (401, 403) else "other", "Cloudinary upload failed. %s" % msg[:120])
    url = j["secure_url"].replace("/upload/", "/upload/f_jpg/", 1)
    if not url.lower().endswith(".jpg"):
        url = url.rsplit(".", 1)[0] + ".jpg"
    return url, j.get("public_id", public_id)


def cloud_delete(cfg, public_id, http=requests, now=time.time):
    """Best effort: a failed delete never fails a publish."""
    try:
        params = {"public_id": public_id, "timestamp": int(now())}
        data = dict(params, api_key=cfg.CLOUDINARY_API_KEY, signature=sign(params, cfg.CLOUDINARY_API_SECRET))
        http.post(_cloud_url(cfg, "destroy"), data=data, timeout=TIMEOUT)
        return True
    except Exception:
        return False


# ---------- Graph API ----------
def _json(r):
    try:
        j = r.json()
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def map_error(status, j):
    """Turn a Graph API error body into an IGError with a plain message."""
    e = j.get("error") if isinstance(j.get("error"), dict) else {}
    code, sub, msg = e.get("code"), e.get("error_subcode"), (e.get("message") or "")
    low = msg.lower()
    if code in (190, 102) or status == 401 or "access token" in low:
        return IGError("auth", "Instagram login failed: the access token is invalid or expired. Create a new token and update .env.")
    if code in (10, 200, 3) or (isinstance(code, int) and 200 <= code <= 299) or "permission" in low:
        return IGError("permission", "Permission not granted. Meta app review may be needed for content publishing.")
    if code in (4, 17, 32, 613, 9) or "limit" in low or "too many" in low:
        return IGError("limit", "Instagram publishing limit reached. Try again later.")
    if code in (9004, 9007, 2207026, 2207003, 2207052) or sub in (2207026, 2207003) or "media" in low or "container" in low:
        return IGError("container", "Instagram could not process the image: %s" % msg[:140])
    return IGError("other", "Instagram error: %s" % (msg[:140] or "HTTP %s" % status))


def _graph(cfg, method, path, http=requests, **params):
    cfg.require(("IG_USER_ID", "IG_ACCESS_TOKEN"))
    params["access_token"] = cfg.IG_ACCESS_TOKEN
    url = "%s/%s" % (IG_HOST, path.lstrip("/"))
    if method == "GET":
        r = _net(lambda: http.get(url, params=params, timeout=TIMEOUT), "Instagram")
    else:
        r = _net(lambda: http.post(url, data=params, timeout=TIMEOUT), "Instagram")
    j = _json(r)
    if r.status_code >= 400 or "error" in j:
        raise map_error(r.status_code, j)
    return j


def test_connection(cfg, http=requests):
    """GET /{IG_USER_ID}?fields=username only (account_type errors on this endpoint)."""
    try:
        j = _graph(cfg, "GET", cfg.IG_USER_ID, http, fields="username")
        return {"ok": True, "username": j.get("username", "")}
    except IGError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def publishing_limit(cfg, http=requests):
    """Instagram's own count of posts used in the last 24h, or None if it cannot be read (not fatal)."""
    try:
        j = _graph(cfg, "GET", "%s/content_publishing_limit" % cfg.IG_USER_ID, http, fields="quota_usage,config")
        d = (j.get("data") or [{}])[0]
        return int(d.get("quota_usage", 0))
    except Exception:
        return None


def create_container(cfg, image_url, kind, caption="", alt="", http=requests):
    p = {"image_url": image_url}
    if kind == "story":
        p["media_type"] = "STORIES"
    elif kind == "carousel_item":
        p["is_carousel_item"] = "true"
    else:
        if caption:
            p["caption"] = caption
        if alt:
            p["alt_text"] = alt
    return _graph(cfg, "POST", "%s/media" % cfg.IG_USER_ID, http, **p)["id"]


def create_carousel(cfg, children, caption, http=requests):
    p = {"media_type": "CAROUSEL", "children": ",".join(children)}
    if caption:
        p["caption"] = caption
    return _graph(cfg, "POST", "%s/media" % cfg.IG_USER_ID, http, **p)["id"]


def wait_ready(cfg, container_id, http=requests, sleep=time.sleep):
    """Poll status_code once a minute for up to 5 minutes."""
    last = ""
    for i in range(POLL_ATTEMPTS):
        last = _graph(cfg, "GET", container_id, http, fields="status_code").get("status_code", "")
        if last == "FINISHED":
            return True
        if last in ("ERROR", "EXPIRED"):
            raise IGError("container", "Instagram could not process the image (status %s)." % last)
        if i < POLL_ATTEMPTS - 1:
            sleep(POLL_SECS)
    raise IGError("container", "Instagram took too long to process the image (last status %s)." % (last or "unknown"))


def media_publish(cfg, creation_id, http=requests):
    return _graph(cfg, "POST", "%s/media_publish" % cfg.IG_USER_ID, http, creation_id=creation_id)["id"]


def get_permalink(cfg, media_id, http=requests):
    try:
        return _graph(cfg, "GET", media_id, http, fields="permalink").get("permalink", "")
    except IGError:
        return ""


def publish_images(cfg, files, post_type, caption="", alt="", dry_run=True, http=requests, sleep=time.sleep):
    """Upload -> container(s) -> poll -> publish (unless dry_run) -> permalink -> delete hosted images.
    Returns {"dry_run": bool, "media_id": str, "permalink": str}."""
    cfg.require()
    if post_type == "carousel" and not 2 <= len(files) <= 10:
        raise IGError("other", "A carousel needs 2 to 10 images.")
    if post_type != "carousel":
        files = files[:1]
    hosted = []
    try:
        urls = []
        for f in files:
            u, pid = cloud_upload(cfg, f, http)
            hosted.append(pid)
            urls.append(u)
        if post_type == "carousel":
            kids = []
            for u in urls:
                cid = create_container(cfg, u, "carousel_item", http=http)
                wait_ready(cfg, cid, http, sleep)
                kids.append(cid)
            container = create_carousel(cfg, kids, caption, http)
        else:
            container = create_container(cfg, urls[0], "story" if post_type == "story" else "post", caption, alt, http)
        wait_ready(cfg, container, http, sleep)
        if dry_run:
            return {"dry_run": True, "media_id": "", "permalink": ""}
        media_id = media_publish(cfg, container, http)
        return {"dry_run": False, "media_id": media_id, "permalink": get_permalink(cfg, media_id, http)}
    finally:
        for pid in hosted:
            cloud_delete(cfg, pid, http)


# ---------- service used by the Queue page and the scheduler ----------
def _count_24h(c):
    since = (datetime.datetime.now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    return c.execute("SELECT COUNT(*) FROM posts WHERE status='published' AND published_at>=?", (since,)).fetchone()[0]


def publish_post(M, pid, http=requests, sleep=time.sleep):
    """Publish one post. Returns (ok, message). Enforces the approval gate, the 24h cap and DRY_RUN."""
    from . import queue as mq, db as mdb
    with _PUBLISH_LOCK:
        c = M.conn()
        try:
            p = mq.get_post(c, pid)
            if not mq.can_publish(p):
                return False, "Approve the post before publishing it."
            if "instagram" not in (p["platforms"] or ""):
                return False, "This post is not set to go to Instagram. Download the images and post it manually."
            files = [f for f in json.loads(p["image_paths"] or "[]") if pathlib.Path(f).exists()]
            if not files:
                return False, "The post's image files are missing. Create the post again."
            cfg = Config(M.env_val)
            miss = cfg.missing()
            if miss:
                return False, "Missing settings: " + ", ".join(miss) + "."
            if _count_24h(c) >= CAP:
                return False, "Daily limit reached (%d posts in 24 hours). It will be tried again later." % CAP
            used = publishing_limit(cfg, http)
            if used is not None and used >= CAP:
                return False, "Instagram reports %d posts used in the last 24 hours. Try again later." % used
            prior = p["status"] if p["status"] in ("approved", "scheduled") else "approved"
            dry = M.qset("mkt_dry_run", "1") == "1"
            c.execute("UPDATE posts SET status='publishing', error='' WHERE id=?", (pid,))
            c.commit()
            caption = (p["caption"] or "").strip()
            if (p["hashtags"] or "").strip():
                caption += "\n\n" + p["hashtags"].strip()
            try:
                res = publish_images(cfg, files, p["type"], caption, p["description_alt"] or "", dry, http, sleep)
            except IGError as e:
                if e.kind == "limit":                                   # not the post's fault: keep it waiting
                    c.execute("UPDATE posts SET status=?, error=? WHERE id=?", (prior, str(e), pid))
                else:
                    c.execute("UPDATE posts SET status='failed', error=? WHERE id=?", (str(e), pid))
                c.commit()
                return False, str(e)
            except Exception:
                c.execute("UPDATE posts SET status='failed', error=? WHERE id=?", ("Unexpected error while publishing. See the app log.", pid))
                c.commit()
                return False, "Unexpected error while publishing."
            if res["dry_run"]:
                c.execute("UPDATE posts SET status=?, error='' WHERE id=?", (prior, pid))
                c.commit()
                return True, "Dry run finished: upload, container and processing worked. Nothing was published."
            c.execute("UPDATE posts SET status='published', ig_media_id=?, ig_permalink=?, published_at=?, error='' WHERE id=?",
                      (res["media_id"], res["permalink"], mdb.now(), pid))
            c.commit()
            return True, "Published."
        finally:
            c.close()
