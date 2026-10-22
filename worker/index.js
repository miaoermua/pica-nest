/**
 * Pica package source — Cloudflare Worker.
 *
 * Deployed from `wrangler.toml` (`main = "worker/index.js"`). `worker/worker.js`
 * is the superseded copy kept for reference and is not routed here.
 *
 * The worker is a proxy with a cache in front of it, nothing more. It holds no
 * package list of its own: the entry descriptor at `r/index.json` says which
 * backend indexes exist and where, `r/repo.json` says which artifacts exist and
 * what they hash to, and both are read from the repository on every miss. Adding
 * a package to the source therefore needs no redeploy here — which is the only
 * reason a proxy this thin is worth having.
 *
 * Routes:
 *
 *   /                          entry descriptor (same as /index.json)
 *   /index.json, /r/index.json entry descriptor
 *   /repo.json, /r/repo.json   schema 2 index — what Pica clients fetch today
 *   /<mgr>/index.json          a backend index, if the descriptor declares it
 *   /r/<mgr>/index.json        the same, addressed under the r/ prefix
 *   /latest/<file>             artifact from the rolling release
 *   /releases/<tag>/<file>     artifact from a named release
 *
 * The last two exist for clients that resolve download URLs themselves. They
 * are also why the cache key carries the artifact's SHA-256: a filename can be
 * republished with new bytes, a hash cannot.
 */

const JSON_TTL = 60;
const ASSET_IMMUTABLE_TTL = 604800;
const ASSET_FALLBACK_TTL = 300;
const MAX_ASSET_NAME = 200;
const USER_AGENT = "pica-pkg-repo-worker/2.0 (+https://github.com/miaoermua/pica-pkg-repo)";

/** The schema 2 index. The one file a client fetches today. */
const REPO_FILE = "repo.json";

/** A release asset name. No separators, so it cannot address a path. */
const ASSET_NAME = /^[A-Za-z0-9@._+~-]+\.pkg\.tar\.gz$/;
/** A release tag as this repository writes them (`26-10-22-09-35-55`). */
const RELEASE_TAG = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
/** A relative JSON path the descriptor may name. */
const JSON_PATH = /^(?:[A-Za-z0-9][A-Za-z0-9._@+~-]*\/)*index\.json$/;
/** Upstream headers worth keeping: content identity, and range support for resumable downloads. */
const PASSTHROUGH_HEADERS = ["content-type", "content-length", "etag", "last-modified", "accept-ranges"];

class UpstreamError extends Error {
  constructor(message, status = 502) {
    super(message);
    this.status = status;
  }
}

function withCors(headers = {}) {
  return { "Access-Control-Allow-Origin": "*", ...headers };
}

function jsonResponse(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: withCors({ "content-type": "application/json; charset=utf-8", ...headers }),
  });
}

function errorResponse(message, status) {
  return jsonResponse({ error: message }, status, { "cache-control": "no-store" });
}

function upstreamJsonUrl(env, relative) {
  // `relative` is either a constant below or a path already matched against
  // JSON_PATH and looked up in the descriptor's declared set, so it cannot
  // carry a scheme or a traversing segment.
  if (relative !== REPO_FILE && !JSON_PATH.test(relative)) {
    throw new UpstreamError(`refusing to proxy ${relative}`, 500);
  }
  return `https://raw.githubusercontent.com/${env.GITHUB_USER}/${env.GITHUB_REPO}/main/r/${relative}`;
}

/**
 * A `download_url` from the index, but only if it points into this repository's
 * releases. The index is fetched from GitHub, so it is not attacker-controlled —
 * but a mirror that rewrites `download_url` to itself would send this worker
 * back to itself, and following that is a request loop, not a download.
 */
function trustedReleaseUrl(candidate, env) {
  if (typeof candidate !== "string" || !candidate) return null;
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" || parsed.hostname !== "github.com") return null;
  if (!parsed.pathname.startsWith(`/${env.GITHUB_USER}/${env.GITHUB_REPO}/releases/`)) return null;
  return parsed.toString();
}

async function readJson(env, cache, origin, relative) {
  const key = new Request(new URL(`/__cache__/json/${relative}`, origin));
  const hit = await cache.match(key);
  if (hit) {
    try {
      return await hit.json();
    } catch {
      // A truncated cache entry is not worth failing on; fall through and refetch.
    }
  }

  const upstream = await fetch(upstreamJsonUrl(env, relative), {
    redirect: "follow",
    headers: { "User-Agent": USER_AGENT },
  });
  if (!upstream.ok) {
    throw new UpstreamError(
      `upstream ${relative} returned ${upstream.status}`,
      upstream.status === 404 || upstream.status === 410 ? 404 : 502,
    );
  }
  const text = await upstream.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    // Never cache this: a captive portal or an outage page would otherwise sit
    // in the cache for the full TTL and be served as a valid index.
    throw new UpstreamError(`upstream ${relative} is not JSON`);
  }
  await cache.put(
    key,
    new Response(text, {
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": `max-age=${JSON_TTL}`,
      },
    }),
  );
  return body;
}

function declaredPaths(descriptor) {
  const managers = descriptor && typeof descriptor === "object" ? descriptor.managers : null;
  if (!managers || typeof managers !== "object") return new Set();
  const paths = new Set();
  for (const value of Object.values(managers)) {
    const path = value && typeof value === "object" ? value.path : null;
    if (typeof path === "string" && JSON_PATH.test(path)) paths.add(path);
  }
  return paths;
}

