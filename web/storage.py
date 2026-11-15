"""Where match media (clips, thumbnails, avatars) lives: this PC's disk, or a Cloudflare R2 bucket.

Local (default): web/data/media/<slug>/<file>, served by the site at /media/<slug>/<file>.
R2 (when R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET are set):
    m/<slug>/<rev>/<file> in the bucket, viewed straight from the bucket's public address (MEDIA_URL,
    e.g. https://pub-xxxx.r2.dev or a custom domain). Each upload of a match goes to a new <rev> folder,
    so re-uploading never breaks a clip someone is watching; the old folder is removed afterwards.
    upload.py sends files directly to the bucket with short-lived signed links (the website only signs
    them), so big clips never pass through Vercel's 4.5 MB request limit.

S3 requests are signed here with AWS Signature V4 (R2 speaks the S3 API); no boto3 needed on the server.
"""
import datetime
import hashlib
import hmac
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import config  # noqa: F401  (loads admin.env)

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, "data", "media")

ACCOUNT = os.environ.get("R2_ACCOUNT_ID", "").strip()
ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
BUCKET = os.environ.get("R2_BUCKET", "").strip()
ENDPOINT = (os.environ.get("R2_ENDPOINT", "").strip()   # override for tests (e.g. a local moto server)
            or (f"https://{ACCOUNT}.r2.cloudflarestorage.com" if ACCOUNT else "")).rstrip("/")
REGION = os.environ.get("R2_REGION", "auto")
MEDIA_URL = os.environ.get("MEDIA_URL", "").strip().rstrip("/")
R2 = bool(ENDPOINT and ACCESS_KEY and SECRET_KEY and BUCKET)

CONTENT_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".json": "application/json"}


def content_type(path):
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


# ---------------------------------------------------------------- addresses ----

def local_dir(slug):
    return os.path.join(LOCAL_DIR, slug)


def key(slug, rev, path):
    return f"m/{slug}/{rev}/{path}"


def base(slug, rev):
    """URL prefix of a match's files (ends with /)."""
    if R2 and rev:
        return f"{MEDIA_URL or ENDPOINT + '/' + BUCKET}/m/{slug}/{rev}/"
    return f"/media/{slug}/"


def url(slug, rev, path):
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return base(slug, rev) + urllib.parse.quote(path)


# ---------------------------------------------------------------- SigV4 ----

def _quote(s, safe="/"):
    return urllib.parse.quote(s, safe=safe + "~")


def _signing_key(datestamp):
    k = hmac.new(("AWS4" + SECRET_KEY).encode(), datestamp.encode(), hashlib.sha256).digest()
    for part in (REGION, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return k


def _object_path(obj_key=""):
    return "/" + BUCKET + ("/" + _quote(obj_key) if obj_key else "")


def _canonical_query(params):
    return "&".join(f"{_quote(k, '')}={_quote(str(v), '')}" for k, v in sorted(params.items()))


def presign(method, obj_key, expires=3600, now=None):
    """Signed URL anyone can use for `method` on one object until it expires (query-string SigV4)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date, datestamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    host = urllib.parse.urlsplit(ENDPOINT).netloc
    scope = f"{datestamp}/{REGION}/s3/aws4_request"
    params = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{ACCESS_KEY}/{scope}",
              "X-Amz-Date": amz_date, "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host"}
    path = _object_path(obj_key)
    canonical = "\n".join([method, path, _canonical_query(params), f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    sig = hmac.new(_signing_key(datestamp), to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{ENDPOINT}{path}?{_canonical_query(params)}&X-Amz-Signature={sig}"


def _request(method, obj_key="", params=None, body=b"", headers=None, now=None):
    """Signed S3 request (Authorization header). Returns (status, body bytes)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date, datestamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    host = urllib.parse.urlsplit(ENDPOINT).netloc
    scope = f"{datestamp}/{REGION}/s3/aws4_request"
    payload = "UNSIGNED-PAYLOAD" if hasattr(body, "read") else hashlib.sha256(body).hexdigest()
    hdrs = {"host": host, "x-amz-content-sha256": payload, "x-amz-date": amz_date}
    hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
    signed = sorted(hdrs)
    path = _object_path(obj_key)
    query = _canonical_query(params or {})
    canonical = "\n".join([method, path, query, "".join(f"{h}:{str(hdrs[h]).strip()}\n" for h in signed),
                           ";".join(signed), payload])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    sig = hmac.new(_signing_key(datestamp), to_sign.encode(), hashlib.sha256).hexdigest()
    hdrs["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/{scope}, SignedHeaders={';'.join(signed)}, "
                             f"Signature={sig}")
    hdrs.pop("host")
    req = urllib.request.Request(f"{ENDPOINT}{path}" + (f"?{query}" if query else ""), data=body or None,
                                 method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class StorageError(Exception):
    pass


def _check(status, body, what):
    if status >= 300:
        raise StorageError(f"R2 {what} failed: HTTP {status} {body[:300].decode('utf-8', 'replace')}")


# ---------------------------------------------------------------- bucket operations ----

def list_objects(prefix):
    """{key: size} of every object under prefix."""
    out, token = {}, None
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        status, body = _request("GET", params=params)
        _check(status, body, "list")
        root = ET.fromstring(body)
        ns = re.match(r"\{.*\}", root.tag)
        ns = ns.group(0) if ns else ""
        for c in root.iter(f"{ns}Contents"):
            out[c.find(f"{ns}Key").text] = int(c.find(f"{ns}Size").text)
        if (root.findtext(f"{ns}IsTruncated") or "false") != "true":
            return out
        token = root.findtext(f"{ns}NextContinuationToken")


def put_file(obj_key, local_path):
    with open(local_path, "rb") as f:
        data = f.read()
    status, body = _request("PUT", obj_key, body=data, headers={"content-type": content_type(local_path)})
    _check(status, body, f"upload of {obj_key}")


def delete_keys(keys):
    for k in keys:
        status, body = _request("DELETE", k)
        if status not in (200, 204, 404):
            _check(status, body, f"delete of {k}")


def files_of(slug, rev):
    """{relative path: size} of one upload's files in the bucket."""
    prefix = key(slug, rev, "")
    return {k[len(prefix):]: v for k, v in list_objects(prefix).items()}


def drop_other_revs(slug, rev):
    """Delete a match's files from older uploads (keeps the folder `rev`)."""
    keep = key(slug, rev, "")
    delete_keys([k for k in list_objects(f"m/{slug}/") if not k.startswith(keep)])


def delete_match(slug):
    if R2:
        delete_keys(list(list_objects(f"m/{slug}/")))
    import shutil
    shutil.rmtree(local_dir(slug), ignore_errors=True)
