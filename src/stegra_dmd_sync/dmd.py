"""DMD Hub read-side enumeration.

Confirmed read endpoints (cookie-session auth):

    GET /api/gpx-manager.php?action=list[&folder_id={id}]
        -> {success, folders[], files[], current_folder, current_folder_parent_id,
            search_mode, folders_with_matches, has_filters}
        Each entry in `files[]` is the FULL gpx record (same fields as
        get_gpx_info). No separate per-record fetch is needed during
        enumeration.

    GET /api/gpx-manager.php?action=list_folders
        -> {success, folders[]} (folder shells only; we don't use it because
           `action=list` returns folders alongside files for the current
           level, which is what we need.)

    GET /storage/users/{owner}/gpx_files/{file}
        -> raw application/gpx+xml

The "Community Collection" entry in the UI is a system entity and is not
returned by either listing; we ignore it entirely.

Write endpoints (upload / update / move / delete / create_folder /
delete_folder / rename_folder) are not yet implemented — the action names are
confirmed (the API responds to them) but parameter shapes need a recon round
where the user takes those UI actions.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

import httpx

from .auth import AuthBundle
from .footer import parse as parse_footer
from .models import (
    ROOT_FOLDER_ID,
    DmdFolder,
    DmdGpx,
    DmdSnapshot,
)

BASE_URL = "https://hub.dmdnavigation.com"
LIST_PATH = "/api/gpx-manager.php"

# Progress callback: (phase, current, total, label)
ProgressFn = Callable[[str, int, int, str], None]


class DmdAuthError(RuntimeError):
    """Raised when DMD calls fail authentication. Caller should re-run auth."""


def _client(auth: AuthBundle) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        cookies=auth.dmd_cookies,
        timeout=30.0,
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )


def _list(cli: httpx.Client, folder_id: Optional[str] = None) -> dict:
    """One `action=list` call. Detects auth failure (HTML response → 200 OK)."""
    params: dict[str, str] = {"action": "list"}
    if folder_id:
        params["folder_id"] = folder_id
    resp = cli.get(LIST_PATH, params=params)
    if resp.status_code in (301, 302, 401, 403):
        raise DmdAuthError(
            "DMD Hub returned auth failure. Run `stegra-to-dmdhub-sync auth` again."
        )
    if not resp.headers.get("content-type", "").startswith("application/json"):
        raise DmdAuthError(
            "DMD Hub returned non-JSON (likely a login redirect). "
            "Run `stegra-to-dmdhub-sync auth` again."
        )
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"DMD listing failed: {data.get('error', 'unknown')}")
    return data


def _to_gpx(raw: dict) -> DmdGpx:
    description = raw.get("description") or ""
    sync_state, _user_text = parse_footer(description)
    return DmdGpx(
        id=raw["_id"],
        title=raw.get("title", "") or "",
        description=description,
        public=bool(raw.get("public", False)),
        approved=bool(raw.get("approved", False)),
        difficulty=raw.get("difficulty", "") or "",
        off_road_percentage=int(raw.get("off_road_percentage") or 0),
        tags=raw.get("tags", "") or "",
        color=raw.get("color", "") or "",
        gpx_length_km=float(raw.get("gpx_length_km") or 0),
        created=int(raw.get("_created") or 0),
        modified=int(raw.get("_modified") or 0),
        file_path=raw.get("file_path", "") or "",
        sync_state=sync_state,
    )


def fetch_snapshot(
    auth: AuthBundle,
    on_progress: Optional[ProgressFn] = None,
) -> DmdSnapshot:
    """Walk the GPX Manager tree: root + every folder + every GPX record."""
    folders: dict[str, DmdFolder] = {
        ROOT_FOLDER_ID: DmdFolder(
            id=ROOT_FOLDER_ID, name="Root", gpx_ids=[], parent_id=""
        ),
    }
    gpx: dict[str, DmdGpx] = {}

    with _client(auth) as cli:
        # Stack-based DFS: pull root, queue its subfolders, etc. Stops on cycles.
        to_visit: list[Optional[str]] = [None]  # None == root
        visited: set[str] = set()
        page_count = 0

        while to_visit:
            current = to_visit.pop()
            key = current if current is not None else ROOT_FOLDER_ID
            if key in visited:
                continue
            visited.add(key)
            page_count += 1

            data = _list(cli, folder_id=current)

            file_ids: list[str] = []
            for f in data.get("files", []):
                g = _to_gpx(f)
                gpx[g.id] = g
                file_ids.append(g.id)

            folders[key].gpx_ids = file_ids

            if on_progress:
                label = (f"{folders[key].name}: "
                         f"{len(data.get('folders', []))} subfolders, "
                         f"{len(file_ids)} files")
                on_progress("dmd_list", page_count, page_count, label)

            for sub in data.get("folders", []):
                sid = sub["_id"]
                if sid in folders:
                    continue
                folders[sid] = DmdFolder(
                    id=sid,
                    name=sub.get("name", "") or "",
                    gpx_ids=[],
                    parent_id=key,
                )
                to_visit.append(sid)

    return DmdSnapshot(
        pulled_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        folders=folders,
        gpx=gpx,
    )


# ---------- snapshot persistence ----------

def write_snapshot(snapshot: DmdSnapshot, snapshots_dir: Path) -> Path:
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    out = snapshots_dir / "dmd.json"
    serialised = {
        "pulled_at": snapshot.pulled_at,
        "folders": {fid: asdict(f) for fid, f in snapshot.folders.items()},
        "gpx": {gid: _gpx_to_dict(g) for gid, g in snapshot.gpx.items()},
    }
    out.write_text(json.dumps(serialised, indent=2, sort_keys=True))
    return out


def _gpx_to_dict(g: DmdGpx) -> dict:
    d = asdict(g)
    # asdict recursively handles SyncState already, so nothing extra needed.
    return d


def read_snapshot(snapshots_dir: Path) -> Optional[DmdSnapshot]:
    from .models import SyncState

    p = snapshots_dir / "dmd.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    folders = {fid: DmdFolder(**f) for fid, f in data["folders"].items()}
    gpx: dict[str, DmdGpx] = {}
    for gid, g in data["gpx"].items():
        state_dict = g.pop("sync_state", None)
        state = SyncState(**state_dict) if state_dict else None
        gpx[gid] = DmdGpx(sync_state=state, **g)
    return DmdSnapshot(
        pulled_at=data["pulled_at"],
        folders=folders,
        gpx=gpx,
    )
