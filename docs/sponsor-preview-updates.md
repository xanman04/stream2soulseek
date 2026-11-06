# Stable and sponsor preview updates

**Stable** is the public update channel and needs no GitHub account. Sponsor Preview is implemented and
disabled in the public build. When enabled, eligible monthly sponsors will be able to opt in from
**Settings → Updates** after GitHub verification.

The app compares semantic versions, so `1.7.0-rc.9 < 1.7.0-rc.10 < 1.7.0`. Stable ignores prereleases even if GitHub labels a tag incorrectly. Preview considers stable releases and authorized preview releases together and offers the newest compatible signed installer. Switching to Stable never silently downgrades an installation. If the installed preview is ahead of Stable, it waits for a newer stable version. This does not guarantee that manually installing an older version can read newer saved data.

## Identity and access

The app starts GitHub's device flow using a public OAuth client ID from the sponsor service. It displays the code and an Open GitHub button. The user enters credentials only on GitHub. Sign-in and network requests run off the Qt thread, and closing the sign-in dialog discards late results rather than saving a new token.

User tokens are kept in the existing per-user SecretStore (Windows DPAPI / macOS Keychain), never in settings JSON or release packages. The local entitlement cache is for display only; it is not permission to fetch a preview. The server verifies access on each metadata and installer request. An expired cache or temporary service outage does not silently change an existing channel preference. Stable checks remain independent.

The Cloudflare Worker in `services/sponsor-worker/` returns preview metadata and streams installers from a private GitHub repository. It fails closed if that repository is public, the identity cannot be checked, or the recurring sponsorship is inactive. One-time contributions do not create ongoing preview access. The configured maintainer can test access without sponsoring themselves.

Only the configured service origin receives the user's authorization header; desktop downloads refuse redirects. The Worker strips repository credentials before following an allowlisted GitHub storage redirect and does not publicly cache responses. All installers still pass the existing Ed25519 verification before staging or being offered for manual installation. When self-installation is unavailable, an authenticated preview download saves the verified installer locally and offers Open folder.

## Activating the preview channel

The public desktop app keeps sponsor login disabled while its service URL is empty. Before enabling it,
verify the OAuth application, Worker deployment and private release repository together.

1. Create a fine-grained GitHub token for the private preview repository only, with **Contents: Read-only**. In the Worker's Cloudflare settings, save it as the secret `PREVIEW_REPO_TOKEN`. Keep the token out of the app, repository, and chat.
2. Seed the private repository with the reviewed S2S source and release workflow. Add the existing updater signing key as the private repository's GitHub Actions secret `S2S_UPDATE_SIGNING_KEY`. The Worker token and signing key serve separate purposes and go in different places.
3. Set the verified Worker origin in a local test build through `src/s2s/sponsor_config.py`. Create a signed release candidate in the private repository following the instructions below. The release workflow builds Windows and macOS installers, verifies their versions and checksums, and signs them for the updater.
4. Test GitHub device sign-in and signed installer delivery with the owner and an active sponsor. Check private sponsorship, non-sponsor and one-time contribution cases, canceled sponsorship, and expired sign-in. Confirm the Stable channel still works during a preview service failure. Run a packaged update on Windows and macOS before enabling this in a public build.
5. Once the live checks pass, set the verified origin in the public build and include the connection controls in the next stable release.

## Producing preview releases

Use a private repository containing the same reviewed app source and release workflow. Configure its signing key through GitHub Actions secrets and keep the same app update verification key. Build preview versions such as `1.7.0-rc.1`; `pyproject.toml`, `s2s.__version__`, the tag, and installer names must agree. Public source publication and private distribution of convenience preview installers are separate concerns; preserve GPL source access and third-party notices for recipients.

The classification job rejects a preview version in a public repository **before** building or uploading CI artifacts. This is necessary because simply making the final release private would not protect installers uploaded by a public Actions run. Private previews are published with `prerelease=true` and `make_latest=false`. Public stable releases retain the existing complete-asset and signature checks and remain Latest.

Switching an app to Preview does not bypass the per-platform signed-installer requirement. Drafts, malformed versions, unsigned assets and mislabelled prereleases are ignored. Keep a reasonable number of recent preview releases; the service bounds pagination and JSON size rather than making unbounded upstream requests.

## Sponsor credits

**Credits** in the footer and under **More** loads approved display names from the sponsor service
when opened. After a sponsor explicitly approves public recognition and the exact name, add their
GitHub GraphQL node ID and display name to the private Worker secret `SPONSOR_CREDITS_OPT_INS`
as a JSON object. The Worker uses `SPONSOR_CREDITS_TOKEN`, a separate maintainer GitHub credential,
to check for an active recurring $10 tier. Test the token with a real consenting sponsor before
relying on it. Only approved names appear in the public response. Remove the opt-in entry on
withdrawal. A sponsorship that ends or changes tiers drops out automatically on the next lookup.

In the private preview repo, run **Actions → Release → Run workflow** with
**Verify the private preview signing key without building installers** selected. The check verifies
that the Actions secret matches the public key embedded in the app without exposing it or publishing
anything. Tagged builds run the same check before building installers.

## Verification

Python tests cover version precedence, stable/preview selection, stable fallback, protected downloads, device-flow states, canceled sign-ins, cache expiry and native settings. The Worker has separate Node tests for sponsorship and private asset authorization. Live GitHub sign-in, Cloudflare runtime/deployment, private installer delivery, and packaged Windows/Mac preview updates must be verified during activation.
