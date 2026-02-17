"""Profile configuration loader for CAO runtime defaults."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

_PROFILE_CACHE: Optional[Dict[str, Any]] = None
_PROFILE_PATH_CACHE: Optional[Path] = None


def get_profile_path(default_home: Path) -> Path:
    """Return the profile path from env override or default location."""
    override = os.getenv("CAO_PROFILE_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return default_home / "profile.json"


def load_profile(default_home: Path) -> Dict[str, Any]:
    """Load profile JSON once per process.

    Invalid JSON or non-object payloads are ignored and treated as empty config.
    """
    global _PROFILE_CACHE, _PROFILE_PATH_CACHE

    profile_path = get_profile_path(default_home)
    if _PROFILE_CACHE is not None and _PROFILE_PATH_CACHE == profile_path:
        return _PROFILE_CACHE

    if not profile_path.exists():
        _PROFILE_CACHE = {}
        _PROFILE_PATH_CACHE = profile_path
        return _PROFILE_CACHE

    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            _PROFILE_CACHE = payload
        else:
            logger.warning("CAO profile at %s is not a JSON object; ignoring", profile_path)
            _PROFILE_CACHE = {}
    except Exception as exc:
        logger.warning("Failed to load CAO profile at %s: %s", profile_path, exc)
        _PROFILE_CACHE = {}

    _PROFILE_PATH_CACHE = profile_path
    return _PROFILE_CACHE


def reset_profile_cache() -> None:
    """Reset profile cache (used by tests)."""
    global _PROFILE_CACHE, _PROFILE_PATH_CACHE
    _PROFILE_CACHE = None
    _PROFILE_PATH_CACHE = None


def get_profile_value(profile: Dict[str, Any], path: Iterable[str], default: Any = None) -> Any:
    """Get a nested value from profile by path segments."""
    cursor: Any = profile
    for segment in path:
        if not isinstance(cursor, dict) or segment not in cursor:
            return default
        cursor = cursor[segment]
    return cursor