/**
 * `/apk/index.json` and `/r/apk/index.json` name the same file. Both spellings
 * are accepted because the root a user configures may or may not end in `/r/`,
 * and a mirror should not have to care which.
 */
function backendPath(pathname) {
  if (!pathname.endsWith("/index.json")) return null;
  const stripped = pathname.startsWith("/r/") ? pathname.slice(3) : pathname.slice(1);
  return JSON_PATH.test(stripped) ? stripped : null;
}

function assetTarget(pathname) {
  if (pathname.includes("%")) return null;
  const parts = pathname.split("/").filter(Boolean);
  let name;
  let tag = null;
  if (parts.length === 2 && parts[0] === "latest") {
    name = parts[1];
  } else if (parts.length === 3 && parts[0] === "releases") {
    tag = parts[1];
    name = parts[2];
  } else {
    return null;
  }
  if (tag !== null && !RELEASE_TAG.test(tag)) return null;
  // The name is the whole path segment: no slash survived the split, and the
  // pattern rejects dots-only forms, so nothing here can escape the prefix.
  if (name.length > MAX_ASSET_NAME || !ASSET_NAME.test(name)) return null;
  return { name, tag };
}

function releaseUrl(env, entry, { name, tag }) {
  const base = `https://github.com/${env.GITHUB_USER}/${env.GITHUB_REPO}/releases/download/`;
  if (tag) return `${base}${tag}/${name}`;
  // Prefer what the index says, so the worker follows the publish rule rather
  // than re-deriving it from the filename.
  const declared = trustedReleaseUrl(entry && entry.download_url, env);
  return declared || `${base}latest/${name}`;
}

async function serveAsset(request, env, cache, origin, target) {
  let entry = null;
  try {
    const index = await readJson(env, cache, origin, "repo.json");
    const packages = Array.isArray(index.packages) ? index.packages : [];
    entry = packages.find((pkg) => pkg && pkg.filename === target.name) || null;
  } catch (err) {
    // The artifact still resolves without the index — only the cache key gets
    // weaker — so a GitHub read failure here must not fail the download.
    entry = null;
  }

  const sha = entry && typeof entry.sha256 === "string" ? entry.sha256.toLowerCase() : "";
  const keyUrl = new URL(`/__cache__/asset/${target.name}`, origin);
  keyUrl.searchParams.set("v", sha || "no-hash");
  const key = new Request(keyUrl);

  let response = await cache.match(key);
  let cacheStatus = "hit";

  if (!response) {
    cacheStatus = "miss";
    const upstream = await fetch(releaseUrl(env, entry, target), {
      redirect: "follow",
      headers: { "User-Agent": USER_AGENT },
    });
    if (!upstream.ok) {
      const status = upstream.status === 404 || upstream.status === 410 ? 404 : 502;
      return errorResponse(`upstream returned ${upstream.status} for ${target.name}`, status);
    }

    const headers = new Headers(withCors());
    for (const header of PASSTHROUGH_HEADERS) {
      const value = upstream.headers.get(header);
      if (value) headers.set(header, value);
    }

    const storable = new Response(upstream.body, { status: 200, headers });
    if (sha) {
      storable.headers.set("cache-control", `public, max-age=${ASSET_IMMUTABLE_TTL}, immutable`);
      await cache.put(key, storable.clone());
    } else {
      // Unknown hash: a short TTL and no cache entry, so the next request after
      // the index catches up gets the artifact under its real fingerprint.
      storable.headers.set("cache-control", `public, max-age=${ASSET_FALLBACK_TTL}`);
    }
    response = storable;
  }

  const headers = new Headers(response.headers);
  headers.set("X-Pica-Cache", cacheStatus);
  return new Response(request.method === "HEAD" ? null : response.body, {
    status: response.status,
    headers,
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const cache = caches.default;

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: withCors({
          "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
          "Access-Control-Allow-Headers": "*",
          "Access-Control-Max-Age": "86400",
        }),
      });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return errorResponse("only GET and HEAD are supported", 405);
    }

    try {
      // The unified entry. `/` answers it too: a base URL with or without a
      // trailing slash should both be usable as the source root.
      if (path === "/" || path === "/index.json" || path === "/r/index.json") {
        const descriptor = await readJson(env, cache, url.origin, "index.json");
        return jsonResponse(descriptor, 200, { "cache-control": `public, max-age=${JSON_TTL}` });
      }

      // Schema 2, unchanged: existing clients fetch exactly this and nothing else.
      if (path === "/repo.json" || path === "/r/repo.json") {
        const index = await readJson(env, cache, url.origin, REPO_FILE);
        return jsonResponse(index, 200, { "cache-control": `public, max-age=${JSON_TTL}` });
      }

      const relative = backendPath(path);
      if (relative) {
        const descriptor = await readJson(env, cache, url.origin, "index.json");
        const declared = declaredPaths(descriptor);
        // Anything the descriptor does not name is a 404, not a proxy. Without
        // this the route would be an open reader onto raw.githubusercontent.
        if (!declared.has(relative)) {
          return errorResponse(`no such index: ${relative}`, 404);
        }
        const index = await readJson(env, cache, url.origin, relative);
        return jsonResponse(index, 200, { "cache-control": `public, max-age=${JSON_TTL}` });
      }

      const target = assetTarget(path);
      if (target) {
        return await serveAsset(request, env, cache, url.origin, target);
      }

      return errorResponse(`no such route: ${path}`, 404);
    } catch (err) {
      const status = err instanceof UpstreamError ? err.status : 502;
      return errorResponse(err && err.message ? err.message : "upstream error", status);
    }
  },
};
