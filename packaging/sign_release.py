"""Sign the release archives for S2S's in-app updater (run by the release workflow).

    S2S_UPDATE_SIGNING_KEY=<base64 Ed25519 private key> python packaging/sign_release.py <assets folder>

Writes `<archive>.sig` (base64) next to every S2S-*.zip. The signed message is the archive's name and SHA-256 (see
s2s.updater.signed_message); the app accepts an update only when this signature matches its built-in public key.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from s2s.update_key import PUBLIC_KEY_B64
    from s2s.updater import signed_message, verify_signature

    secret = os.environ.get("S2S_UPDATE_SIGNING_KEY", "").strip()
    if not secret:
        raise SystemExit("S2S_UPDATE_SIGNING_KEY is not set: add it under the repository's Actions secrets.")
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret))
    folder = Path(sys.argv[1])
    archives = sorted(folder.glob("S2S-*.zip"))
    if not archives:
        raise SystemExit(f"no S2S-*.zip archives in {folder}")
    for archive in archives:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        signature = base64.b64encode(key.sign(signed_message(archive.name, digest))).decode()
        if not verify_signature(archive.name, digest, signature, PUBLIC_KEY_B64):
            raise SystemExit("the signing key does not match the public key built into S2S (s2s/update_key.py)")
        archive.with_name(archive.name + ".sig").write_text(signature + "\n", encoding="utf-8")
        print(f"signed {archive.name}")


if __name__ == "__main__":
    main()
