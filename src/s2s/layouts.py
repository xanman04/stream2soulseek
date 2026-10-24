"""Named "layouts" of matching rules, so a regular user can switch rule sets without refilling the form.

"Default" is built in (the stock MatchPolicy), always listed first, and can be neither overwritten nor deleted.
User layouts live in Settings.rule_layouts as name -> MatchPolicy.
"""

from __future__ import annotations

from .models import MatchPolicy, Settings

DEFAULT_NAME = "Default"
MAX_NAME = 40


class LayoutError(ValueError):
    pass


def all_layouts(layouts: dict[str, MatchPolicy]) -> list[tuple[str, MatchPolicy]]:
    """Default first, then the user's layouts sorted by name (case-insensitive)."""
    return [(DEFAULT_NAME, MatchPolicy())] + sorted(layouts.items(), key=lambda kv: kv[0].casefold())


def validate_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise LayoutError("Give the layout a name.")
    if len(name) > MAX_NAME:
        raise LayoutError(f"Layout names are at most {MAX_NAME} characters.")
    if name.casefold() == DEFAULT_NAME.casefold():
        raise LayoutError(f'"{DEFAULT_NAME}" is built in; choose another name.')
    return name


def find(layouts: dict[str, MatchPolicy], name: str) -> str | None:
    """The stored key matching `name` case-insensitively, if any."""
    return next((k for k in layouts if k.casefold() == name.strip().casefold()), None)


def save_layout(layouts: dict[str, MatchPolicy], name: str, policy: MatchPolicy) -> str:
    """Add or replace a user layout (same name in any case replaces it). Returns the stored name."""
    name = validate_name(name)
    old = find(layouts, name)
    if old is not None:
        del layouts[old]
    layouts[name] = policy
    return name


def delete_layout(layouts: dict[str, MatchPolicy], name: str) -> None:
    if name.strip().casefold() == DEFAULT_NAME.casefold():
        raise LayoutError(f'"{DEFAULT_NAME}" is built in and cannot be deleted.')
    key = find(layouts, name)
    if key is None:
        raise LayoutError(f'No layout named "{name}".')
    del layouts[key]


def layout_policy(layouts: dict[str, MatchPolicy], name: str) -> MatchPolicy:
    if name.strip().casefold() == DEFAULT_NAME.casefold():
        return MatchPolicy()
    key = find(layouts, name)
    if key is None:
        raise LayoutError(f'No layout named "{name}".')
    return layouts[key]


def matching_layout(settings: Settings) -> str | None:
    """Name of the layout identical to the active rules (Default wins ties), or None for custom rules."""
    fp = settings.match_policy.fingerprint()
    return next((n for n, p in all_layouts(settings.rule_layouts) if p.fingerprint() == fp), None)
