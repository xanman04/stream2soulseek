"""Public half of the S2S update signing key (Ed25519, raw, base64).

The private half is only in the repository's GitHub Actions secret S2S_UPDATE_SIGNING_KEY; the release build signs
each archive with it, and S2S installs an update only when its signature matches this key. Kept in a module without
imports so the release scripts can read it too.
"""

PUBLIC_KEY_B64 = "6GfOWuli7qps+/MfsNizrS7wpRl4L8suoFvbxjmP+HQ="
