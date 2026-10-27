"""Compile-time file hashes let updates keep byte-identical runtime files in place."""

from __future__ import annotations

import hashlib
from pathlib import Path


def write_files(app: Path, output: Path) -> Path:
    def quoted(value: str) -> str:
        return value.replace('"', '""')

    paths = sorted(app.rglob("*"))
    directories = [str(p.relative_to(app)).replace("/", "\\").replace("{", "{{")
                   for p in paths if p.is_dir()]
    lines = ["[Dirs]", *(f'Name: "{{app}}\\{quoted(d)}"' for d in directories), "[Files]"]
    file_count = 0
    for path in paths:
        if not path.is_file():
            continue
        relative = str(path.relative_to(app)).replace("/", "\\")
        dest = str(path.relative_to(app).parent).replace("/", "\\")
        dest = "{app}" if dest == "." else "{app}\\" + dest.replace("{", "{{")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        check_path = relative.replace("'", "''")
        lines.append(f'Source: "{quoted(str(path.resolve()))}"; DestDir: "{quoted(dest)}"; '
                     f'Flags: ignoreversion; Check: "NeedsUpdate(\'{quoted(check_path)}\', \'{digest}\')"')
        file_count += 1
    if not file_count:
        raise ValueError("Cannot build an installer from an empty app folder")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return output
