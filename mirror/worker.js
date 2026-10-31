/**
 * pica-nest — Cloudflare Worker mirror.
 *
 * A proxy with a cache, holding no package list of its own: `r/index.json`
 * says which backend indexes exist and where, the backend indexes say which
 * artifacts exist and what they hash to. Both are read from the repository on
 * a cache miss, so adding a package never needs a redeploy.
 *
 *   /  /index.json  /r  /r/  /r/index.json   entry descriptor
 *   /<path>  /r/<path>                        a backend index the descriptor declares,
 *                                             download_url re-addressed to this origin
 *   /releases/<tag>/<file>                    a release asset of this repository
 *
 * Indexes are served with every artifact this worker can proxy re-addressed
 * through the last route, so a device that can reach the worker can install
 * everything it lists. The files in Git keep the GitHub address. An asset is
 * cached under its SHA-256 from the index: a filename can be republished with
 * new bytes, a hash cannot.
 *
 * Configuration (wrangler.toml [vars]): GITHUB_REPO = "owner/name", BRANCH.
 */

export const JSON_TTL = 60;
export const ASSET_TTL = 604800;
export const ASSET_UNKNOWN_TTL = 300;
const USER_AGENT = "pica-nest-mirror/2";

const DESCRIPTOR = "index.json";
/** A manager path as the client accepts it: plain segments, ending in .json. */
const INDEX_SHAPE = /^(?:[A-Za-z0-9._-]+\/)*[A-Za-z0-9._-]+\.json$/;
function isIndexPath(path) {
  return INDEX_SHAPE.test(path) && !path.split("/").some((segment) => /^\.+$/.test(segment));
}
const ASSET_NAME = /^[A-Za-z0-9][A-Za-z0-9@._+-]*\.pkg\.tar\.gz$/;
const RELEASE_TAG = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const PASSTHROUGH = ["content-type", "content-length", "etag", "last-modified", "accept-ranges"];

class HttpError extends Error {
  constructor(message, status = 502) {
    super(message);
    this.status = status;
  }
}

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "access-control-allow-origin": "*",
      "content-type": "application/json; charset=utf-8",
      "cache-control": status === 200 ? `public, max-age=${JSON_TTL}` : "no-store",
      ...extra,
    },
  });
}

function rawUrl(env, relative) {
  if (relative !== DESCRIPTOR && !isIndexPath(relative)) throw new HttpError(`refusing ${relative}`, 500);
  return `https://raw.githubusercontent.com/${env.GITHUB_REPO}/${env.BRANCH || "main"}/r/${relative}`;
}

/** Read one JSON file under r/, through the cache. Failures are never cached. */
async function readJson(env, cache, origin, relative) {
  const key = new Request(new URL(`/__cache__/r/${relative}`, origin));
  const hit = await cache.match(key);
  if (hit) {
    try {
      return await hit.json();
    } catch {
      // A damaged entry is refetched rather than served.
    }
  }
  const upstream = await fetch(rawUrl(env, relative), { headers: { "user-agent": USER_AGENT } });
  if (!upstream.ok) {
    throw new HttpError(`upstream r/${relative}: ${upstream.status}`, upstream.status === 404 ? 404 : 502);
  }
  const text = await upstream.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    throw new HttpError(`upstream r/${relative} is not JSON`);
  }
  await cache.put(key, new Response(text, { headers: { "cache-control": `max-age=${JSON_TTL}` } }));
  return body;
}

/** The backend index paths the descriptor declares. Nothing else is proxied. */
export function declaredPaths(descriptor) {
  const managers = descriptor && typeof descriptor.managers === "object" ? descriptor.managers : {};
  const paths = new Set();
  for (const value of Object.values(managers || {})) {
    if (value && typeof value.path === "string" && isIndexPath(value.path) && value.path !== DESCRIPTOR) {
      paths.add(value.path);
    }
  }
  return paths;
}

/** `{tag, name}` when `url` is a release asset of this repository named `filename`. */
export function releaseAsset(url, filename, env) {
  if (typeof url !== "string") return null;
  const prefix = `https://github.com/${env.GITHUB_REPO}/releases/download/`;
  if (!url.startsWith(prefix)) return null;
  const [tag, name, ...rest] = url.slice(prefix.length).split("/");
  if (rest.length || name !== filename || !RELEASE_TAG.test(tag || "") || !ASSET_NAME.test(name || "")) return null;
  return { tag, name };
}

