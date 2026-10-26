"""Reject local account state and personal media in a standalone app bundle."""

from pathlib import Path, PurePosixPath

PRIVATE_DIRECTORIES = {
    ".git", ".auth", "secrets", "sessions", "browser-profile", "user-data",
    "downloads", "incomplete", "exports", "reviews", "runs", "logs", "tracks",
}
PRIVATE_FILENAMES = {
    "settings.json", "state.sqlite3", "slskd.yml", "cookies", "login data", "web data",
    "history", "credentials.json", "cookies.json", "storage-state.json", "storage_state.json",
}
PRIVATE_SUFFIXES = {
    ".dpapi", ".sqlite", ".sqlite3", ".db", ".mp3", ".flac", ".m4a", ".wav", ".aiff",
    ".aif", ".ogg", ".opus", ".m3u", ".m3u8", ".har", ".p8", ".p12", ".pfx", ".key",
}


def private_paths(names) -> list[str]:
    found = []
    for name in names:
        path = PurePosixPath(str(name).replace("\\", "/"))
        parts = {part.casefold() for part in path.parts}
        filename = path.name.casefold()
        if (parts & PRIVATE_DIRECTORIES or filename in PRIVATE_FILENAMES
                or path.suffix.casefold() in PRIVATE_SUFFIXES
                or filename == ".env" or filename.startswith(".env.")):
            found.append(str(name))
    return sorted(found)


def check_directory(folder: Path) -> None:
    found = private_paths(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())
    if found:
        # Report filenames only, never credential contents.
        raise ValueError("Personal state or media must not be packaged: " + ", ".join(found))
