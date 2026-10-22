/**
 * Pica Package Repository - Cloudflare Worker (legacy)
 *
 * SUPERSEDED by `worker/index.js`, which is what `wrangler.toml` deploys.
 * Kept for reference only — do not deploy this file. It is kept working so a
 * copy-paste deploy is not silently broken, which it was: the repository name
 * was still the old `pica-app-repo`, so every route 404'd.
 *
 * This worker acts as a proxy and redirector to optimize access to the Pica software source
 * for users in regions with poor GitHub connectivity (e.g., mainland China).
 */

const DEFAULT_GITHUB_USERNAME = 'miaoermua';
const DEFAULT_GITHUB_REPO = 'pica-pkg-repo';
const BRANCH = 'main'; // The branch where r/repo.json is stored
const RELEASE_TAG = 'latest'; // The tag used for the rolling release

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const username = (env && env.GITHUB_USER) || DEFAULT_GITHUB_USERNAME;
    const repo = (env && env.GITHUB_REPO) || DEFAULT_GITHUB_REPO;

    // 1. Intercept index requests: /r/repo.json
    // We proxy this to GitHub's raw content so it's always up-to-date and accessible.
    if (url.pathname === '/r/repo.json') {
      const rawUrl = `https://raw.githubusercontent.com/${username}/${repo}/${BRANCH}/r/repo.json`;
      
      const response = await fetch(rawUrl, {
        headers: {
          'User-Agent': 'Pica-Cloudflare-Worker'
        }
      });

      // Clone response to add CORS headers
      const newResponse = new Response(response.body, response);
      newResponse.headers.set('Access-Control-Allow-Origin', '*');
      newResponse.headers.set('Cache-Control', 'public, max-age=60'); // 1 minute cache
      
      return newResponse;
    }

    // 2. Intercept package requests: /r/<filename>.pkg.tar.gz
    // Redirect to the GitHub Releases download URL
    if (url.pathname.startsWith('/r/') && url.pathname.endsWith('.pkg.tar.gz')) {
      const filename = url.pathname.substring(3); // Remove '/r/'
      // Make sure there are no slashes in the filename to enforce the flat structure
      if (filename.includes('/')) {
        return new Response('Invalid package path', { status: 400 });
      }

      const releaseUrl = `https://github.com/${username}/${repo}/releases/download/${RELEASE_TAG}/${filename}`;
      
      // Perform a 302 redirect so the client downloads directly from GitHub Releases
      // For further acceleration, you could use a GitHub proxy service here.
      return Response.redirect(releaseUrl, 302);
    }

    // Default response for unmatched routes
    return new Response('Pica Package Repository Worker is running.', { status: 200 });
  },
};
