import test from "node:test";
import assert from "node:assert/strict";
import { createWorker } from "../index.js";

const env = {
  GITHUB_CLIENT_ID: "public-client-id",
  SPONSOR_LOGIN: "owner",
  PREVIEW_REPO: "owner/private-preview",
  PREVIEW_REPO_TOKEN: "contents-read-secret",
};

function request(path, token = "user-token") {
  return new Request(`https://worker.example${path}`, token === null ? {} : {
    headers: { Authorization: `Bearer ${token}` },
  });
}

function response(value, status = 200, headers = {}) {
  return new Response(typeof value === "string" ? value : JSON.stringify(value), { status, headers });
}

function fetchSequence(steps) {
  const calls = [];
  const fetchImpl = async (input, init = {}) => {
    const url = String(input);
    calls.push({ url, init });
    const step = steps.shift();
    assert.ok(step, `unexpected fetch ${url}`);
    return step(url, init);
  };
  return { fetchImpl, calls, remaining: () => steps.length };
}

const identityStep = (login = "sponsor", status = 200) => () => response({ login }, status);
const sponsorStep = (sponsorship) => (url, init) => {
  assert.equal(url, "https://api.github.com/graphql");
  assert.equal(init.headers.Authorization, "Bearer user-token");
  const body = JSON.parse(init.body);
  assert.equal(body.variables.login, "owner");
  assert.match(body.query, /sponsorshipForViewerAsSponsor\(activeOnly: true\)/);
  assert.doesNotMatch(body.query, /sponsorshipForViewerAsSponsorable/);
  return response({ data: { viewer: { login: "sponsor" }, user: { sponsorshipForViewerAsSponsor: sponsorship } } });
};
const privateRepoStep = (isPrivate = true) => (url, init) => {
  assert.equal(url, "https://api.github.com/repos/owner/private-preview");
  assert.equal(init.headers.Authorization, "Bearer contents-read-secret");
  return response({ private: isPrivate });
};
const prereleaseAsset = (id = 123, extra = {}) => ({
  id: 11,
  draft: false,
  prerelease: true,
  tag_name: "preview-1",
  name: "Preview 1",
  body: "Preview notes",
  published_at: "2026-11-03T12:00:00Z",
  assets: [{ id, name: "S2S-release.zip", size: 3, content_type: "application/zip", digest: "sha256:abc", ...extra }],
});
const releaseListStep = (items) => (url, init) => {
  assert.equal(url, "https://api.github.com/repos/owner/private-preview/releases?per_page=100&page=1");
  assert.equal(init.headers.Authorization, "Bearer contents-read-secret");
  return response(items);
};

test("config is public, GET-only, and never reveals secrets", async () => {
  const worker = createWorker(async () => { throw new Error("unexpected upstream"); });
  const res = await worker.fetch(request("/v1/config", null), env);
  assert.deepEqual(await res.json(), { client_id: "public-client-id" });
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.equal((await worker.fetch(new Request("https://worker.example/v1/config", { method: "POST" }), env)).status, 405);
});

test("malformed or absent bearer auth is refused without upstream calls", async () => {
  let count = 0;
  const worker = createWorker(async () => { count++; throw new Error("unexpected"); });
  for (const authorization of [null, "Basic user-token", "Bearer x y", `Bearer ${"a".repeat(513)}`]) {
    const headers = authorization ? { Authorization: authorization } : {};
    const res = await worker.fetch(new Request("https://worker.example/v1/entitlement", { headers }), env);
    assert.equal(res.status, 401);
  }
  assert.equal(count, 0);
});

test("GitHub rejects an expired user token with HTTP 401", async () => {
  const worker = createWorker(fetchSequence([() => response({ message: "Bad credentials" }, 401)]).fetchImpl);
  const res = await worker.fetch(request("/v1/entitlement"), env);
  assert.equal(res.status, 401);
  assert.deepEqual(await res.json(), { error: "unauthorized" });
});

test("non-sponsor receives entitlement false and cannot list releases", async () => {
  const { fetchImpl, calls } = fetchSequence([identityStep(), sponsorStep(null), identityStep(), sponsorStep(null)]);
  const worker = createWorker(fetchImpl);
  const entitlement = await worker.fetch(request("/v1/entitlement"), env);
  assert.deepEqual(await entitlement.json(), { login: "sponsor", allowed: false });
  assert.equal((await worker.fetch(request("/v1/releases"), env)).status, 403);
  assert.equal(calls.length, 4);
});

test("only active recurring sponsorship of at least one dollar is allowed", async () => {
  const cases = [
    [{ isActive: true, isOneTimePayment: true, tier: { monthlyPriceInDollars: 10 } }, false],
    [{ isActive: true, isOneTimePayment: false, tier: { monthlyPriceInDollars: 0 } }, false],
    [{ isActive: true, isOneTimePayment: false, tier: { monthlyPriceInDollars: 1 } }, true],
  ];
  for (const [s, allowed] of cases) {
    const worker = createWorker(fetchSequence([identityStep(), sponsorStep(s)]).fetchImpl);
    const res = await worker.fetch(request("/v1/entitlement"), env);
    assert.equal((await res.json()).allowed, allowed);
  }
});

