"""Compile-time file hashes let updates keep byte-identical runtime files in place."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


def build_comparer(output: Path) -> Path:
    """Compile the Windows comparison and cleanup workers using the bundled .NET Framework compiler."""
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/platform:x64", "/optimize+",
                    "/r:System.Web.Extensions.dll", f"/out:{output}",
                    str(Path(__file__).with_name("RuntimeComparer.cs"))], check=True)
    cleaner = output.with_name("s2s-runtime-clean.exe")
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/platform:x64", "/optimize+",
                    "/r:System.Web.Extensions.dll", f"/out:{cleaner}",
                    str(Path(__file__).with_name("RuntimeCleaner.cs"))], check=True)
    return output


def write_files(app: Path, output: Path) -> Path:
    def quoted(value: str) -> str:
        return value.replace('"', '""')

    paths = sorted(app.rglob("*"))
    directories = [str(p.relative_to(app)).replace("/", "\\").replace("{", "{{")
                   for p in paths if p.is_dir()]
    manifest = output.with_name("s2s-runtime-hashes.json")
    helper = output.with_name("s2s-runtime-compare.exe")
    layout = output.with_name("s2s-runtime-layout.json")
    cleaner = output.with_name("s2s-runtime-clean.exe")
    lines = ["[Dirs]", *(f'Name: "{{app}}\\{quoted(d)}"' for d in directories), "[Files]",
             f'Source: "{quoted(str(manifest.resolve()))}"; Flags: dontcopy',
             f'Source: "{quoted(str(helper.resolve()))}"; Flags: dontcopy',
             f'Source: "{quoted(str(layout.resolve()))}"; Flags: dontcopy',
             f'Source: "{quoted(str(cleaner.resolve()))}"; Flags: dontcopy']
    entries = []
    file_count = 0
    for path in paths:
        if not path.is_file():
            continue
        relative = str(path.relative_to(app)).replace("/", "\\")
        dest = str(path.relative_to(app).parent).replace("/", "\\")
        dest = "{app}" if dest == "." else "{app}\\" + dest.replace("{", "{{")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        entries.append({"path": relative, "size": path.stat().st_size, "sha256": digest})
        check_path = relative.replace("'", "''")
        lines.append(f'Source: "{quoted(str(path.resolve()))}"; DestDir: "{quoted(dest)}"; '
                     f'Flags: ignoreversion; Check: "NeedsUpdate({file_count}, \'{quoted(check_path)}\')"')
        file_count += 1
    if not file_count:
        raise ValueError("Cannot build an installer from an empty app folder")
    manifest.write_text(json.dumps(entries, ensure_ascii=True), encoding="utf-8")
    actual_dirs = [str(p.relative_to(app)).replace("/", "\\") for p in paths if p.is_dir()]
    names = [entry['path'] for entry in entries] + actual_dirs
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("Runtime contains case-insensitive duplicate paths")
    if any(part.endswith((' ', '.')) for name in names for part in name.split('\\')):
        raise ValueError("Runtime contains ambiguous Windows paths")
    layout.write_text(json.dumps({'files': [e['path'] for e in entries], 'directories': actual_dirs},
                                 ensure_ascii=True), encoding="utf-8")
    lines.insert(0, f"#define RuntimeFileCount {file_count}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return output
