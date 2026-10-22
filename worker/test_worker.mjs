/**
 * Tests for `worker/index.js`.
 *
 * Run with `node --test worker/test_worker.mjs`.
 *
 * `caches.default` and `fetch` are the only two things the worker asks of its
 * runtime, so both are replaced here with in-memory stand-ins: a Map for the
 * edge cache and a URL router for upstream. Nothing reaches GitHub, and every
 * test can assert how many upstream requests a route actually made — which is
 * how the caching claims in the worker get proven rather than assumed.
 */
import test from "node:test";
import assert from "node:assert/strict";

import worker from "./index.js";

const ENV = { GITHUB_USER: "miaoermua", GITHUB_REPO: "pica-pkg-repo" };
const ORIGIN = "https://pica.example.workers.dev";
const RAW = "https://raw.githubusercontent.com/miaoermua/pica-pkg-repo/main/r/";
const RELEASES = "https://github.com/miaoermua/pica-pkg-repo/releases/download/";
const ASSET = "demo-1.0-1-amd64-x86_64-apk.pkg.tar.gz";
const SHA = "a".repeat(64);

const DESCRIPTOR = {
  schema: 1,
  name: "Pica",
  managers: {
    apk: { path: "apk/index.json" },
    pica: { path: "pica/index.json" },
  },
};

const APK_INDEX = {
  schema: 1,
  mgr: "apk",
  packages: [
    {
      pkgname: "demo",
      pkgver: "1.0",
      pkgrel: "1",
      pkgmgr: "apk",
      platform: "amd64",
      arch: "x86_64",
      filename: ASSET,
      sha256: SHA,
      download_url: `${RELEASES}latest/${ASSET}`,
    },
  ],
};

function repoIndex(overrides = {}) {
  return {
    schema: 2,
    packages: [{ ...APK_INDEX.packages[0], ...overrides }],
  };
}

class MemoryCache {
  constructor() {
    this.entries = new Map();
  }

  async match(request) {
    const hit = this.entries.get(request.url);
    return hit ? hit.clone() : undefined;
  }

  async put(request, response) {
    this.entries.set(request.url, response.clone());
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

/**
 * Install a cache and an upstream router for one test. `overrides` maps an
 * exact upstream URL to a function returning that response, so a test can make
 * one endpoint fail without disturbing the others.
 */
function setup({ overrides = {}, repo = null, cache = new MemoryCache() } = {}) {
  const calls = [];

  globalThis.caches = { default: cache };
  globalThis.fetch = async (input) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push(url);
    if (overrides[url]) return overrides[url]();
    if (url === `${RAW}index.json`) return json(DESCRIPTOR);
    if (url === `${RAW}repo.json`) return json(repo || repoIndex());
    if (url === `${RAW}apk/index.json`) return json(APK_INDEX);
    if (url === `${RAW}pica/index.json`) return json({ schema: 1, mgr: "pica", packages: [] });
    if (url.startsWith(RELEASES)) {
      return new Response("artifact-bytes", {
        status: 200,
        headers: { "content-type": "application/octet-stream", etag: '"abc"' },
      });
    }
    return new Response("not found", { status: 404 });
  };

  return {
    cache,
    calls,
    get: (path, init) => worker.fetch(new Request(ORIGIN + path, init), ENV),
    upstreamCalls: (fragment) => calls.filter((url) => url.includes(fragment)).length,
  };
}

test("the descriptor answers on / and both index spellings, cached once", async () => {
  const ctx = setup();

  for (const path of ["/", "/index.json", "/r/index.json"]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 200, path);
    assert.equal(response.headers.get("access-control-allow-origin"), "*", path);
    assert.deepEqual(await response.json(), DESCRIPTOR, path);
  }
  assert.equal(ctx.upstreamCalls("index.json"), 1, "the descriptor is fetched once, then served from cache");
});

test("repo.json keeps answering on its old path for existing clients", async () => {
  const ctx = setup();

  for (const path of ["/repo.json", "/r/repo.json"]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 200, path);
    const body = await response.json();
    assert.equal(body.schema, 2, path);
    assert.equal(body.packages[0].filename, ASSET, path);
  }
  assert.equal(ctx.upstreamCalls("repo.json"), 1);
});

test("a declared backend index is reachable in both spellings and cached once", async () => {
  const ctx = setup();

  for (const path of ["/apk/index.json", "/r/apk/index.json"]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 200, path);
    const body = await response.json();
    assert.equal(body.mgr, "apk", path);
    assert.equal(body.packages[0].filename, ASSET, path);
  }
  assert.equal(ctx.upstreamCalls("apk/index.json"), 1);
});

test("a manager the descriptor does not declare is a 404 and is never fetched", async () => {
  const ctx = setup();

  const response = await ctx.get("/opkg/index.json");
  assert.equal(response.status, 404);
  assert.deepEqual(await response.json(), { error: "no such index: opkg/index.json" });
  assert.equal(ctx.upstreamCalls("opkg/index.json"), 0, "an undeclared manager must not be proxied");
});

test("paths that leave the r/ tree resolve nowhere", async () => {
  const ctx = setup();

  for (const path of ["/r/../secret.json", "/apk/../../etc/passwd", "/r/%2e%2e/secret.json"]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 404, path);
  }
  assert.equal(ctx.upstreamCalls("secret"), 0);
  assert.equal(ctx.upstreamCalls("passwd"), 0);
});