test("a private recurring sponsorship is eligible when GitHub returns it to the signed-in sponsor", async () => {
  const worker = createWorker(fetchSequence([
    identityStep(),
    sponsorStep({ isActive: true, isOneTimePayment: false, privacyLevel: "PRIVATE", tier: { monthlyPriceInDollars: 1 } }),
  ]).fetchImpl);
  const res = await worker.fetch(request("/v1/entitlement"), env);
  assert.equal((await res.json()).allowed, true);
});

test("owner account is allowed without a sponsorship query", async () => {
  const { fetchImpl, calls } = fetchSequence([identityStep("owner")]);
  const res = await createWorker(fetchImpl).fetch(request("/v1/entitlement"), env);
  assert.deepEqual(await res.json(), { login: "owner", allowed: true });
  assert.equal(calls.length, 1);
});

test("sponsor GraphQL uses configured owner variable and verifies the authenticated viewer", async () => {
  const maliciousLogin = 'sponsor") { viewer { login } } #';
  const { fetchImpl, calls } = fetchSequence([
    identityStep(maliciousLogin),
    (url, init) => {
      const body = JSON.parse(init.body);
      assert.equal(body.variables.login, "owner");
      assert.equal(init.headers.Authorization, "Bearer user-token");
      return response({ data: { viewer: { login: "not-user" }, user: {} } });
    },
  ]);
  const res = await createWorker(fetchImpl).fetch(request("/v1/entitlement"), env);
  assert.equal(res.status, 502);
  assert.equal(calls.length, 2);
});

test("release listing refuses a repository that is not private", async () => {
  const { fetchImpl, calls } = fetchSequence([identityStep(), sponsorStep({ isActive: true, isOneTimePayment: false, tier: { monthlyPriceInDollars: 1 } }), privateRepoStep(false)]);
  const res = await createWorker(fetchImpl).fetch(request("/v1/releases"), env);
  assert.equal(res.status, 502);
  assert.equal(calls.length, 3);
});

test("release responses return only published prereleases and whitelisted fields", async () => {
  const steps = [
    identityStep(),
    sponsorStep({ isActive: true, isOneTimePayment: false, tier: { monthlyPriceInDollars: 2 } }),
    privateRepoStep(true),
    () => response([
      { id: 9, draft: false, prerelease: false, html_url: "private-url", upload_url: "private-upload-url", assets: [] },
      { id: 10, draft: true, prerelease: true, assets: [{ id: 100, name: "S2S-build.zip" }] },
      { ...prereleaseAsset(101), html_url: "https://github.com/owner/private-preview/releases/11", upload_url: "private-upload-url", zipball_url: "private-zip-url", assets: [{ id: 101, name: "S2S-Preview.exe", size: 55, content_type: "application/zip", digest: "sha256:abc", browser_download_url: "https://github.com/leak" }, { id: 102, name: "notes.txt" }] },
    ]),
  ];
  const res = await createWorker(fetchSequence(steps).fetchImpl).fetch(request("/v1/releases"), env);
  const result = await res.json();
  assert.equal(result.length, 1);
  assert.equal(result[0].prerelease, true);
  assert.equal(result[0].html_url, "https://github.com/sponsors/owner");
  assert.deepEqual(Object.keys(result[0]).sort(), ["assets", "body", "html_url", "id", "name", "prerelease", "published_at", "tag_name"].sort());
  assert.equal(result[0].upload_url, undefined);
  assert.equal(result[0].zipball_url, undefined);
  assert.deepEqual(result[0].assets.map((a) => a.id), [101]);
  assert.equal(result[0].assets[0].browser_download_url, "https://worker.example/v1/assets/101");
});

test("asset delivery rechecks sponsor and private repo, then redirects without forwarding credentials", async () => {
  const calls = [];
  const fetchImpl = async (input, init = {}) => {
    const url = String(input);
    calls.push({ url, init });
    if (url === `${"https://api.github.com"}/user`) return response({ login: "sponsor" });
    if (url === "https://api.github.com/graphql") return response({ data: { viewer: { login: "sponsor" }, user: { sponsorshipForViewerAsSponsor: { isActive: true, isOneTimePayment: false, tier: { monthlyPriceInDollars: 1 } } } } });
    if (url === "https://api.github.com/repos/owner/private-preview") return response({ private: true });
    if (url.endsWith("/releases?per_page=100&page=1")) return response([prereleaseAsset(123)]);
    if (url.endsWith("/releases/assets/123") && init.headers.Accept === "application/vnd.github+json") return response({ id: 123, name: "S2S-release.zip" });
    if (url.endsWith("/releases/assets/123")) return new Response(null, { status: 302, headers: { Location: "https://release-assets.githubusercontent.com/object/file" } });
    if (url.startsWith("https://release-assets.githubusercontent.com/")) {
      assert.equal(init.headers?.Authorization, undefined);
      return new Response(new Uint8Array([1, 2, 3]), { status: 200 });
    }
    throw new Error(`unexpected upstream ${url}`);
  };
  const res = await createWorker(fetchImpl).fetch(request("/v1/assets/123"), env);
  assert.equal(res.status, 200);
  assert.deepEqual([...new Uint8Array(await res.arrayBuffer())], [1, 2, 3]);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.equal(calls.find((c) => c.url.startsWith("https://release-assets"))?.init.headers?.Authorization, undefined);
});

