"""
Shared state for the auto-scanner.  Lives in /tmp so it survives in-process
restarts (e.g. uncaught exceptions) but resets on machine reboot — that's
fine; the worst case is one duplicate notification after a deploy.
"""

import json
import logging
import os
from pathlib import Path

_STATE_FILE = Path("/tmp/tw_scanner_state.json")
log = logging.getLogger("tw-scanner-state")


def _load() -> dict:
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(d))
    except Exception as exc:
        log.warning(f"could not persist scanner state: {exc}")


def is_enabled() -> bool:
    return _load().get("enabled", True)


def set_enabled(value: bool) -> None:
    d = _load()
    d["enabled"] = bool(value)
    _save(d)


def last_signature() -> list | None:
    return _load().get("last_signature")


def set_last_signature(sig: list) -> None:
    d = _load()
    d["last_signature"] = sig
    _save(d)
