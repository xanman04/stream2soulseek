const API = "https://api.github.com";
const API_VERSION = "2022-11-28";
const JSON_LIMIT = 1_000_000;
const DOWNLOAD_TIMEOUT_MS = 10 * 60_000;
const REDIRECT_HOSTS = new Set([
  "release-assets.githubusercontent.com",
  "objects.githubusercontent.com",
  "github-releases.githubusercontent.com",
]);

const json = (value, status = 200, headers = {}) => Response.json(value, {
  status,
  headers: { "Cache-Control": "no-store", ...headers },
});

function configuredRepo(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(value)) return null;
  const [owner, repo] = value.split("/");
  if (owner === "." || owner === ".." || repo === "." || repo === "..") return null;
  return { owner, repo };
}

function githubHeaders(token, accept = "application/vnd.github+json") {
  return {
    Accept: accept,
    Authorization: `Bearer ${token}`,
    "X-GitHub-Api-Version": API_VERSION,
    "User-Agent": "S2S-Sponsor-Worker",
  };
}

async function readJsonBounded(response) {
  const length = Number(response.headers.get("content-length"));
  if (Number.isFinite(length) && length > JSON_LIMIT) throw new Error("upstream");
  if (!response.body) throw new Error("upstream");
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > JSON_LIMIT) {
        await reader.cancel();
        throw new Error("upstream");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const all = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { all.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder().decode(all));
}

async function githubJson(fetchImpl, url, token, accept) {
  const response = await fetchImpl(url, {
    headers: githubHeaders(token, accept),
    redirect: "manual",
    signal: AbortSignal.timeout(12_000),
  });
  if (!response.ok) {
    const error = new Error("upstream");
    error.status = response.status;
    throw error;
  }
  return readJsonBounded(response);
}

async function identity(fetchImpl, userToken) {
  if (!userToken) return null;
  const result = await githubJson(fetchImpl, `${API}/user`, userToken);
  return typeof result?.login === "string" ? result.login : null;
}

async function sponsored(fetchImpl, env, userToken, login) {
  if (!env.SPONSOR_LOGIN || !login) return false;
  if (login.toLowerCase() === env.SPONSOR_LOGIN.toLowerCase()) return true;
  const query = `query SponsorCheck($login: String!) {
    viewer { login }
    user(login: $login) {
      sponsorshipForViewerAsSponsor(activeOnly: true) {
        isActive
        isOneTimePayment
        tier { monthlyPriceInDollars }
      }
    }
  }`;
  const response = await fetchImpl("https://api.github.com/graphql", {
    method: "POST",
    headers: { ...githubHeaders(userToken, "application/json"), "Content-Type": "application/json" },
    body: JSON.stringify({ query, variables: { login: env.SPONSOR_LOGIN } }),
    redirect: "manual",
    signal: AbortSignal.timeout(12_000),
  });
  if (!response.ok) throw new Error("upstream");
  const payload = await readJsonBounded(response);
  if (payload?.errors?.length || payload?.data?.viewer?.login?.toLowerCase() !== login.toLowerCase()) {
    throw new Error("upstream");
  }
  const s = payload?.data?.user?.sponsorshipForViewerAsSponsor;
  return Boolean(s?.isActive && !s.isOneTimePayment && Number(s?.tier?.monthlyPriceInDollars) >= 1);
}

async function privateRepo(fetchImpl, env) {
  const repo = configuredRepo(env.PREVIEW_REPO);
  if (!repo || !env.PREVIEW_REPO_TOKEN) throw new Error("unconfigured");
  const metadata = await githubJson(fetchImpl, `${API}/repos/${repo.owner}/${repo.repo}`, env.PREVIEW_REPO_TOKEN);
  if (metadata?.private !== true) throw new Error("not-private");
  return repo;
}

