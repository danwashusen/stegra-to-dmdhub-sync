"""DMD Hub client — both read-side enumeration and write-side mutations.

Read endpoints (cookie-session auth):

    GET /api/gpx-manager.php?action=list[&folder_id={id}]
        -> {success, folders[], files[], current_folder, current_folder_parent_id,
            search_mode, folders_with_matches, has_filters}
        Each entry in `files[]` is the FULL gpx record.

    GET /storage/users/{owner}/gpx_files/{file}
        -> raw application/gpx+xml

Write endpoints:

    POST /api/gpx-manager.php (JSON body, no CSRF)
        {"action":"create_folder", "name":"...", "parent_id": null|<int>}
        {"action":"move_item", "item_type":"file"|"folder", "item_id":"...",
                                "target_folder_id":<int|null>}
        {"action":"delete_folder", "folder_id": <int>}
            -- cascades by moving contents to parent
        {"action":"delete", "gpx_id":"..."}

    POST /account/profile/gpx/add/ (multipart, CSRF token required)
        Fields: csrf_token, submit_new_gpx=1, gpx_file (File), title,
        description, continent, country, public, allow_download,
        allow_index, tags, show_on_map, warnings, best_time[], vehicle[],
        difficulty (1-5), off_road_percentage, image1, image2, youtube,
        gpx_meta_description, gpx_meta_link, gpx_meta_author, gpx_meta_time.
        302-redirect on success; new gpx_id discovered by post-upload list.

    POST /account/profile/gpx/edit/?id={gpx_id} (multipart, CSRF required)
        Same fields with submit_edit_gpx=1; gpx_file optional (metadata-only
        update if omitted).

Folder IDs are numeric ints; GPX IDs are MongoDB ObjectIds (24 hex chars).
CSRF tokens are per-session, scraped from the add-form HTML.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
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
            "DMD Hub returned auth failure. Run `stegra-sync auth` again."
        )
    if not resp.headers.get("content-type", "").startswith("application/json"):
        raise DmdAuthError(
            "DMD Hub returned non-JSON (likely a login redirect). "
            "Run `stegra-sync auth` again."
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


# ============================================================================
# Write client
# ============================================================================

ADD_FORM_PATH = "/account/profile/gpx/add/"
EDIT_FORM_PATH = "/account/profile/gpx/edit/"

_DIFFICULTY_NUMERIC = {"": 2, "easy": 1, "medium": 2, "hard": 3, "extreme": 4, "expert": 5}


class DmdClient:
    """DMD Hub write client. Handles CSRF, multipart uploads, JSON mutations."""

    def __init__(self, auth: AuthBundle, *, timeout: float = 60.0) -> None:
        self._http = httpx.Client(
            base_url=BASE_URL,
            cookies=auth.dmd_cookies,
            timeout=timeout,
            follow_redirects=False,
        )
        self._csrf: Optional[str] = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "DmdClient":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    # ----- CSRF -----

    def _ensure_csrf(self) -> str:
        if self._csrf is not None:
            return self._csrf
        resp = self._http.get(ADD_FORM_PATH, headers={"Accept": "text/html"})
        if resp.status_code in (301, 302, 401, 403):
            raise DmdAuthError(
                "DMD Hub returned auth failure while fetching CSRF token. Re-run auth."
            )
        # Match either input or meta tag for csrf_token; tolerant of attribute order
        m = (
            re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', resp.text)
            or re.search(r'value=["\']([^"\']+)["\']\s+name=["\']csrf_token["\']', resp.text)
        )
        if not m:
            raise RuntimeError("Could not find csrf_token in DMD add-form HTML.")
        self._csrf = m.group(1)
        return self._csrf

    # ----- JSON gpx-manager.php mutations -----

    def _post_json(self, payload: dict) -> dict:
        resp = self._http.post(LIST_PATH, json=payload)
        if resp.status_code in (301, 302, 401, 403):
            raise DmdAuthError(
                f"DMD Hub returned auth failure on {payload.get('action')}. Re-run auth."
            )
        if not resp.headers.get("content-type", "").startswith("application/json"):
            raise DmdAuthError(
                f"DMD Hub returned non-JSON on {payload.get('action')} "
                "(likely login redirect). Re-run auth."
            )
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(
                f"DMD {payload.get('action')} failed: {data.get('error', data)}"
            )
        return data

    def create_folder(self, name: str, parent_id: Optional[int] = None) -> int:
        """Returns the new folder_id."""
        data = self._post_json({
            "action": "create_folder",
            "name": name,
            "parent_id": parent_id,
        })
        new_id = data.get("folder_id") or data.get("id") or (
            data.get("folder") or {}
        ).get("_id")
        if new_id is None:
            raise RuntimeError(f"create_folder returned no id: {data}")
        return int(new_id)

    def move_item(
        self,
        item_id: str,
        target_folder_id: Optional[int],
        item_type: str = "file",
    ) -> None:
        self._post_json({
            "action": "move_item",
            "item_type": item_type,
            "item_id": item_id,
            "target_folder_id": target_folder_id,
        })

    def delete_folder(self, folder_id: int) -> None:
        """Cascades by moving folder contents to parent. Empty before calling
        if you want true deletion of contents."""
        self._post_json({
            "action": "delete_folder",
            "folder_id": str(folder_id),
        })

    def delete_gpx(self, gpx_id: str) -> None:
        self._post_json({"action": "delete", "gpx_id": gpx_id})

    # ----- Multipart upload / edit -----

    def upload_gpx(
        self,
        gpx_bytes: bytes,
        *,
        title: str,
        description: str,
        filename: str = "upload.gpx",
        public: bool = False,
        allow_download: bool = False,
        allow_index: bool = True,
        show_on_map: bool = False,
        difficulty: int = 2,
        off_road_percentage: int = 50,
        tags: str = "",
        warnings: str = "",
        youtube: str = "",
        continent: str = "",
        country: str = "",
        best_time: Optional[list[str]] = None,
        vehicle: Optional[list[str]] = None,
        gpx_meta_description: str = "",
        gpx_meta_link: str = "",
        gpx_meta_author: str = "",
        gpx_meta_time: str = "1970-01-01 00:00:00",
    ) -> str:
        """Submit the add form. Returns the new gpx_id by probing the listing."""
        # Snapshot the list of root GPX IDs BEFORE upload so we can diff after.
        ids_before = self._collect_all_gpx_ids()
        self._submit_gpx_form(
            url=ADD_FORM_PATH,
            extra_fields={"submit_new_gpx": "1"},
            gpx_bytes=gpx_bytes,
            filename=filename,
            title=title,
            description=description,
            public=public,
            allow_download=allow_download,
            allow_index=allow_index,
            show_on_map=show_on_map,
            difficulty=difficulty,
            off_road_percentage=off_road_percentage,
            tags=tags,
            warnings=warnings,
            youtube=youtube,
            continent=continent,
            country=country,
            best_time=best_time,
            vehicle=vehicle,
            gpx_meta_description=gpx_meta_description,
            gpx_meta_link=gpx_meta_link,
            gpx_meta_author=gpx_meta_author,
            gpx_meta_time=gpx_meta_time,
        )
        ids_after = self._collect_all_gpx_ids()
        new = ids_after - ids_before
        if len(new) != 1:
            raise RuntimeError(
                f"Could not unambiguously identify new gpx_id after upload "
                f"(found {len(new)} new id(s): {sorted(new)[:5]})."
            )
        return new.pop()

    def update_gpx(
        self,
        gpx_id: str,
        *,
        title: str,
        description: str,
        gpx_bytes: Optional[bytes] = None,
        filename: str = "update.gpx",
        public: bool = False,
        allow_download: bool = False,
        allow_index: bool = True,
        show_on_map: bool = False,
        difficulty: int = 2,
        off_road_percentage: int = 50,
        tags: str = "",
        warnings: str = "",
        youtube: str = "",
        continent: str = "",
        country: str = "",
        best_time: Optional[list[str]] = None,
        vehicle: Optional[list[str]] = None,
        gpx_meta_description: str = "",
        gpx_meta_link: str = "",
        gpx_meta_author: str = "",
        gpx_meta_time: str = "1970-01-01 00:00:00",
    ) -> None:
        """Submit the edit form. If gpx_bytes is None, only metadata is updated."""
        self._submit_gpx_form(
            url=f"{EDIT_FORM_PATH}?id={gpx_id}",
            extra_fields={"submit_edit_gpx": "1"},
            gpx_bytes=gpx_bytes,
            filename=filename,
            title=title,
            description=description,
            public=public,
            allow_download=allow_download,
            allow_index=allow_index,
            show_on_map=show_on_map,
            difficulty=difficulty,
            off_road_percentage=off_road_percentage,
            tags=tags,
            warnings=warnings,
            youtube=youtube,
            continent=continent,
            country=country,
            best_time=best_time,
            vehicle=vehicle,
            gpx_meta_description=gpx_meta_description,
            gpx_meta_link=gpx_meta_link,
            gpx_meta_author=gpx_meta_author,
            gpx_meta_time=gpx_meta_time,
        )

    # ----- Internals -----

    def _submit_gpx_form(
        self,
        *,
        url: str,
        extra_fields: dict,
        gpx_bytes: Optional[bytes],
        filename: str,
        title: str,
        description: str,
        public: bool,
        allow_download: bool,
        allow_index: bool,
        show_on_map: bool,
        difficulty: int,
        off_road_percentage: int,
        tags: str,
        warnings: str,
        youtube: str,
        continent: str,
        country: str,
        best_time: Optional[list[str]],
        vehicle: Optional[list[str]],
        gpx_meta_description: str,
        gpx_meta_link: str,
        gpx_meta_author: str,
        gpx_meta_time: str,
    ) -> httpx.Response:
        csrf = self._ensure_csrf()
        # httpx encodes form fields with a list value as repeated names — good
        # for best_time[]/vehicle[]. Use list of tuples for repeated keys.
        data: list[tuple[str, str]] = [
            ("csrf_token", csrf),
            ("title", title),
            ("description", description),
            ("warnings", warnings),
            ("tags", tags),
            ("continent", continent),
            ("country", country),
            ("youtube", youtube),
            ("difficulty", str(difficulty)),
            ("off_road_percentage", str(off_road_percentage)),
            ("gpx_meta_description", gpx_meta_description),
            ("gpx_meta_link", gpx_meta_link),
            ("gpx_meta_author", gpx_meta_author),
            ("gpx_meta_time", gpx_meta_time),
        ]
        for k, v in extra_fields.items():
            data.append((k, str(v)))
        # Checkbox fields: only send when truthy (form omits them otherwise)
        if public:
            data.append(("public", "true"))
        if allow_download:
            data.append(("allow_download", "true"))
        if allow_index:
            data.append(("allow_index", "true"))
        if show_on_map:
            data.append(("show_on_map", "true"))
        for v in best_time or []:
            data.append(("best_time[]", v))
        for v in vehicle or []:
            data.append(("vehicle[]", v))

        files: dict[str, tuple[str, bytes, str]] = {}
        if gpx_bytes is not None:
            files["gpx_file"] = (filename, gpx_bytes, "application/gpx+xml")

        resp = self._http.post(
            url, data=data, files=files or None,
            headers={"Accept": "text/html"},
        )
        # Success path: 302 redirect back to /account/profile/gpx/
        if resp.status_code in (200, 302, 303):
            return resp
        if resp.status_code in (401, 403):
            raise DmdAuthError("DMD Hub returned auth failure on form post. Re-run auth.")
        raise RuntimeError(
            f"DMD form post failed: {resp.status_code} {resp.text[:300]}"
        )

    def _collect_all_gpx_ids(self) -> set[str]:
        """Return the set of all gpx ids visible to this user across folders."""
        ids: set[str] = set()
        to_visit: list[Optional[str]] = [None]
        visited: set[str] = set()
        while to_visit:
            cur = to_visit.pop()
            key = cur if cur is not None else ""
            if key in visited:
                continue
            visited.add(key)
            params: dict[str, str] = {"action": "list"}
            if cur:
                params["folder_id"] = cur
            resp = self._http.get(LIST_PATH, params=params)
            if resp.status_code in (301, 302, 401, 403):
                raise DmdAuthError("DMD list failed mid-upload-probe. Re-run auth.")
            data = resp.json()
            for f in data.get("files", []):
                ids.add(f["_id"])
            for sub in data.get("folders", []):
                to_visit.append(sub["_id"])
        return ids
