"""Sign the release installers for S2S's in-app updater (run by the release workflow).

    S2S_UPDATE_SIGNING_KEY=<base64 Ed25519 private key> python packaging/sign_release.py <assets folder> <notes.md>

Appends a hidden comment to the release text (notes.md) with each installer's name, SHA-256 and base64 signature;
the release page then lists only the installers. The signed message is the installer's name and SHA-256 (see
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
    from s2s.updater import signature_block, signed_message, verify_signature

    secret = os.environ.get("S2S_UPDATE_SIGNING_KEY", "").strip()
    if not secret:
        raise SystemExit("S2S_UPDATE_SIGNING_KEY is not set: add it under the repository's Actions secrets.")
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret))
    folder, notes = Path(sys.argv[1]), Path(sys.argv[2])
    installers = sorted([*folder.glob("S2S-*-setup.exe"), *folder.glob("S2S-*.dmg")])
    if not installers:
        raise SystemExit(f"no S2S installers in {folder}")
    entries = {}
    for installer in installers:
        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        signature = base64.b64encode(key.sign(signed_message(installer.name, digest))).decode()
        if not verify_signature(installer.name, digest, signature, PUBLIC_KEY_B64):
            raise SystemExit("the signing key does not match the public key built into S2S (s2s/update_key.py)")
        entries[installer.name] = (digest, signature)
        print(f"signed {installer.name}")
    rows = "\n".join(f"| `{name}` | `{digest}` |" for name, (digest, _sig) in sorted(entries.items()))
    checksums = (f"\n\n<details><summary>SHA-256 checksums</summary>\n\n| File | SHA-256 |\n| --- | --- |\n{rows}\n\n"
                 "</details>")
    notes.write_text(notes.read_text(encoding="utf-8").rstrip() + checksums + signature_block(entries),
                     encoding="utf-8")


if __name__ == "__main__":
    main()