test("an asset is fetched once and then served from cache under its hash", async () => {
  const ctx = setup();

  const first = await ctx.get(`/latest/${ASSET}`);
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("x-pica-cache"), "miss");
  assert.equal(first.headers.get("cache-control"), "public, max-age=604800, immutable");
  assert.equal(await first.text(), "artifact-bytes");

  const second = await ctx.get(`/latest/${ASSET}`);
  assert.equal(second.status, 200);
  assert.equal(second.headers.get("x-pica-cache"), "hit");
  assert.equal(await second.text(), "artifact-bytes");
  assert.equal(ctx.upstreamCalls(RELEASES), 1);
});

test("republishing a filename under a new hash bypasses the cached copy", async () => {
  const cache = new MemoryCache();
  const before = setup({ cache });
  await before.get(`/latest/${ASSET}`);

  // Let the 60s metadata TTL lapse, the way it would before any real republish.
  cache.entries.delete(new URL("/__cache__/json/repo.json", ORIGIN).toString());

  const after = setup({ cache, repo: repoIndex({ sha256: "b".repeat(64) }) });
  const response = await after.get(`/latest/${ASSET}`);
  assert.equal(
    response.headers.get("x-pica-cache"),
    "miss",
    "a new hash must not be served from the previous artifact's entry",
  );
  assert.equal(after.upstreamCalls(RELEASES), 1);

  const assetKeys = [...cache.entries.keys()].filter((key) => key.includes("/__cache__/asset/"));
  assert.equal(assetKeys.length, 2, "the two hashes coexist rather than overwrite each other");
});

test("a tagged release path uses the tag, not the rolling latest", async () => {
  const ctx = setup();

  const response = await ctx.get(`/releases/26-10-22-09-35-55/${ASSET}`);
  assert.equal(response.status, 200);
  assert.equal(ctx.upstreamCalls(`${RELEASES}26-10-22-09-35-55/${ASSET}`), 1);
});

test("an asset name that is not a package archive is refused before reaching upstream", async () => {
  const ctx = setup();

  for (const path of [
    `/latest/notes.txt`,
    `/latest/../${ASSET}`,
    `/latest/${ASSET}/..%2f..%2fetc%2fpasswd`,
    `/releases/bad%20tag/${ASSET}`,
  ]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 404, path);
  }
  assert.equal(ctx.upstreamCalls(RELEASES), 0);
});

test("a download_url outside this repository is not followed", async () => {
  const ctx = setup({
    repo: repoIndex({ download_url: "https://evil.example/collect" }),
  });

  const response = await ctx.get(`/latest/${ASSET}`);
  assert.equal(response.status, 200);
  assert.equal(
    ctx.upstreamCalls(RELEASES + "latest/" + ASSET),
    1,
    "the worker falls back to the release it controls",
  );
  assert.equal(ctx.upstreamCalls("evil.example"), 0);
});

test("an upstream failure is reported as 502 and not cached", async () => {
  const ctx = setup({
    overrides: {
      [`${RAW}repo.json`]: () => new Response("boom", { status: 500 }),
    },
  });

  const response = await ctx.get("/repo.json");
  assert.equal(response.status, 502);
  assert.match((await response.json()).error, /returned 500/);
  assert.equal(ctx.cache.entries.size, 0);

  const retry = await ctx.get("/repo.json");
  assert.equal(retry.status, 502);
  assert.equal(ctx.upstreamCalls("repo.json"), 2, "a failure must not be served from cache");
});

test("a missing index is a 404 rather than a 502", async () => {
  const ctx = setup({
    overrides: {
      [`${RAW}pica/index.json`]: () => new Response("gone", { status: 404 }),
    },
  });

  const response = await ctx.get("/pica/index.json");
  assert.equal(response.status, 404);
});

test("a response that is not JSON is refused instead of cached as an index", async () => {
  const ctx = setup({
    overrides: {
      [`${RAW}index.json`]: () => new Response("<html>portal</html>", { status: 200 }),
    },
  });

  const response = await ctx.get("/index.json");
  assert.equal(response.status, 502);
  assert.match((await response.json()).error, /not JSON/);
  assert.equal(ctx.cache.entries.size, 0);
});

test("an artifact the index does not name is still served, but only briefly cached", async () => {
  const ctx = setup({ repo: { schema: 2, packages: [] } });

  const first = await ctx.get(`/latest/${ASSET}`);
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("cache-control"), "public, max-age=300");

  await ctx.get(`/latest/${ASSET}`);
  assert.equal(ctx.upstreamCalls(RELEASES), 2, "without a hash there is nothing to key a permanent entry on");
});

test("HEAD returns the headers without the body", async () => {
  const ctx = setup();

  const response = await ctx.get(`/latest/${ASSET}`, { method: "HEAD" });
  assert.equal(response.status, 200);
  assert.equal(response.body, null);
  assert.equal(response.headers.get("etag"), '"abc"');
});

test("preflight is answered, and other methods are refused", async () => {
  const ctx = setup();

  const preflight = await ctx.get(`/latest/${ASSET}`, {
    method: "OPTIONS",
    headers: { origin: ORIGIN },
  });
  assert.equal(preflight.status, 204);
  assert.match(preflight.headers.get("access-control-allow-methods"), /GET/);

  const posted = await ctx.get(`/latest/${ASSET}`, { method: "POST" });
  assert.equal(posted.status, 405);
});

test("an unknown route is a 404 with a JSON body", async () => {
  const ctx = setup();

  const response = await ctx.get("/nothing/here");
  assert.equal(response.status, 404);
  assert.match((await response.json()).error, /no such route/);
});
