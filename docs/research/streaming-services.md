# Spotify and SoundCloud as playlist sources: research notes

Researched 2026-10-24 from official developer docs and blogs (sources at the end). Nothing here has been
tried against a real account yet. These services change their API rules often, so recheck before building.

Goal: read a user's playlists and track metadata (title, artists, version text, duration) so the existing
Soulseek search, review gate and import can run on them. The tracks themselves are never downloaded from
these services. They plug in through the `PlaylistSource` interface in `src/s2s/sources/`.

## Spotify

### Official Web API (recommended route)

- **Sign-in:** Authorization Code with PKCE. It needs no client secret, so it suits a desktop app. Redirect
  to a loopback address: `http://127.0.0.1:<port>/callback` (or `[::1]`). `localhost` is no longer
  accepted, plain HTTP is allowed only for loopback, and the port may be chosen at sign-in time. The
  sign-in page can open in the embedded Qt WebEngine view, as with Apple. The implicit grant was removed
  in November 2025.
- **Endpoints after the February 2026 changes:**
  - `GET /me/playlists`
  - `GET /playlists/{id}/items`. This was renamed from `/tracks`; the old path now returns 403, and each
    entry's key changed from `track` to `item`.
- **Track fields:** `name`, `artists`, `album` and `duration_ms` were not on the removed list. Spotify
  first removed `external_ids` (ISRC), then reverted that, so whether ISRC still arrives needs a live check.
  ISRC would help matching.
- **Library choice:** use direct `httpx`, as we already do for Apple and slskd. spotipy (MIT) is
  reportedly fine if you call `playlist_items()` rather than `playlist_tracks()`. But its release notes
  don't confirm the February 2026 changes, and a thin client of our own reacts faster when Spotify changes
  rules again.

### Blockers that matter for us

1. **Followed playlists can't be read:** `GET /playlists/{id}/items` works only for playlists the user
   owns or collaborates on. Anyone else's playlist, including one you follow, returns metadata without
   items. The workaround: in Spotify, select all tracks and "Add to playlist" to make your own copy.
2. **Development Mode limits** (every app without extended quota):
   - The app owner must have **Spotify Premium**.
   - **5 users per app** for apps created since February 2026, and each one must be added to the app's
     allowlist by email.
   - Up to 25 Client IDs per developer since July 2026. The July update didn't mention the 5-user cap.
   - Quota is shared per developer account.
3. **Extended quota is out of reach.** It reportedly needs a launched business with about 250k monthly users.

**Effect on sharing with friends:** either you register one app and add up to 5 friends by email (you need
Premium), or each friend registers their own app and enters its Client ID in the app's settings (each
friend then needs Premium). The second option scales to any number of friends but adds a setup step.

### Unofficial route (not recommended)

Like the Apple approach, you can reuse the Spotify web player's own access token. Spotify actively blocks
this (the token endpoint has been hardened repeatedly), it breaks the terms of service, and it fails without
warning. Keep it as a last resort only.

## SoundCloud

### Official API

- **Registration:** self-service app registration opened in May 2026, but only for **Artist Pro**
  subscribers. SoundCloud also publishes a command-line tool (`sc-api-auth.mjs`) that registers the app and
  prints its credentials, and an OpenAPI spec in `soundcloud/api` on GitHub.
- **Sign-in:** OAuth 2.1 Authorization Code with PKCE (S256), using `secure.soundcloud.com/authorize` and
  `/oauth/token`.
  - Desktop apps are told to use a custom URL scheme as the redirect, for example
    `s2s://soundcloud/callback`. Whether a loopback redirect is accepted needs checking.
  - Access tokens last about 1 hour, and refresh tokens are single-use, so the app must save each new one.
  - Open question: does the token exchange require the client secret? If so, the secret can't be kept safe
    inside a desktop app sent to friends.
- **Endpoints:** `GET /me/playlists`, plus the likes endpoints (listing liked tracks and playlists: check
  the OpenAPI spec). Paging uses `linked_partitioning=true`, returns at most 200 items per page, and follows
  `next_href`. User requests send the header `Authorization: OAuth <token>`.
- **Rate limits:** for client-credentials tokens, 50 tokens per 12 hours per app and 30 per hour per IP.
  Other limits are on SoundCloud's rate-limit page.
- **Terms:** apps must let users disconnect at any time (`POST /disconnect`). The API documentation covers
  streaming with attribution but says nothing about downloading.

### Unofficial route

`soundcloud-v2` (Python) and similar libraries use the public client id that soundcloud.com's own web app
uses. They can read **public** playlists from a URL without signing in. This conflicts with the API terms
and breaks whenever SoundCloud rotates that id. If used at all, keep it behind a setting.

### Matching caveat

SoundCloud titles are uploader-written and messy: `[FREE DL]`, `Premiere:`, the uploader in place of the
artist, and remixes posted by the remixer. Expect more unresolved tracks and flags. Use `metadata_artist` /
`publisher_metadata` when present, and add normalisation rules with their own tests before trusting matches.

## Cheap fallback that works for every service

A **CSV / text import source**: read a file exported by tools like Exportify (Spotify) or TuneMyMusic, or
a pasted "Artist - Title" list. It needs no API keys, Premium or registration, covers followed and
algorithmic playlists, and is quick to build. Its weakness is that durations, and sometimes versions, may be
missing, so matching relies more on titles.

## Suggested order

1. **CSV/text import source:** a day's work, and it unblocks Spotify and SoundCloud playlists right away.
2. **Spotify via PKCE** for owned and collaborative playlists, with a per-user Client ID setting so friends
   can bring their own app.
3. **SoundCloud** via the official API if someone has Artist Pro; otherwise public playlist URLs through the
   unofficial client, behind a setting.
4. Rename `AppleTrack` to a neutral `SourceTrack` with a `source` field before adding the second service.

Each step uses the existing review gate, so no service change can cause an unreviewed download.

## Sources

- Spotify: [February 2026 Dev Mode migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide),
  [Web API changelog Feb 2026](https://developer.spotify.com/documentation/web-api/references/changes/february-2026),
  [Update on developer access (2026-02-06)](https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security),
  [Quota updates (2026-07-23)](https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates),
  [Redirect URIs](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri),
  [OAuth migration reminder (Nov 2025)](https://developer.spotify.com/blog/2025-10-14-reminder-oauth-migration-27-nov-2025),
  [TechCrunch on the Premium requirement](https://techcrunch.com/2026/02/06/spotify-changes-developer-mode-api-to-require-premium-accounts-limits-test-users/embed/),
  [Music Assistant issue on /tracks 403](https://github.com/music-assistant/support/issues/5114)
- SoundCloud: [API guide](https://developers.soundcloud.com/docs/api/guide),
  [API credentials from your terminal (2026-06-16)](https://developers.soundcloud.com/blog/api-credentials-cli-openapi-github),
  [Developer docs](https://developers.soundcloud.com/docs)