async function creditsResponse(fetchImpl, env) {
  try {
    // Keys are immutable GitHub node IDs, values are explicitly approved public names.
    const consent = JSON.parse(env.SPONSOR_CREDITS_OPT_INS || "{}");
    const ids = Object.keys(consent);
    if (ids.length === 0) return json({ names: [] });
    if (ids.length > 100 || !env.SPONSOR_CREDITS_TOKEN) throw new Error("unconfigured");
    const query = `query Credits($ids: [ID!]!) {
      viewer { login }
      nodes(ids: $ids) {
        id
        ... on Sponsorable {
          sponsorshipForViewerAsSponsorable(activeOnly: true) {
            isActive
            isOneTimePayment
            tier { monthlyPriceInCents isCustomAmount }
          }
        }
      }
    }`;
    const response = await fetchImpl(`${API}/graphql`, {
      method: "POST",
      headers: { ...githubHeaders(env.SPONSOR_CREDITS_TOKEN), "Content-Type": "application/json" },
      body: JSON.stringify({ query, variables: { ids } }),
      redirect: "manual",
      signal: AbortSignal.timeout(8_000),
    });
    if (!response.ok) throw new Error("upstream");
    const payload = await readJsonBounded(response);
    if (payload.errors?.length || !env.SPONSOR_LOGIN
        || payload.data?.viewer?.login?.toLowerCase() !== env.SPONSOR_LOGIN.toLowerCase()) {
      throw new Error("upstream");
    }
    const names = new Set();
    for (const node of payload.data.nodes) {
      const s = node?.sponsorshipForViewerAsSponsorable;
      if (s?.isActive !== true || s.isOneTimePayment !== false || s.tier?.monthlyPriceInCents !== 1000
          || s.tier?.isCustomAmount !== false || !Object.hasOwn(consent, node.id)) continue;
      const name = consent[node.id];
      if (typeof name === "string" && name.trim() && name.length <= 80 && !/[\x00-\x1f\x7f]/.test(name)) {
        names.add(name.trim());
      }
    }
    return json({ names: [...names].sort((a, b) => a.localeCompare(b)) });
  } catch {
    return json({ error: "credits_unavailable" }, 503);
  }
}

async function requireSponsor(fetchImpl, env, request) {
  const header = request.headers.get("authorization") || "";
  const match = /^Bearer ([A-Za-z0-9_\-.]+)$/.exec(header);
  if (!match || match[1].length > 512) return { error: json({ error: "unauthorized" }, 401) };
  try {
    const login = await identity(fetchImpl, match[1]);
    if (!login) return { error: json({ error: "unauthorized" }, 401) };
    return { login, allowed: await sponsored(fetchImpl, env, match[1], login) };
  } catch (error) {
    if (error?.status === 401) return { error: json({ error: "unauthorized" }, 401) };
    return { error: json({ error: "verification_unavailable" }, 502) };
  }
}

async function listReleases(fetchImpl, env, repo) {
  const releases = [];
  for (let page = 1; page <= 10; page++) {
    const items = await githubJson(fetchImpl,
      `${API}/repos/${repo.owner}/${repo.repo}/releases?per_page=100&page=${page}`,
      env.PREVIEW_REPO_TOKEN);
    if (!Array.isArray(items)) throw new Error("upstream");
    releases.push(...items.filter((r) => r && typeof r === "object"));
    if (items.length < 100) break;
  }
  return releases;
}

function installerName(name) {
  return typeof name === "string" && /^S2S-[A-Za-z0-9._+-]+\.(?:zip|exe|dmg|pkg)$/i.test(name);
}

async function releasesResponse(fetchImpl, env, url, login) {
  let repo;
  try { repo = await privateRepo(fetchImpl, env); }
  catch { return json({ error: "preview_unavailable" }, 502); }
  try {
    const releases = await listReleases(fetchImpl, env, repo);
    const body = releases
      .filter((release) => release.prerelease === true && release.draft !== true)
      .map((release) => ({
        id: Number.isSafeInteger(release.id) ? release.id : null,
        tag_name: typeof release.tag_name === "string" ? release.tag_name : "",
        name: typeof release.name === "string" ? release.name : "",
        body: typeof release.body === "string" ? release.body : "",
        prerelease: true,
        published_at: typeof release.published_at === "string" ? release.published_at : null,
        html_url: `https://github.com/sponsors/${encodeURIComponent(env.SPONSOR_LOGIN)}`,
        assets: (Array.isArray(release.assets) ? release.assets : [])
          .filter((asset) => Number.isSafeInteger(asset?.id) && installerName(asset?.name))
          .map((asset) => ({
            id: asset.id,
            name: asset.name,
            size: Number.isSafeInteger(asset.size) ? asset.size : null,
            content_type: typeof asset.content_type === "string" ? asset.content_type : null,
            digest: typeof asset.digest === "string" ? asset.digest : null,
            browser_download_url: `${url.origin}/v1/assets/${asset.id}`,
          })),
      }));
    return json(body);
  } catch {
    return json({ error: "preview_unavailable" }, 502);
  }
}

