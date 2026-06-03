"""Stegra.io reader.

API (Azure B2C bearer token, lifetime ~60min):

    GET /api/route-service/sync/pull?since={seq}&limit={n}
        -> {reset_required, max_seq, has_more, routes[], collections[],
            pois[], pins[], deleted_*_ids[]}

    GET /api/route-service/routes/{id}/gpx
        -> application/gpx+xml

Snapshot persistence layout:
    snapshots/stegra.json     - full enumeration
    snapshots/stegra.cursor   - last successful max_seq for incremental pulls
    gpx/{route_id}.gpx        - downloaded GPX, only refreshed when modified_at
                                changes vs. the stored snapshot.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx

# Progress callback for long-running operations.
# Args: phase, current_index, total, label
# - phase: "pull_page" | "gpx_download"
# - current_index, total: 1-based progress
# - label: human-readable item name (e.g. route title)
ProgressFn = Callable[[str, int, int, str], None]

from .auth import AuthBundle
from .models import StegraCollection, StegraRoute, StegraSnapshot

BASE_URL = "https://stegra.io"
SYNC_PATH = "/api/route-service/sync/pull"
GPX_PATH_FMT = "/api/route-service/routes/{id}/gpx"

DEFAULT_LIMIT = 1000


class StegraAuthError(RuntimeError):
    """Raised on 401 — caller should re-run `sync auth`."""


def _client(auth: AuthBundle) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {auth.stegra_token}"},
        timeout=30.0,
    )


def _raise_for_auth(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise StegraAuthError(
            "Stegra returned 401 — token expired or invalid. Run `sync auth` again."
        )
    resp.raise_for_status()


def pull_all(
    auth: AuthBundle,
    since: int = 0,
    on_progress: Optional[ProgressFn] = None,
) -> dict:
    """Loop sync/pull until has_more is false. Returns merged response.

    Behaviour: Stegra's sync endpoint returns deltas since the supplied
    cursor. We start from `since` and accumulate routes/collections/pois/pins
    keyed by id (later pages overwrite earlier ones for the same id).
    """
    merged = {
        "routes": {},
        "collections": {},
        "pois": {},
        "pins": {},
        "deleted_route_ids": set(),
        "deleted_collection_ids": set(),
        "deleted_poi_ids": set(),
        "deleted_pin_ids": set(),
        "max_seq": since,
    }
    cursor = since
    page = 0
    with _client(auth) as cli:
        while True:
            page += 1
            resp = cli.get(SYNC_PATH, params={"since": cursor, "limit": DEFAULT_LIMIT})
            _raise_for_auth(resp)
            data = resp.json()
            for key in ("routes", "collections", "pois", "pins"):
                for item in data.get(key, []):
                    merged[key][item["id"]] = item
            for key in ("deleted_route_ids", "deleted_collection_ids",
                        "deleted_poi_ids", "deleted_pin_ids"):
                merged[key].update(data.get(key, []))
            merged["max_seq"] = data.get("max_seq", cursor)
            if on_progress:
                label = (f"page {page}: +{len(data.get('routes', []))} routes, "
                         f"+{len(data.get('collections', []))} collections, "
                         f"+{len(data.get('pois', []))} pois")
                on_progress("pull_page", page, page, label)
            if not data.get("has_more"):
                break
            cursor = merged["max_seq"]
    # Normalise sets back to lists for JSON serialisation later.
    for k in ("deleted_route_ids", "deleted_collection_ids",
              "deleted_poi_ids", "deleted_pin_ids"):
        merged[k] = sorted(merged[k])
    return merged


def to_snapshot(raw: dict) -> StegraSnapshot:
    routes: dict[str, StegraRoute] = {}
    for rid, r in raw["routes"].items():
        routes[rid] = StegraRoute(
            id=r["id"],
            name=r.get("name", ""),
            description=r.get("description", "") or "",
            private=bool(r.get("private", True)),
            created_at=r.get("created_at", ""),
            modified_at=r.get("modified_at", ""),
            total_distance=float(r.get("total_distance", 0) or 0),
            total_unpaved_distance=float(r.get("total_unpaved_distance", 0) or 0),
            total_duration=float(r.get("total_duration", 0) or 0),
            color=r.get("color", "") or "",
            change_seq=int(r.get("change_seq", 0) or 0),
            owner_name=r.get("owner_name", "") or "",
            owner_user_id=r.get("owner_user_id", "") or "",
        )

    collections: dict[str, StegraCollection] = {}
    for cid, c in raw["collections"].items():
        collections[cid] = StegraCollection(
            id=c["id"],
            name=c.get("name", ""),
            description=c.get("description", "") or "",
            modified_at=c.get("modified_at", ""),
            route_ids=list(c.get("route_ids", []) or []),
            poi_ids=list(c.get("poi_ids", []) or []),
            change_seq=int(c.get("change_seq", 0) or 0),
        )

    _backfill_collection_ids(routes, collections)

    return StegraSnapshot(
        cursor=raw["max_seq"],
        pulled_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        routes=routes,
        collections=collections,
    )


def _backfill_collection_ids(
    routes: dict[str, StegraRoute],
    collections: dict[str, StegraCollection],
) -> None:
    """Reset and recompute each route's collection_ids from collections.route_ids."""
    for r in routes.values():
        r.collection_ids = []
    for coll in collections.values():
        for rid in coll.route_ids:
            if rid in routes:
                routes[rid].collection_ids.append(coll.id)


