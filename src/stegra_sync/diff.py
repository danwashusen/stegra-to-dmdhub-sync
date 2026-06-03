"""Stegra snapshot + DMD snapshot → SyncPlan.

Algorithm
---------

Step 1. Determine which DMD folder "belongs" to which Stegra collection.
    A folder's identity is read from the SyncState footer embedded in any
    GPX inside it. If multiple GPX disagree (shouldn't happen unless the user
    manually shuffled things), the lexicographically first id wins; the
    inconsistency is logged in the action `reason`.

Step 2. For each Stegra Collection:
    - if no DMD folder owns this collection_id -> create_folder
    - if the matched folder's name diverges -> rename_folder

Step 3. For each (route, collection) pair in Stegra (a route appears once per
    collection it belongs to; routes with no collection are paired with the
    DMD Root pseudo-folder, collection_id=""):
    - no DMD GPX with that key in its SyncState        -> upload_gpx (new)
    - DMD GPX exists, stegra modified_at == footer's   -> skip
    - DMD GPX exists, stegra modified_at differs       -> upload_gpx (stale)

Step 4. For each managed DMD GPX whose (route_id, collection_id) is no
    longer in Stegra -> delete_gpx (orphan).

Step 5. DMD records without a SyncState footer are NEVER touched (they were
    not put there by this tool).

Step 6. Empty DMD folders that don't correspond to any Stegra collection
    are left alone (we can't tell their identity).
"""
from __future__ import annotations

import datetime as _dt

from .models import (
    ROOT_FOLDER_ID,
    DmdSnapshot,
    StegraSnapshot,
)
from .plan import PlanAction, SyncPlan


def compute(stegra: StegraSnapshot, dmd: DmdSnapshot) -> SyncPlan:
    plan = SyncPlan(
        dry_run=True,
        generated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
    )

    # Step 1: identify each DMD folder's owning Stegra collection_id (if any).
    folder_owner: dict[str, str] = {}  # dmd_folder_id -> stegra_collection_id
    for fid, folder in dmd.folders.items():
        if fid == ROOT_FOLDER_ID:
            # Root represents the "no collection" bucket (collection_id="")
            folder_owner[fid] = ""
            continue
        votes: dict[str, int] = {}
        for gid in folder.gpx_ids:
            g = dmd.gpx.get(gid)
            if g and g.sync_state:
                votes[g.sync_state.collection_id] = votes.get(g.sync_state.collection_id, 0) + 1
        if votes:
            # Most-voted collection_id wins; ties broken lexicographically
            winner = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            folder_owner[fid] = winner

    # Reverse: Stegra collection_id -> DMD folder_id
    stegra_to_dmd_folder: dict[str, str] = {}
    for fid, cid in folder_owner.items():
        if cid not in stegra_to_dmd_folder or fid < stegra_to_dmd_folder[cid]:
            stegra_to_dmd_folder[cid] = fid

    # Fallback for empty folders: match by name. An empty user-created folder
    # has no GPX to vote on its identity, but if its name matches an unclaimed
    # Stegra collection, treat it as that collection's home. The first sync
    # writing into it will then establish the identity for future runs.
    claimed_dmd_fids = set(stegra_to_dmd_folder.values())
    for cid, coll in stegra.collections.items():
        if cid in stegra_to_dmd_folder:
            continue
        for fid, folder in dmd.folders.items():
            if fid == ROOT_FOLDER_ID or fid in claimed_dmd_fids:
                continue
            if not folder.gpx_ids and folder.name == coll.name:
                stegra_to_dmd_folder[cid] = fid
                claimed_dmd_fids.add(fid)
                break

    # Step 2: ensure folders exist and have the right names
    for cid, coll in stegra.collections.items():
        if cid not in stegra_to_dmd_folder:
            plan.actions.append(PlanAction(
                kind="create_folder",
                reason="Stegra collection has no matching DMD folder",
                payload={
                    "stegra_collection_id": cid,
                    "stegra_collection_name": coll.name,
                    "name": coll.name,
                },
            ))
        else:
            dmd_fid = stegra_to_dmd_folder[cid]
            dmd_folder = dmd.folders[dmd_fid]
            if dmd_folder.name != coll.name:
                plan.actions.append(PlanAction(
                    kind="rename_folder",
                    reason=f"Stegra collection renamed: '{dmd_folder.name}' → '{coll.name}'",
                    payload={
                        "stegra_collection_id": cid,
                        "dmd_folder_id": dmd_fid,
                        "old_name": dmd_folder.name,
                        "new_name": coll.name,
                    },
                ))

    # Build a lookup: (stegra_route_id, stegra_collection_id) -> dmd_gpx_id
    managed_index: dict[tuple[str, str], str] = {}
    for gid, g in dmd.gpx.items():
        if g.sync_state:
            key = (g.sync_state.route_id, g.sync_state.collection_id)
            managed_index[key] = gid

    # Step 3: walk Stegra (route, collection) pairs
    stegra_pairs: set[tuple[str, str]] = set()
    for route in stegra.routes.values():
        # Routes with no collection are paired with collection_id=""
        coll_ids = list(route.collection_ids) if route.collection_ids else [""]
        for cid in coll_ids:
            pair = (route.id, cid)
            stegra_pairs.add(pair)

            dest_folder_id = stegra_to_dmd_folder.get(cid)
            dest_folder_name = (
                stegra.collections[cid].name if cid and cid in stegra.collections
                else "Root"
            )

            existing_gid = managed_index.get(pair)
            if existing_gid is None:
                # Not yet on DMD
                plan.actions.append(PlanAction(
                    kind="upload_gpx",
                    reason="new route in this collection",
                    payload={
                        "stegra_route_id": route.id,
                        "stegra_route_name": route.name,
                        "stegra_collection_id": cid,
                        "stegra_collection_name": dest_folder_name,
                        "stegra_modified_at": route.modified_at,
                        "target_dmd_folder_id": dest_folder_id,  # may be None
                    },
                ))
                continue

            existing = dmd.gpx[existing_gid]
            assert existing.sync_state is not None  # by construction of managed_index
            if existing.sync_state.modified_at == route.modified_at:
                # Up to date — no action
                continue
            plan.actions.append(PlanAction(
                kind="upload_gpx",
                reason=(f"Stegra modified_at changed: "
                        f"{existing.sync_state.modified_at} → {route.modified_at}"),
                payload={
                    "stegra_route_id": route.id,
                    "stegra_route_name": route.name,
                    "stegra_collection_id": cid,
                    "stegra_collection_name": dest_folder_name,
                    "stegra_modified_at": route.modified_at,
                    "replaces_dmd_gpx_id": existing_gid,
                    "replaces_dmd_gpx_title": existing.title,
                    "target_dmd_folder_id": dest_folder_id,
                },
            ))

    # Step 4: orphans — managed DMD entries with no matching Stegra pair
    for pair, gid in managed_index.items():
        if pair in stegra_pairs:
            continue
        existing = dmd.gpx[gid]
        plan.actions.append(PlanAction(
            kind="delete_gpx",
            reason="no matching Stegra (route, collection) pair (orphan)",
            payload={
                "dmd_gpx_id": gid,
                "dmd_gpx_title": existing.title,
                "stegra_route_id": pair[0],
                "stegra_collection_id": pair[1],
            },
        ))

    return plan
