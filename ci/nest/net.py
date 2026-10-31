"""HTTP: text/JSON fetches, verified downloads with a content cache, probes.

Failure is classified rather than collapsed into one "failed": a 404 means the
resource is gone, a timeout or 5xx/429/403-rate-limit says nothing about it.
`Unreachable` exists so a scheduled check does not report a deleted upstream
when GitHub merely rate-limited the runner.
"""

import hashlib
import http.client
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

from . import util

USER_AGENT = "pica-nest/2 (+https://github.com/miaoermua/pica-nest)"
_TEXT_CACHE = {}


class FetchError(util.NestError):
    """`status` is missing | unreachable | invalid."""

    def __init__(self, url, status, reason):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.status = status
        self.reason = reason


def _headers(url, accept=None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    host = urllib.parse.urlsplit(url).hostname or ""
    # Only the API: a release download redirects to a signed CDN URL, and
    # urllib would forward the header to it.
    if token and host == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    return headers


def quote_url(url):
    """Percent-encode what a feed filename may carry, keeping the URL's structure."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path=urllib.parse.quote(parts.path, safe="/@+~:")))


def _retryable(err):
    if isinstance(err, urllib.error.HTTPError):
        return err.code in (408, 429, 500, 502, 503, 504)
    return isinstance(err, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))


def _classify(url, err):
    if isinstance(err, urllib.error.HTTPError):
        if err.code in (404, 410):
            return FetchError(url, "missing", f"HTTP {err.code}")
        if err.code in (403, 408, 429) or err.code >= 500:
            return FetchError(url, "unreachable", f"HTTP {err.code}")
        return FetchError(url, "invalid", f"HTTP {err.code}")
    return FetchError(url, "unreachable", str(getattr(err, "reason", err)))


def _open(url, attempts=3, timeout=60, method="GET", headers=None):
    last = None
    for attempt in range(attempts):
        request = urllib.request.Request(quote_url(url), method=method, headers=headers or _headers(url))
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except Exception as err:  # noqa: BLE001 - classified below
            last = err
            if not _retryable(err) or attempt + 1 == attempts:
                break
            time.sleep(min(2 ** attempt, 8))
    raise _classify(url, last)


def get_bytes(url, attempts=3):
    with _open(url, attempts) as response:
        try:
            return response.read()
        except (OSError, http.client.HTTPException) as err:
            raise FetchError(url, "unreachable", f"connection failed mid-read: {err}") from err


def get_text(url, attempts=3):
    """GET a small document, memoised for this process (indexes, release lists)."""
    if url not in _TEXT_CACHE:
        _TEXT_CACHE[url] = get_bytes(url, attempts).decode("utf-8", "replace")
    return _TEXT_CACHE[url]


def get_json(url, attempts=3):
    text = get_text(url, attempts)
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise FetchError(url, "invalid", f"not JSON: {err}") from err


def github_api(path):
    return get_json(f"https://api.github.com/{path.lstrip('/')}")


def probe(url, expected_size=None):
    """HEAD a URL. Returns (status, reason): valid | missing | unreachable."""
    try:
        with _open(url, attempts=3, timeout=30, method="HEAD") as response:
            length = response.headers.get("Content-Length")
    except FetchError as err:
        if err.status == "invalid" and "405" in err.reason:
            try:
                headers = _headers(url)
                headers["Range"] = "bytes=0-0"
                with _open(url, attempts=2, timeout=30, headers=headers):
                    return "valid", "range request answered"
            except FetchError as range_err:
                return range_err.status, range_err.reason
        return err.status, err.reason
    if expected_size is not None and length and length.isdigit() and int(length) != expected_size:
        return "missing", f"size is {length}, expected {expected_size}"
    return "valid", ""


# --- downloads ------------------------------------------------------------

def _cache_paths(cache_dir, url):
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, key), os.path.join(cache_dir, key + ".json")


def _from_cache(cache_dir, url, dest, size, sha256):
    data, meta_file = _cache_paths(cache_dir, url)
    try:
        meta = util.read_json(meta_file)
    except (OSError, ValueError):
        return False
    if meta.get("url") != url or not os.path.isfile(data):
        return False
    if size is not None and meta.get("size") != size:
        return False
    if sha256 and meta.get("sha256") != sha256:
        return False
    if util.sha256_file(data) != meta.get("sha256"):
        return False
    shutil.copyfile(data, dest)
    return True


def _to_cache(cache_dir, url, source, size, sha256):
    os.makedirs(cache_dir, exist_ok=True)
    data, meta_file = _cache_paths(cache_dir, url)
    shutil.copyfile(source, data + ".tmp")
    os.replace(data + ".tmp", data)
    util.write_json(meta_file, {"url": url, "size": size, "sha256": sha256})


def forget(cache_dir, url):
    if cache_dir:
        for item in _cache_paths(cache_dir, url):
            try:
                os.remove(item)
            except OSError:
                pass


def download(url, dest, size=None, sha256=None, cache_dir=None, attempts=3):
    """Download to `dest`, verifying length and digest. Raises FetchError.

    A partial or mismatched file is removed, never left for the next step to
    mistake for a resource. A cache entry is trusted only after its URL, size
    and digest check out.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    if cache_dir and _from_cache(cache_dir, url, dest, size, sha256):
        util.log(f"  cached   {url}")
        return
    last = None
    for attempt in range(attempts):
        util.log(f"  download {url}")
        try:
            with _open(url, attempts=1, timeout=120) as response:
                declared = response.headers.get("Content-Length")
                with open(dest, "wb") as out:
                    shutil.copyfileobj(response, out, 1 << 20)
            written = os.path.getsize(dest)
            if declared and declared.isdigit() and written != int(declared):
                raise FetchError(url, "unreachable", f"truncated: {written} of {declared} bytes")
            if written == 0:
                raise FetchError(url, "invalid", "empty download")
            if size is not None and written != size:
                raise FetchError(url, "invalid", f"size {written}, expected {size}")
            digest = util.sha256_file(dest)
            if sha256 and digest != sha256:
                raise FetchError(url, "invalid", f"sha256 {digest}, expected {sha256}")
            if cache_dir:
                try:
                    _to_cache(cache_dir, url, dest, written, digest)
                except OSError as err:
                    util.warn(f"asset cache write failed: {err}")
            return
        except FetchError as err:
            last = err
        except (OSError, http.client.HTTPException) as err:
            last = FetchError(url, "unreachable", f"connection failed mid-download: {err}")
        try:
            os.remove(dest)
        except OSError:
            pass
        if last.status != "unreachable" or attempt + 1 == attempts:
            break
        time.sleep(min(2 ** attempt, 8))
    raise last
