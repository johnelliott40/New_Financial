"""
Save/load/delete named snapshots of the app's user-entered inputs (macro inputs, accounts,
holdings, manual data overrides) to/from JSON files in saved_states/.

This is a deliberate exception to the "no I/O" rule in CLAUDE.md ground rule 4, isolated here in
one place, same pattern as modules/market_data.py. No calculation logic lives here — live
price/expense ratio are never saved, since they should be re-fetched fresh on load.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

_SAVES_DIR = Path(__file__).resolve().parent.parent / "saved_states"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_").lower()
    return slug or "untitled"


def list_saves(saves_dir: Path = _SAVES_DIR) -> list[dict]:
    """Returns [{"filename", "display_name", "saved_at"}, ...], newest first. Unreadable files are skipped."""
    if not saves_dir.exists():
        return []
    saves = []
    for path in sorted(saves_dir.glob("*.json")):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        saves.append(
            {
                "filename": path.name,
                "display_name": data.get("display_name", path.stem),
                "saved_at": data.get("saved_at", ""),
            }
        )
    saves.sort(key=lambda s: s["saved_at"], reverse=True)
    return saves


def save_state(
    display_name: str,
    macro: dict,
    demographics: dict,
    accounts: list[dict],
    positions: dict[str, list[dict]],
    manual_quotes: dict,
    ticker_universe: dict,
    asset_class_returns: dict,
    tax: dict | None = None,
    projection: dict | None = None,
    target_allocations: dict | None = None,
    target_allocation_standard: dict | None = None,
    target_allocation_overrides: dict | None = None,
    custom_asset_classes: dict | None = None,
    removed_canonical_asset_classes: list | None = None,
    social_security: dict | None = None,
    saves_dir: Path = _SAVES_DIR,
) -> str:
    """
    Writes a snapshot to saved_states/<slug(display_name)>.json, overwriting any existing save
    with the same slug. Returns the filename written.

    ticker_universe ({ticker: asset_class_code}) and asset_class_returns ({code: nominal_return})
    are the user's personal ETF-universe-builder choices — which ticker belongs to which class,
    and any in-session edit to a class's return — per PROJECT_PLAN.md Step 1: "The user's
    ticker→asset-class mapping is personal data: it lives in session state and saved states, not
    in data/."

    `target_allocation_standard`/`target_allocation_overrides` (2026-09-06 UI redesign, NEXT.md
    item 3) — the two new sources `ui/portfolio_tab.py` derives `target_allocations` FROM every
    rerun (a Standard `{ticker: weight}` list applied to every account type, plus only the account
    types explicitly overridden diverging from it). `target_allocations` itself is still saved too,
    for a human-readable snapshot of the effective (derived) allocation — but these two are what
    `ui/sidebar.py` actually restores from on load, falling back to migrating the flat
    `target_allocations` dict into `target_allocation_overrides` for a save written before this
    redesign existed. Both default to `None` (stored as `{}`), same convention as every other
    optional dict here.

    `custom_asset_classes` ({code: {"label": str, "nominal_return": float}}, 2026-08-14, user
    request) — asset classes the user defined in-app beyond data/asset_classes.json's canonical
    list, same personal-data category as the two fields above. Defaults to `None` (stored as `{}`),
    same convention as `target_allocations`, so every pre-existing call site stays valid untouched.

    `removed_canonical_asset_classes` (list of codes, 2026-08-14, user request — asset classes
    should be "addable and removable, the same as an ETF would be") — built-in classes the user
    excluded from their own active set; `ui/sidebar.py` stores this as a plain sorted list (JSON has
    no set type) and converts it back to a `set` on load. Defaults to `None` (stored as `[]`).

    `social_security` (Module F, 2026-08-31) — the new Social Security tab's own personal inputs
    (`{"historical_ss_earnings": {year: dollars}}`), same "personal data, not shared config"
    category and same `None`-defaults-to-`{}` convention as `custom_asset_classes` above.

    `tax` and `projection` are the Tax tab's and Projection tab's own plain-input state (added
    2026-08-09, per the user's request that every tab's inputs — not just Portfolio's — round-trip
    through save/load). Both default to `None` (stored as `{}`), so every pre-existing call site
    (including every fixture in tests/test_save_state.py) stays valid untouched — this module has
    no opinion on either dict's shape; that's `ui/sidebar.py`'s job to gather and restore, same as
    every other section here.
    """
    saves_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_slugify(display_name)}.json"
    payload = {
        "display_name": display_name,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "macro": macro,
        "demographics": demographics,
        "accounts": accounts,
        "positions": positions,
        "manual_quotes": manual_quotes,
        "ticker_universe": ticker_universe,
        "asset_class_returns": asset_class_returns,
        "tax": tax or {},
        "projection": projection or {},
        "target_allocations": target_allocations or {},
        "target_allocation_standard": target_allocation_standard or {},
        "target_allocation_overrides": target_allocation_overrides or {},
        "custom_asset_classes": custom_asset_classes or {},
        "removed_canonical_asset_classes": removed_canonical_asset_classes or [],
        "social_security": social_security or {},
    }
    with open(saves_dir / filename, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return filename


def load_state(filename: str, saves_dir: Path = _SAVES_DIR) -> dict:
    with open(saves_dir / filename, "r") as f:
        return json.load(f)


def delete_state(filename: str, saves_dir: Path = _SAVES_DIR) -> None:
    path = saves_dir / filename
    if path.exists():
        path.unlink()
