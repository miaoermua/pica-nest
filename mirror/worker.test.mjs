// node --test mirror/worker.test.mjs — no network: cache and upstream are stubs.
import assert from "node:assert/strict";
import { test } from "node:test";

import worker, { declaredPaths, releaseAsset, rewriteIndex } from "./worker.js";

const ENV = { GITHUB_REPO: "owner/nest", BRANCH: "main" };
const ORIGIN = "https://mirror.example";
const RAW = "https://raw.githubusercontent.com/owner/nest/main/r/";
const GH = "https://github.com/owner/nest/releases/download/";
const NAME = "demo-1.0-1-amd64-x86_64-opkg.pkg.tar.gz";
const SHA = "a".repeat(64);

const DESCRIPTOR = { schema: 1, name: "t", managers: { opkg: { path: "opkg/index.json" } } };
const INDEX = {
  schema: 1,
  mgr: "opkg",
  packages: [
    { filename: NAME, sha256: SHA, download_url: `${GH}latest/${NAME}` },
    { filename: "other.pkg.tar.gz", sha256: SHA, download_url: "http://192.168.1.2/other.pkg.tar.gz" },
  ],
};

function setup(routes = {}) {
  const store = new Map();
  const calls = [];
  globalThis.caches = {
    default: {
      async match(request) {
        const hit = store.get(request.url);
        return hit ? hit.clone() : undefined;
      },
      async put(request, response) {
        store.set(request.url, response.clone());
      },
    },
  };
  globalThis.fetch = async (url) => {
    calls.push(url);
    if (routes[url]) return routes[url]();
    if (url === `${RAW}index.json`) return new Response(JSON.stringify(DESCRIPTOR));
    if (url === `${RAW}opkg/index.json`) return new Response(JSON.stringify(INDEX));
    if (url === `${GH}latest/${NAME}`) return new Response("bytes", { headers: { "content-length": "5" } });
    return new Response("nope", { status: 404 });
  };
  const get = (path, method = "GET") => worker.fetch(new Request(`${ORIGIN}${path}`, { method }), ENV);
  return { get, calls, store };
}

test("the descriptor answers on every root spelling", async () => {
  const ctx = setup();
  for (const path of ["/", "/index.json", "/r", "/r/", "/r/index.json"]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 200, path);
    assert.deepEqual(await response.json(), DESCRIPTOR);
  }
  assert.equal(ctx.calls.filter((u) => u === `${RAW}index.json`).length, 1, "cached after the first read");
});

test("a declared index is served with this origin's download addresses", async () => {
  const ctx = setup();
  for (const path of ["/opkg/index.json", "/r/opkg/index.json"]) {
    const body = await (await ctx.get(path)).json();
    assert.equal(body.packages[0].download_url, `${ORIGIN}/releases/latest/${NAME}`);
    assert.equal(body.packages[1].download_url, "http://192.168.1.2/other.pkg.tar.gz", "foreign URLs stay");
  }
});

test("an undeclared path is not proxied", async () => {
  const ctx = setup();
  const response = await ctx.get("/apk/index.json");
  assert.equal(response.status, 404);
  assert.ok(!ctx.calls.includes(`${RAW}apk/index.json`));
});

test("assets are cached under their sha256", async () => {
  const ctx = setup();
  const first = await ctx.get(`/releases/latest/${NAME}`);
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("x-pica-cache"), "miss");
  assert.equal(await first.text(), "bytes");
  const second = await ctx.get(`/releases/latest/${NAME}`);
  assert.equal(second.headers.get("x-pica-cache"), "hit");
  assert.equal(ctx.calls.filter((u) => u.startsWith(GH)).length, 1);
  assert.ok([...ctx.store.keys()].some((k) => k.includes(`sha256=${SHA}`)));
});

test("an asset the index does not know is served but not cached", async () => {
  const unknown = "ghost-1-1-all-pica.pkg.tar.gz";
  const ctx = setup({ [`${GH}latest/${unknown}`]: () => new Response("x") });
  const response = await ctx.get(`/releases/latest/${unknown}`);
  assert.equal(response.status, 200);
  assert.match(response.headers.get("cache-control"), /max-age=300/);
  assert.ok(![...ctx.store.keys()].some((k) => k.includes(unknown)));
});

test("malformed asset paths are refused", async () => {
  const ctx = setup();
  for (const path of ["/releases/latest/../x.pkg.tar.gz", "/releases/latest/x.txt", "/releases/a%2Fb/x.pkg.tar.gz"]) {
    assert.equal((await ctx.get(path)).status, 404, path);
  }
});

test("upstream failures are not cached", async () => {
  let fail = true;
  const ctx = setup({ [`${RAW}index.json`]: () => (fail ? new Response("boom", { status: 500 }) : new Response(JSON.stringify(DESCRIPTOR))) });
  assert.equal((await ctx.get("/")).status, 502);
  fail = false;
  assert.equal((await ctx.get("/")).status, 200);
});

test("helpers", () => {
  assert.deepEqual([...declaredPaths(DESCRIPTOR)], ["opkg/index.json"]);
  assert.deepEqual([...declaredPaths({ managers: { x: { path: "../evil.json" }, y: { path: "index.json" } } })], []);
  assert.deepEqual(releaseAsset(`${GH}t1/${NAME}`, NAME, ENV), { tag: "t1", name: NAME });
  assert.equal(releaseAsset(`${GH}t1/${NAME}`, "different", ENV), null);
  assert.equal(releaseAsset(`https://github.com/else/repo/releases/download/t/${NAME}`, NAME, ENV), null);
  assert.equal(rewriteIndex({ schema: 1 }, ENV, ORIGIN).packages, undefined);
});
