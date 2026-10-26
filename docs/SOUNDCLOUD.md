# SoundCloud playlist source

Open **SoundCloud** in the sidebar, enable web sign-in, and choose **Sign in to SoundCloud**.
Sign in on SoundCloud's page inside S2S. After the session is verified, S2S shows a native account
status page. Choose **1. Choose playlist** to browse the account's playlists and preview their tracks.
The remaining search, review, confirmation, download and import steps are shared with the other sources.

If verification fails temporarily, choose **Retry verification** without signing in again. A rejected
session offers **Restart sign-in**. **Cancel sign-in** returns to the account page and preserves any
previously saved connection; a late verification result cannot replace it after cancellation.

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
