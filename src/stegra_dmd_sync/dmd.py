"""DMD Hub side — STUB.

Confirmed read endpoints (recon notes for the implementer):

    GET /api/gpx-manager.php?action=list_folders
        -> {"success": true, "folders": [...]}  (folder shape TBD; verify when
           at least one user-created folder exists)

    GET /api/gpx-manager.php?action=get_gpx_info&gpx_id={id}
        -> {"success": true, "gpx": {...}, "parsed": {...}}
           gpx fields: _id, owner, title, description, warnings,
           continent, country, best_time[], vehicle[], difficulty,
           off_road_percentage, file, file_path, public, approved,
           show_on_map, allow_index, allow_download, tags,
           gpx_length_km, _created, _modified

    GET /storage/users/{owner}/gpx_files/{file}
        -> raw application/gpx+xml bytes

    GET /api/gpx-manager.php?action=create_folder&name=...
        -> exists; requires `name` (action confirmed, params TBD)

Still TODO (needs UI recon):
    - Root GPX listing (page is server-rendered; no list_gpx call fires.
      Probe: search-GPX input or folder-contents endpoint when implementing.)
    - Folder gpx_ids[] enumeration shape
    - upload_gpx / form post target & params
    - update / edit / save_gpx action name
    - move-to-folder mechanism (likely folder owns gpx_ids[], so this is an
      "edit folder" or "set membership" call)
    - delete_gpx
    - delete_folder

The Community Collection visible in UI is a system entity and is NOT returned
by list_folders. Sync should ignore it.
"""
from __future__ import annotations

from .auth import AuthBundle
from .models import DmdSnapshot


def fetch_snapshot(auth: AuthBundle) -> DmdSnapshot:
    """Enumerate folders + GPX records into a DmdSnapshot."""
    raise NotImplementedError(
        "DMD enumeration not implemented yet. Pending recon: see module docstring."
    )