/** The index as this origin's clients should read it. Only download_url changes. */
export function rewriteIndex(index, env, origin) {
  if (!index || !Array.isArray(index.packages)) return index;
  return {
    ...index,
    packages: index.packages.map((entry) => {
      const asset = releaseAsset(entry && entry.download_url, entry && entry.filename, env);
      return asset ? { ...entry, download_url: `${origin}/releases/${asset.tag}/${asset.name}` } : entry;
    }),
  };
}

async function findEntry(env, cache, origin, name) {
  try {
    const descriptor = await readJson(env, cache, origin, DESCRIPTOR);
    for (const path of declaredPaths(descriptor)) {
      const index = await readJson(env, cache, origin, path);
      const entry = (index.packages || []).find((item) => item && item.filename === name);
      if (entry) return entry;
    }
  } catch {
    // Without the index the asset still resolves; only the cache key is weaker.
  }
  return null;
}

async function serveAsset(request, env, cache, origin, tag, name) {
  const entry = await findEntry(env, cache, origin, name);
  const sha = entry && typeof entry.sha256 === "string" ? entry.sha256.toLowerCase() : "";
  const keyUrl = new URL(`/__cache__/asset/${tag}/${name}`, origin);
  keyUrl.searchParams.set("sha256", sha || "unknown");
  const key = new Request(keyUrl);

  let response = await cache.match(key);
  const status = response ? "hit" : "miss";
  if (!response) {
    const upstream = await fetch(`https://github.com/${env.GITHUB_REPO}/releases/download/${tag}/${name}`, {
      redirect: "follow",
      headers: { "user-agent": USER_AGENT },
    });
    if (!upstream.ok) {
      return json({ error: `upstream ${upstream.status} for ${name}` }, upstream.status === 404 ? 404 : 502);
    }
    const headers = new Headers({ "access-control-allow-origin": "*" });
    for (const header of PASSTHROUGH) {
      const value = upstream.headers.get(header);
      if (value) headers.set(header, value);
    }
    headers.set("cache-control", sha ? `public, max-age=${ASSET_TTL}, immutable` : `public, max-age=${ASSET_UNKNOWN_TTL}`);
    response = new Response(upstream.body, { status: 200, headers });
    if (sha) await cache.put(key, response.clone());
  }
  const headers = new Headers(response.headers);
  headers.set("x-pica-cache", status);
  return new Response(request.method === "HEAD" ? null : response.body, { status: 200, headers });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const cache = caches.default;
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET, HEAD, OPTIONS",
          "access-control-max-age": "86400",
        },
      });
    }
    if (request.method !== "GET" && request.method !== "HEAD") return json({ error: "GET and HEAD only" }, 405);
    try {
      if (["/", "/index.json", "/r", "/r/", "/r/index.json"].includes(path)) {
        return json(await readJson(env, cache, url.origin, DESCRIPTOR));
      }
      const release = path.match(/^\/releases\/([^/]+)\/([^/]+)$/);
      if (release) {
        const [, tag, name] = release;
        if (!RELEASE_TAG.test(tag) || !ASSET_NAME.test(name) || name.length > 200) {
          return json({ error: `not an asset: ${path}` }, 404);
        }
        return await serveAsset(request, env, cache, url.origin, tag, name);
      }
      const relative = path.replace(/^\/(?:r\/)?/, "");
      if (isIndexPath(relative)) {
        const descriptor = await readJson(env, cache, url.origin, DESCRIPTOR);
        if (!declaredPaths(descriptor).has(relative)) return json({ error: `no such index: ${relative}` }, 404);
        const index = await readJson(env, cache, url.origin, relative);
        return json(rewriteIndex(index, env, url.origin));
      }
      return json({ error: `no such route: ${path}` }, 404);
    } catch (err) {
      return json({ error: err.message || "upstream error" }, err instanceof HttpError ? err.status : 502);
    }
  },
};