test("assets are not served when upstream redirect host is unsafe", async () => {
  const fetchImpl = async (input, init = {}) => {
    const url = String(input);
    if (url.endsWith("/user")) return response({ login: "sponsor" });
    if (url.endsWith("/graphql")) return response({ data: { viewer: { login: "sponsor" }, user: { sponsorshipForViewerAsSponsor: { isActive: true, isOneTimePayment: false, tier: { monthlyPriceInDollars: 5 } } } } });
    if (url.endsWith("/private-preview")) return response({ private: true });
    if (url.endsWith("/releases?per_page=100&page=1")) return response([prereleaseAsset(77)]);
    if (init.headers.Accept === "application/vnd.github+json") return response({ id: 77, name: "S2S-release.zip" });
    return new Response(null, { status: 302, headers: { Location: "https://attacker.example/asset" } });
  };
  const res = await createWorker(fetchImpl).fetch(request("/v1/assets/77"), env);
  assert.equal(res.status, 502);
});

test("upstream GitHub errors return a generic error without leaking details", async () => {
  const worker = createWorker(fetchSequence([
    () => response({ message: "secret account response" }, 500),
  ]).fetchImpl);
  const res = await worker.fetch(request("/v1/entitlement"), env);
  assert.equal(res.status, 502);
  assert.equal(await res.text(), '{"error":"verification_unavailable"}');
});

test("routes reject query strings and asset route only accepts numeric IDs", async () => {
  let count = 0;
  const worker = createWorker(async () => { count++; throw new Error("unexpected"); });
  const res = await worker.fetch(new Request("https://worker.example/v1/assets/abc?token=anything"), env);
  assert.equal(res.status, 400);
  assert.equal((await worker.fetch(new Request("https://worker.example/v1/entitlement?token=anything", { headers: { Authorization: "Bearer user-token" } }), env)).status, 400);
  assert.equal(count, 0);
});

test("asset ID from draft or non-preview release is refused before metadata lookup", async () => {
  const steps = [
    identityStep(),
    sponsorStep({ isActive: true, isOneTimePayment: false, tier: { monthlyPriceInDollars: 1 } }),
    privateRepoStep(true),
    releaseListStep([
      { id: 1, draft: true, prerelease: true, assets: [{ id: 44, name: "S2S-draft.zip" }] },
      { id: 2, draft: false, prerelease: false, assets: [{ id: 45, name: "S2S-stable.zip" }] },
    ]),
  ];
  const { fetchImpl, calls } = fetchSequence(steps);
  const res = await createWorker(fetchImpl).fetch(request("/v1/assets/44"), env);
  assert.equal(res.status, 404);
  assert.equal(calls.length, 4);
});

test("asset ID from a different preview release is refused before metadata lookup", async () => {
  const { fetchImpl, calls } = fetchSequence([
    identityStep(),
    sponsorStep({ isActive: true, isOneTimePayment: false, tier: { monthlyPriceInDollars: 1 } }),
    privateRepoStep(true),
    releaseListStep([prereleaseAsset(88)]),
  ]);
  const res = await createWorker(fetchImpl).fetch(request("/v1/assets/89"), env);
  assert.equal(res.status, 404);
  assert.equal(calls.length, 4);
});

test("release redirect rejects userinfo and non-default ports", async () => {
  for (const location of [
    "https://user@release-assets.githubusercontent.com/object",
    "https://user:pass@release-assets.githubusercontent.com/object",
    "https://release-assets.githubusercontent.com:444/object",
  ]) {
    const calls = [];
    const fetchImpl = async (input, init = {}) => {
      const url = String(input);
      calls.push(url);
      if (url.endsWith("/user")) return response({ login: "sponsor" });
      if (url.endsWith("/graphql")) return response({ data: { viewer: { login: "sponsor" }, user: { sponsorshipForViewerAsSponsor: { isActive: true, isOneTimePayment: false, tier: { monthlyPriceInDollars: 1 } } } } });
      if (url.endsWith("/private-preview")) return response({ private: true });
      if (url.endsWith("/releases?per_page=100&page=1")) return response([prereleaseAsset(91)]);
      if (url.endsWith("/releases/assets/91") && init.headers.Accept === "application/vnd.github+json") return response({ id: 91, name: "S2S-release.zip" });
      if (url.endsWith("/releases/assets/91")) return new Response(null, { status: 302, headers: { Location: location } });
      throw new Error(`unsafe redirect was followed: ${url}`);
    };
    const res = await createWorker(fetchImpl).fetch(request("/v1/assets/91"), env);
    assert.equal(res.status, 502);
    assert.equal(calls.some((url) => url.startsWith("https://release-assets.githubusercontent.com")), false);
  }
});
