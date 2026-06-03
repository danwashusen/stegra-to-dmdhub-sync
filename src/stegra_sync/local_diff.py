"""Diff Stegra snapshot vs local target manifest → SyncPlan.

Algorithm (same shape as the original DMD diff, adapted to filesystem):

1. For each Stegra Collection:
   - if its corresponding folder name is missing in the manifest → create_folder
   - if folder name changed (manifest folder_name != current collection name)
     → rename_folder

2. For each (route, collection) pair in Stegra:
   - if no manifest entry with this key → upload_gpx (new)
   - if entry exists but modified_at differs → upload_gpx (stale; payload
     carries `replaces_relative_path`)
   - if entry exists and modified_at matches → skip

3. For each manifest entry whose (route_id, collection_id) is no longer in
   Stegra → delete_gpx (orphan)

4. After the above, any folder that ends up empty AND maps to a collection
   that no longer exists in Stegra → delete_folder.

The "Unsorted" folder is treated like any collection with cid="".
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from .local_target import (
    LocalManifest,
    PlannedEntry,
    UNSORTED_FOLDER_NAME,
    collection_name,
    plan_entries,
)
from .models import StegraSnapshot
from .plan import PlanAction, SyncPlan


def compute(
    stegra: StegraSnapshot,
    manifest: LocalManifest,
    target_dir: Path,
) -> SyncPlan:
    plan = SyncPlan(
        dry_run=True,
        generated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
    )

    planned, new_folder_names = plan_entries(stegra, target_dir, existing=manifest)
    existing_lookup = manifest.by_key()
    existing_folder_names = dict(manifest.folder_names)

    # Step 1 + 2: folder creates and renames
    for cid, new_name in new_folder_names.items():
        # Determine if any planned entry uses this folder
        uses_folder = any(p.folder_name == new_name for p in planned.values())
        if not uses_folder:
            continue
        old_name = existing_folder_names.get(cid)
        if old_name is None:
            # First time we see this folder. Only emit create_folder if the
            # directory doesn't already exist.
            if not (target_dir / new_name).exists():
                plan.actions.append(PlanAction(
                    kind="create_folder",
                    reason="new collection on Stegra"
                            if cid else "no Unsorted folder yet",
                    payload={
                        "stegra_collection_id": cid,
                        "stegra_collection_name": collection_name(stegra, cid),
                        "name": new_name,
                    },
                ))
        elif old_name != new_name:
            plan.actions.append(PlanAction(
                kind="rename_folder",
                reason=f"collection renamed: '{old_name}' → '{new_name}'",
                payload={
                    "stegra_collection_id": cid,
                    "old_name": old_name,
                    "new_name": new_name,
                },
            ))

    # Step 3: write/update GPX
    stegra_keys: set[tuple[str, str]] = set()
    for key, p in planned.items():
        stegra_keys.add(key)
        route = stegra.routes[p.route_id]
        existing = existing_lookup.get(key)
        if existing is None:
            plan.actions.append(PlanAction(
                kind="upload_gpx",
                reason="new route in this collection",
                payload={
                    "stegra_route_id": p.route_id,
                    "stegra_route_name": route.name,
                    "stegra_collection_id": p.collection_id,
                    "stegra_collection_name": collection_name(stegra, p.collection_id),
                    "stegra_modified_at": p.modified_at,
                    "relative_path": p.gpx_relpath,
                    "md_relative_path": p.md_relpath,
                    "folder_name": p.folder_name,
                },
            ))
        elif existing.modified_at != p.modified_at:
            plan.actions.append(PlanAction(
                kind="upload_gpx",
                reason=(f"Stegra modified_at changed: "
                        f"{existing.modified_at} → {p.modified_at}"),
                payload={
                    "stegra_route_id": p.route_id,
                    "stegra_route_name": route.name,
                    "stegra_collection_id": p.collection_id,
                    "stegra_collection_name": collection_name(stegra, p.collection_id),
                    "stegra_modified_at": p.modified_at,
                    "relative_path": p.gpx_relpath,
                    "md_relative_path": p.md_relpath,
                    "folder_name": p.folder_name,
                    "replaces_relative_path": existing.relative_path,
                },
            ))
        elif existing.relative_path != p.gpx_relpath:
            # Same content, moved (e.g. collection was renamed, basename
            # changed due to collision resolution). Emit upload_gpx so the
            # file lands in the new location; executor removes the old.
            plan.actions.append(PlanAction(
                kind="upload_gpx",
                reason="entry moved to new path (collection rename or basename change)",
                payload={
                    "stegra_route_id": p.route_id,
                    "stegra_route_name": route.name,
                    "stegra_collection_id": p.collection_id,
                    "stegra_collection_name": collection_name(stegra, p.collection_id),
                    "stegra_modified_at": p.modified_at,
                    "relative_path": p.gpx_relpath,
                    "md_relative_path": p.md_relpath,
                    "folder_name": p.folder_name,
                    "replaces_relative_path": existing.relative_path,
                },
            ))

    # Step 4: orphans (in manifest but not in Stegra anymore)
    for key, entry in existing_lookup.items():
        if key in stegra_keys:
            continue
        plan.actions.append(PlanAction(
            kind="delete_gpx",
            reason="no matching Stegra (route, collection) pair (orphan)",
            payload={
                "relative_path": entry.relative_path,
                "stegra_route_id": entry.route_id,
                "stegra_collection_id": entry.collection_id,
            },
        ))

    # Step 5: empty folders for removed collections
    stegra_cids = set(stegra.collections.keys()) | {""}
    for cid, old_name in existing_folder_names.items():
        if cid in stegra_cids:
            continue
        # The collection is gone. After the deletes above, this folder will
        # have no managed entries. Emit delete_folder.
        plan.actions.append(PlanAction(
            kind="delete_folder",
            reason="Stegra collection removed",
            payload={
                "stegra_collection_id": cid,
                "folder_name": old_name,
            },
        ))

    return plan
