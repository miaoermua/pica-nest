#!/usr/bin/env python3
"""Serve the pica source from this machine, for devices that cannot reach GitHub.

    /r/index.json            the entry descriptor, unchanged
    /r/<path>                each backend index it declares, download_url pointing here
    /releases/<tag>/<file>   the artifact, downloaded once, verified, cached

Only `download_url` is rewritten, so `sha256` still describes the bytes the
device receives. Point the device at `http://<this machine>:<port>/r`; pica
accepts plain HTTP for private addresses without `allow_insecure_http`.

    python3 mirror/serve.py
    python3 mirror/serve.py --port 9000 --advertised 192.168.1.10
"""

import argparse
import hashlib
import http.server
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ROOT = "https://raw.githubusercontent.com/miaoermua/pica-nest/main/r"
USER_AGENT = "pica-nest-lan-mirror/2"
ASSET = re.compile(r"^/releases/([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9@._+-]*\.pkg\.tar\.gz)$")


def lan_address():
    for probe in ("192.168.1.1", "10.0.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 80))
                return sock.getsockname()[0]
        except OSError:
            continue
    return "127.0.0.1"


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 18), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Mirror:
    def __init__(self, root, base, cache_dir):
        self.root = root.rstrip("/")
        self.base = base
        self.cache_dir = cache_dir
        self.descriptor = {}
        self.indexes = {}  # path -> document
        self.entries = {}  # filename -> entry
        os.makedirs(cache_dir, exist_ok=True)

    def fetch_json(self, relative, attempts=3):
        last = None
        for attempt in range(attempts):
            try:
                request = urllib.request.Request(f"{self.root}/{relative}", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except (urllib.error.URLError, OSError, ValueError) as err:
                last = err
                time.sleep(1 + attempt)
        raise last

    def refresh(self):
        self.descriptor = self.fetch_json("index.json")
        indexes, entries = {}, {}
        for value in (self.descriptor.get("managers") or {}).values():
            relative = value.get("path", "")
            if not relative or ".." in relative.split("/"):
                continue
            document = self.fetch_json(relative)
            indexes[relative] = document
            for entry in document.get("packages") or []:
                entries.setdefault(entry.get("filename"), entry)
        self.indexes, self.entries = indexes, entries

    def rewritten(self, relative):
        document = json.loads(json.dumps(self.indexes[relative]))
        for entry in document.get("packages") or []:
            url = entry.get("download_url") or ""
            match = re.search(r"/releases/download/([^/]+)/([^/]+)$", url)
            if match and match.group(2) == entry.get("filename"):
                entry["download_url"] = f"{self.base}/releases/{match.group(1)}/{match.group(2)}"
        return document

    def artifact(self, name):
        entry = self.entries.get(name)
        if entry is None or not entry.get("download_url"):
            raise KeyError(name)
        target = os.path.join(self.cache_dir, name)
        if os.path.isfile(target) and sha256_of(target) == entry.get("sha256"):
            return target
        partial = target + ".part"
        last = None
        for attempt in range(3):
            try:
                request = urllib.request.Request(entry["download_url"], headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=120) as response, open(partial, "wb") as out:
                    while chunk := response.read(1 << 18):
                        out.write(chunk)
                if sha256_of(partial) != entry.get("sha256"):
                    raise ValueError(f"{name}: sha256 does not match the index")
                os.replace(partial, target)
                return target
            except (urllib.error.URLError, OSError, ValueError) as err:
                last = err
                time.sleep(1 + attempt)
            finally:
                if os.path.exists(partial):
                    os.remove(partial)
        raise last


def make_handler(mirror):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003 - the base class names it
            sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

        def reply(self, status, body, content_type, head=False):
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def reply_json(self, status, value, head=False):
            self.reply(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", head)

        def do_HEAD(self):
            self.route(head=True)

        def do_GET(self):
            self.route(head=False)

        def route(self, head):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/r", "/r/", "/r/index.json"):
                try:
                    mirror.refresh()
                except (urllib.error.URLError, OSError, ValueError) as err:
                    return self.reply_json(502, {"error": f"upstream: {err}"}, head)
                return self.reply_json(200, mirror.descriptor, head)
            if path.startswith("/r/"):
                relative = path[3:]
                if relative not in mirror.indexes:
                    return self.reply_json(404, {"error": f"no such index: {relative}"}, head)
                return self.reply_json(200, mirror.rewritten(relative), head)
            match = ASSET.match(path)
            if not match:
                return self.reply_json(404, {"error": f"no such route: {path}"}, head)
            try:
                target = mirror.artifact(match.group(2))
            except KeyError:
                return self.reply_json(404, {"error": f"not in any index: {match.group(2)}"}, head)
            except (urllib.error.URLError, OSError, ValueError) as err:
                return self.reply_json(502, {"error": str(err)}, head)
            self.send_response(200)
            self.send_header("content-type", "application/gzip")
            self.send_header("content-length", str(os.path.getsize(target)))
            self.end_headers()
            if not head:
                with open(target, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 18), b""):
                        self.wfile.write(chunk)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Serve the pica source over the LAN.")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="upstream source root (the r/ directory)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--advertised", help="address devices use; defaults to the LAN address")
    parser.add_argument("--cache-dir", default=".mirror-cache")
    args = parser.parse_args()

    base = f"http://{args.advertised or lan_address()}:{args.port}"
    mirror = Mirror(args.root, base, args.cache_dir)
    mirror.refresh()
    managers = ", ".join(mirror.descriptor.get("managers") or {}) or "none"
    print(f"upstream : {mirror.root}")
    print(f"artifacts: {len(mirror.entries)} ({managers})")
    print(f"\n设备上执行 pica setup，软件源根地址填：{base}/r\n", flush=True)
    server = http.server.ThreadingHTTPServer((args.bind, args.port), make_handler(mirror))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
