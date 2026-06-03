"""Sync state footer embedded in DMD Hub Public Description.

Format: a single HTML comment (invisible in rendered HTML):

    <!-- stegra-sync:v1:{"route_id":"…","collection_id":"…","modified_at":"…","synced_at":"…"} -->

Any text above the marker is treated as user content (preserved on next write).
If the marker is absent or unparsable, the GPX is considered "unmanaged" and
the sync tool will not modify or delete it.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict

from .models import SYNC_STATE_VERSION, SyncState

# DOTALL: tolerate newlines inside the JSON payload (defensive)
_SENTINEL_RE = re.compile(
    r"<!--\s*stegra-sync:v(?P<ver>\d+):(?P<json>\{.*?\})\s*-->",
    re.DOTALL,
)


def render(user_text: str, state: SyncState) -> str:
    """Append a sentinel comment to user_text, replacing any existing one."""
    stripped = strip(user_text)
    payload = {
        "route_id": state.route_id,
        "collection_id": state.collection_id,
        "modified_at": state.modified_at,
        "synced_at": state.synced_at,
    }
    marker = f"<!-- stegra-sync:v{state.version}:{json.dumps(payload, separators=(',', ':'))} -->"
    sep = "\n" if stripped and not stripped.endswith("\n") else ""
    return f"{stripped}{sep}{marker}"


def parse(text: str) -> tuple[SyncState | None, str]:
    """Return (state, user_text_without_marker). state is None if no marker found.

    Unknown versions are returned as None (treated as unmanaged) — callers can
    decide how to handle migration.
    """
    if not text:
        return None, text or ""
    m = _SENTINEL_RE.search(text)
    if not m:
        return None, text
    ver = int(m.group("ver"))
    if ver != SYNC_STATE_VERSION:
        return None, text
    try:
        payload = json.loads(m.group("json"))
    except json.JSONDecodeError:
        return None, text
    state = SyncState(
        route_id=payload["route_id"],
        collection_id=payload["collection_id"],
        modified_at=payload["modified_at"],
        synced_at=payload["synced_at"],
        version=ver,
    )
    return state, strip(text)


def strip(text: str) -> str:
    """Remove the sentinel comment (and trailing whitespace) from text."""
    if not text:
        return ""
    cleaned = _SENTINEL_RE.sub("", text)
    return cleaned.rstrip()