async function assetResponse(fetchImpl, env, request, login, id) {
  if (!/^\d{1,16}$/.test(id) || !env.PREVIEW_REPO_TOKEN) return json({ error: "not_found" }, 404);
  let repo;
  try { repo = await privateRepo(fetchImpl, env); }
  catch { return json({ error: "preview_unavailable" }, 502); }
  let releaseAsset;
  try {
    const releases = await listReleases(fetchImpl, env, repo);
    releaseAsset = releases
      .filter((release) => release.prerelease === true && release.draft !== true)
      .flatMap((release) => Array.isArray(release.assets) ? release.assets : [])
      .find((asset) => asset?.id === Number(id) && installerName(asset?.name));
  } catch {
    return json({ error: "preview_unavailable" }, 502);
  }
  if (!releaseAsset) return json({ error: "not_found" }, 404);
  const assetUrl = `${API}/repos/${repo.owner}/${repo.repo}/releases/assets/${id}`;
  try {
    const metadata = await githubJson(fetchImpl, assetUrl, env.PREVIEW_REPO_TOKEN);
    if (metadata?.id !== Number(id) || metadata?.name !== releaseAsset.name) return json({ error: "not_found" }, 404);
    const upstream = await fetchImpl(assetUrl, {
      headers: githubHeaders(env.PREVIEW_REPO_TOKEN, "application/octet-stream"),
      redirect: "manual",
      signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS),
    });
    if (upstream.status === 302 || upstream.status === 301 || upstream.status === 303 || upstream.status === 307 || upstream.status === 308) {
      const location = upstream.headers.get("location");
      if (!location) return json({ error: "download_unavailable" }, 502);
      const target = new URL(location);
      if (target.protocol !== "https:" || target.username || target.password || target.port || !REDIRECT_HOSTS.has(target.hostname)) {
        return json({ error: "download_unavailable" }, 502);
      }
      const download = await fetchImpl(target.toString(), {
        redirect: "manual",
        signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS),
      });
      if (!download.ok || !download.body || download.status >= 300) return json({ error: "download_unavailable" }, 502);
      return new Response(download.body, {
        headers: {
          "Content-Type": "application/octet-stream",
          "Content-Disposition": `attachment; filename="${metadata.name}"`,
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
        },
      });
    }
    if (!upstream.ok || !upstream.body) return json({ error: "download_unavailable" }, 502);
    return new Response(upstream.body, {
      headers: {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": `attachment; filename="${metadata.name}"`,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch {
    return json({ error: "download_unavailable" }, 502);
  }
}

export function createWorker(fetchImpl = fetch) {
  return {
    async fetch(request, env) {
      const url = new URL(request.url);
      if (url.search) return json({ error: "invalid_request" }, 400);
      if (url.pathname === "/v1/credits") {
        if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405, { Allow: "GET" });
        return creditsResponse(fetchImpl, env);
      }
      if (url.pathname === "/v1/config") {
        if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405, { Allow: "GET" });
        if (!env.GITHUB_CLIENT_ID) return json({ error: "unavailable" }, 503);
        return json({ client_id: env.GITHUB_CLIENT_ID });
      }
      const route = url.pathname === "/v1/entitlement" || url.pathname === "/v1/releases" || /^\/v1\/assets\/\d+$/.test(url.pathname);
      if (!route) return json({ error: "not_found" }, 404);
      if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405, { Allow: "GET" });
      const auth = await requireSponsor(fetchImpl, env, request);
      if (auth.error) return auth.error;
      if (url.pathname === "/v1/entitlement") return json({ login: auth.login, allowed: auth.allowed });
      if (!auth.allowed) return json({ error: "forbidden" }, 403);
      if (url.pathname === "/v1/releases") return releasesResponse(fetchImpl, env, url, auth.login);
      const [, id] = /^\/v1\/assets\/(\d+)$/.exec(url.pathname);
      return assetResponse(fetchImpl, env, request, auth.login, id);
    },
  };
}

export default createWorker();
