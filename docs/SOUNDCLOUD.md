# SoundCloud playlist source

## Open a public playlist by link (no sign-in)

In **Choose playlist**, paste a public SoundCloud playlist link (`soundcloud.com/<user>/sets/<name>`, from the
playlist's Share menu) next to the Spotify box and choose **Open**. A link-only playlist's secret link works too.
This reads only SoundCloud's public metadata and needs no account.

## Signing in to read your own playlists

Open **SoundCloud** in the sidebar and choose **Sign in to SoundCloud**, then sign in on SoundCloud's page inside
S2S. After the session is verified, S2S shows a native account status page, and **Choose playlist** lists the
account's playlists. The remaining steps are shared with the other sources.

Limits of sign-in inside an app window: Google refuses "Continue with Google" in any embedded browser, and
SoundCloud's bot protection may show a slider challenge. S2S does not disguise its browser to get around either;
use the public-link route above when sign-in is blocked.

If verification fails temporarily, choose **Retry verification** without signing in again. A rejected session
starts a fresh sign-in page.

This integration reads playlist and track metadata through SoundCloud's unofficial website API.
It needs no developer application credentials. It does not request SoundCloud audio, download links or
streaming endpoints, and it does not change the source library. Website API changes may require updates.

## Session storage

The embedded browser uses a temporary profile. S2S accepts only the secure `oauth_token` cookie from
SoundCloud domains, validates it against the account endpoint, then saves the token through the existing
Windows-protected secret store outside the repository. Passwords and browser profiles are not saved.
Sign out, or disable web sign-in, to remove S2S's local SoundCloud token. This does not sign out other apps.

## Playlist and matching behavior

- Lists account-owned playlists, including private playlists accessible to the session. Liked playlists,
  liked tracks, albums and feeds are not included in this first implementation.
- Follows pagination and hydrates track ID placeholders in batches while preserving playlist order and duplicates.
- Reports missing track metadata rather than silently presenting a complete playlist.
- Uses publisher artist metadata where available; otherwise recognizes an explicit `Artist - Title`,
  then falls back to the uploader name. Uploader names can differ from the recording artist.
- Keeps recording/version text intact. Existing matching rules and the explicit download approval gate still apply.

## Verification boundary

Synthetic tests cover API responses, pagination, hydration, version text, session storage, cookie origin,
stale callbacks, navigation and sign-out isolation. A public metadata probe succeeded with dynamically
discovered website settings. Actual account sign-in, private playlists and reuse after a real app restart
still require user testing. No account credentials or audio were used for development probes.

References: [SoundCloud API guide](https://developers.soundcloud.com/docs/api/guide) and
[soundcloud-v2 reference implementation](https://github.com/7x11x13/soundcloud.py).