@dataclass
class PullDelta:
    """Summary of what changed in one pull, regardless of total state."""
    routes_added: int = 0
    routes_updated: int = 0
    routes_deleted: int = 0
    collections_added: int = 0
    collections_updated: int = 0
    collections_deleted: int = 0

    @property
    def empty(self) -> bool:
        return all(getattr(self, f) == 0 for f in (
            "routes_added", "routes_updated", "routes_deleted",
            "collections_added", "collections_updated", "collections_deleted",
        ))


def merge(previous: Optional[StegraSnapshot], raw: dict) -> tuple[StegraSnapshot, PullDelta]:
    """Apply a sync/pull delta on top of a previous snapshot.

    Returns (merged_snapshot, delta_summary). If `previous` is None, this is
    equivalent to `to_snapshot(raw)` with an "all new" delta.
    """
    delta_snapshot = to_snapshot(raw)

    prev_routes = dict(previous.routes) if previous else {}
    prev_colls = dict(previous.collections) if previous else {}

    summary = PullDelta()

    # Apply route upserts
    for rid, route in delta_snapshot.routes.items():
        if rid in prev_routes:
            summary.routes_updated += 1
        else:
            summary.routes_added += 1
        prev_routes[rid] = route

    # Apply collection upserts
    for cid, coll in delta_snapshot.collections.items():
        if cid in prev_colls:
            summary.collections_updated += 1
        else:
            summary.collections_added += 1
        prev_colls[cid] = coll

    # Apply deletions
    for rid in raw.get("deleted_route_ids", []):
        if prev_routes.pop(rid, None) is not None:
            summary.routes_deleted += 1
    for cid in raw.get("deleted_collection_ids", []):
        if prev_colls.pop(cid, None) is not None:
            summary.collections_deleted += 1

    _backfill_collection_ids(prev_routes, prev_colls)

    return StegraSnapshot(
        cursor=raw["max_seq"],
        pulled_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        routes=prev_routes,
        collections=prev_colls,
    ), summary


def write_snapshot(snapshot: StegraSnapshot, snapshots_dir: Path) -> Path:
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    out = snapshots_dir / "stegra.json"
    serialised = {
        "cursor": snapshot.cursor,
        "pulled_at": snapshot.pulled_at,
        "routes": {rid: asdict(r) for rid, r in snapshot.routes.items()},
        "collections": {cid: asdict(c) for cid, c in snapshot.collections.items()},
    }
    out.write_text(json.dumps(serialised, indent=2, sort_keys=True))
    (snapshots_dir / "stegra.cursor").write_text(str(snapshot.cursor))
    return out


def read_snapshot(snapshots_dir: Path) -> Optional[StegraSnapshot]:
    p = snapshots_dir / "stegra.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    routes = {rid: StegraRoute(**r) for rid, r in data["routes"].items()}
    colls = {cid: StegraCollection(**c) for cid, c in data["collections"].items()}
    return StegraSnapshot(
        cursor=data["cursor"],
        pulled_at=data["pulled_at"],
        routes=routes,
        collections=colls,
    )


def download_gpx(
    auth: AuthBundle,
    snapshot: StegraSnapshot,
    gpx_dir: Path,
    previous: Optional[StegraSnapshot] = None,
    on_progress: Optional[ProgressFn] = None,
) -> tuple[int, int]:
    """Download GPX for every route whose modified_at changed.

    Returns (downloaded, skipped).
    """
    gpx_dir.mkdir(parents=True, exist_ok=True)
    prev_routes = previous.routes if previous else {}

    routes = list(snapshot.routes.values())
    total = len(routes)
    downloaded = 0
    skipped = 0
    with _client(auth) as cli:
        for idx, route in enumerate(routes, start=1):
            target = gpx_dir / f"{route.id}.gpx"
            prev = prev_routes.get(route.id)
            if target.exists() and prev and prev.modified_at == route.modified_at:
                skipped += 1
                if on_progress:
                    on_progress("gpx_download", idx, total,
                                f"{route.name} (cached)")
                continue
            if on_progress:
                on_progress("gpx_download", idx, total, route.name)
            resp = cli.get(GPX_PATH_FMT.format(id=route.id))
            _raise_for_auth(resp)
            target.write_bytes(resp.content)
            downloaded += 1
    return downloaded, skipped
