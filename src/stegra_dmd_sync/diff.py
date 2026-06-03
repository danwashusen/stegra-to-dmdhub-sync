"""Diff Stegra snapshot vs DMD snapshot → SyncPlan. STUB.

Algorithm (for the implementer):

1. For each Stegra Collection, ensure a matching DMD Folder exists.
   - Match by collection_id embedded in any GPX inside the folder (any one is
     authoritative). If none, treat folder as unmanaged.
   - If no DMD folder corresponds to a Stegra collection -> create_folder.
   - If folder name diverged from Stegra collection name -> rename_folder.

2. For each (route, collection) pair in Stegra (including "Unsorted"):
   - Look for DMD GPX whose embedded SyncState matches both ids.
   - Not found            -> upload_gpx (reason: "new")
   - Found, modified_at == Stegra modified_at -> skip (no plan action)
   - Found, modified_at < Stegra modified_at  -> upload_gpx (reason: "stale")
                                                or update_gpx_metadata if only
                                                non-GPX metadata changed
                                                (future optimisation).

3. For each DMD GPX with a parsed SyncState whose (route_id, collection_id)
   no longer maps to anything in Stegra -> delete_gpx (reason: "orphan").

4. Records without a parsed SyncState are never touched.
"""
from __future__ import annotations

from .models import DmdSnapshot, StegraSnapshot
from .plan import SyncPlan


def compute(stegra: StegraSnapshot, dmd: DmdSnapshot) -> SyncPlan:
    raise NotImplementedError(
        "Diff not implemented yet. See module docstring for the intended algorithm."
    )
