# S2S Sponsor Worker

This Worker gives the S2S desktop app a public OAuth client ID, verifies the signed-in GitHub account's recurring sponsorship, and serves release metadata and private preview installers to eligible sponsors. It is deliberately read-only. It does not store user tokens or cache entitlement or asset responses.

## Configuration

Set the non-secret Wrangler vars in `wrangler.jsonc` or through the Cloudflare dashboard:

- `GITHUB_CLIENT_ID`: the desktop GitHub OAuth App client ID; this value is public.
- `SPONSOR_LOGIN`: the GitHub account that owns the Sponsors profile and private preview repository.
- `PREVIEW_REPO`: exact `owner/repository` name for the private repository.

Add this secret with Wrangler, never in source control:

```powershell
npx wrangler secret put PREVIEW_REPO_TOKEN
```

`PREVIEW_REPO_TOKEN` should be a fine-grained token scoped to only `PREVIEW_REPO` with **Contents: read**. GitHub's Get a release asset endpoint documents that permission for private assets. The Worker verifies the repository is private before release metadata or asset access and fails closed if that check cannot be made.

The desktop OAuth flow needs to return a user access token with the `read:user` scope. The Worker uses that same token for `GET /user` and to query whether the authenticated viewer has an active sponsorship of `SPONSOR_LOGIN`. GraphQL's returned viewer login must match the identity from `GET /user`. This requires no separate maintainer-side sponsorship token. Validate the private sponsorship query with an actual sponsor account before deployment. The Worker never logs, persists, or returns the user token. Do not put it in a URL or query string.

## API

- `GET /v1/config` returns `{ "client_id": "..." }` without authentication.
- `GET /v1/entitlement` requires `Authorization: Bearer <user GitHub token>` and returns `{ "login": "...", "allowed": true|false }`. The configured owner account is allowed. Other users need an active, recurring sponsorship tier of at least $1/month.
- `GET /v1/releases` applies the same identity and entitlement check, verifies the repository is private, then returns only published prereleases. Drafts and stable releases are omitted. Response fields are allowlisted. Only installer assets whose names match `S2S-*.(zip|exe|dmg|pkg)` are returned, and their download URLs point back to this Worker. The release page URL points to the public Sponsors profile.
- `GET /v1/assets/<numeric asset id>` repeats entitlement and private-repository checks, verifies the ID belongs to an accepted installer asset on a published prerelease, then streams the private asset. The GitHub API redirect is handled manually; the preview repository token is never sent to the storage host.

All API errors are generic. Responses use `Cache-Control: no-store`. Routes are GET-only and reject query strings; there is no CORS policy, arbitrary proxy URL, or token logging. Upstream JSON is bounded to 1 MB and API requests have short timeouts. Asset streaming has a separate ten-minute upstream timeout.

## Local checks and deployment

Node.js 22 or later is required for the built-in test runner. No project dependencies are needed:

```powershell
node --test
npx wrangler dev
```

Before deploying, provide the three non-secret vars and `PREVIEW_REPO_TOKEN`, then validate sponsorship GraphQL access and private asset downloads with real accounts. This repository addition has not been deployed or checked against live credentials. No paid service is configured. Cloudflare account plan and current usage determine billing. Workers Logs are enabled with invocation logs disabled. Traces are disabled because Cloudflare's automatic fetch spans can include signed release-storage query URLs, and the current Wrangler config schema does not expose Cloudflare's `observability.redact_query_string` setting. Keep traces disabled until the deployment is configured to redact query strings through Cloudflare's Worker Script Settings API or Wrangler adds schema support.
