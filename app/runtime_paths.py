"""Per-user writable state paths; importing this module never creates files."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def data_dir(*, create: bool = False) -> Path:
    """Resolve the state directory, creating it only when explicitly requested.

    INVISIBLE_TERRAIN_DATA_DIR overrides the platform default. This deliberately
    keeps favorites, logs, and desktop recovery journals outside the checkout.
    """
    override = os.environ.get("INVISIBLE_TERRAIN_DATA_DIR", "").strip()
    if override:
        directory = Path(override).expanduser()
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        directory = (Path(local) if local else Path.home() / "AppData" / "Local") / "InvisibleTerrain"
    else:
        state = os.environ.get("XDG_STATE_HOME", "").strip()
        directory = (Path(state) if state else Path.home() / ".local" / "state") / "invisible-terrain"
    directory = directory.resolve()
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def state_path(name: str, *, create: bool = False) -> Path:
    """Return a single state filename; ``create`` creates only its directory."""
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or name != name.strip() or name.endswith(".")
            or any(character in '<>:"/\\|?*' or ord(character) < 32 for character in name)):
        raise ValueError("State name must be a plain filename, not a path")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}
    if name.split(".", 1)[0].upper() in reserved:
        raise ValueError("State name must not be a reserved Windows device name")
    return data_dir(create=create) / name
