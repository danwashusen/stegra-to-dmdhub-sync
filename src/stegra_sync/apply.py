"""Execute a SyncPlan against DMD Hub.

Execution order:
  1. create_folder  — collect new {stegra_collection_id: dmd_folder_id}
  2. rename_folder  — (currently unsupported; warning only)
  3. upload_gpx (new)    — upload, then move to target folder
  4. upload_gpx (stale)  — uses edit endpoint, preserves DMD gpx_id
  5. delete_gpx           — orphans

Halts on the first failure so the user can fix and re-run. The plan is
declarative, so re-running after a partial failure picks up where it left off
(once `inspect` re-snapshots DMD state).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import footer as footer_mod
from .dmd import DmdAuthError, DmdClient
from .models import (
    ROOT_FOLDER_ID,
    DmdSnapshot,
    StegraSnapshot,
    SyncState,
)
from .plan import PlanAction, SyncPlan

ProgressFn = Callable[[str, int, int, str], None]


@dataclass
class ActionResult:
    action: PlanAction
    status: str   # "ok" | "fail" | "skip"
    message: str = ""


@dataclass
class ExecutionResult:
    results: list[ActionResult] = field(default_factory=list)
    halted: bool = False

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if r.status == "fail")

    @property
    def skip_count(self) -> int:
        return sum(1 for r in self.results if r.status == "skip")


def _resolve_target_folder_id(
    payload_target: Optional[str],
    stegra_collection_id: str,
    created_folders: dict[str, int],
    dmd: DmdSnapshot,
) -> Optional[int]:
    """Return the DMD folder ID to move a newly-uploaded GPX into.

    None means "leave at root".
    """
    if not stegra_collection_id:
        return None  # uncollected -> root
    if stegra_collection_id in created_folders:
        return created_folders[stegra_collection_id]
    # payload_target is either ROOT_FOLDER_ID (""), an existing string ID, or None
    if payload_target in (None, ROOT_FOLDER_ID):
        # Plan said "uncollected" but stegra_collection_id is set — odd; treat as root
        return None
    # Folder IDs in the DMD model are stored as strings (from the JSON enum
    # API). The move/create JSON API uses ints. Convert here.
    try:
        return int(payload_target)
    except (TypeError, ValueError):
        return None


def _build_description(route_description: str, sync_state: SyncState) -> str:
    return footer_mod.render(route_description or "", sync_state)


def _common_form_args(route) -> dict:  # type: ignore[no-untyped-def]
    """Map a StegraRoute into kwargs for upload_gpx / update_gpx."""
    return {
        "title": route.name,
        "off_road_percentage": route.off_road_pct,
        "public": False,        # Always private — DMD requires admin approval
        "allow_index": False,   # don't expose for HUB indexing by default
        "allow_download": False,
        "show_on_map": False,
    }


def execute_plan(
    plan: SyncPlan,
    stegra: StegraSnapshot,
    dmd: DmdSnapshot,
    gpx_dir: Path,
    client: DmdClient,
    on_progress: Optional[ProgressFn] = None,
) -> ExecutionResult:
    """Run plan actions in safe order. Halts on first failure."""
    result = ExecutionResult()
    created_folders: dict[str, int] = {}
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()

    # Group actions by kind
    by_kind: dict[str, list[PlanAction]] = {}
    for a in plan.actions:
        by_kind.setdefault(a.kind, []).append(a)

    total = len(plan.actions)
    done = 0

    def report(action: PlanAction, label: str) -> None:
        nonlocal done
        done += 1
        if on_progress:
            on_progress(action.kind, done, total, label)

    # Phase 1: create_folder
    for action in by_kind.get("create_folder", []):
        cid = action.payload.get("stegra_collection_id", "")
        name = action.payload.get("name", "")
        try:
            new_id = client.create_folder(name)
            created_folders[cid] = new_id
            result.results.append(ActionResult(action, "ok",
                f"created folder '{name}' → id={new_id}"))
            report(action, f"create_folder '{name}'")
        except (DmdAuthError, Exception) as e:
            result.results.append(ActionResult(action, "fail", str(e)))
            result.halted = True
            return result

    # Phase 2: rename_folder (unsupported)
    for action in by_kind.get("rename_folder", []):
        result.results.append(ActionResult(action, "skip",
            "rename endpoint not yet implemented — DMD folder will keep old name"))
        report(action, f"skip rename")

    # Phase 3: upload_gpx (split into new vs stale by replaces_dmd_gpx_id)
    for action in by_kind.get("upload_gpx", []):
        route_id = action.payload.get("stegra_route_id", "")
        coll_id = action.payload.get("stegra_collection_id", "")
        target_payload = action.payload.get("target_dmd_folder_id")
        replaces = action.payload.get("replaces_dmd_gpx_id")

        route = stegra.routes.get(route_id)
        if route is None:
            result.results.append(ActionResult(action, "fail",
                f"route {route_id} missing from Stegra snapshot"))
            result.halted = True
            return result

        gpx_path = gpx_dir / f"{route_id}.gpx"
        if not gpx_path.exists():
            result.results.append(ActionResult(action, "fail",
                f"GPX file not in local cache: {gpx_path}"))
            result.halted = True
            return result
        gpx_bytes = gpx_path.read_bytes()

        sync_state = SyncState(
            route_id=route_id, collection_id=coll_id,
            modified_at=route.modified_at, synced_at=now_iso,
        )
        description = _build_description(route.description, sync_state)
        common = _common_form_args(route)

        try:
            if replaces:
                client.update_gpx(
                    replaces, gpx_bytes=gpx_bytes,
                    description=description, **common,
                )
                result.results.append(ActionResult(action, "ok",
                    f"updated {replaces} ({route.name})"))
                report(action, f"updated {route.name}")
            else:
                new_id = client.upload_gpx(
                    gpx_bytes, filename=f"{route_id}.gpx",
                    description=description, **common,
                )
                target = _resolve_target_folder_id(
                    target_payload, coll_id, created_folders, dmd,
                )
                if target is not None:
                    client.move_item(new_id, target, item_type="file")
                result.results.append(ActionResult(action, "ok",
                    f"uploaded {new_id} ({route.name}) → folder {target or 'root'}"))
                report(action, f"uploaded {route.name}")
        except DmdAuthError as e:
            result.results.append(ActionResult(action, "fail", str(e)))
            result.halted = True
            return result
        except Exception as e:
            result.results.append(ActionResult(action, "fail",
                f"upload failed for {route.name}: {e}"))
            result.halted = True
            return result

    # Phase 4: delete_gpx (orphans)
    for action in by_kind.get("delete_gpx", []):
        gid = action.payload.get("dmd_gpx_id", "")
        title = action.payload.get("dmd_gpx_title", gid)
        try:
            client.delete_gpx(gid)
            result.results.append(ActionResult(action, "ok",
                f"deleted {gid} ({title})"))
            report(action, f"deleted {title}")
        except DmdAuthError as e:
            result.results.append(ActionResult(action, "fail", str(e)))
            result.halted = True
            return result
        except Exception as e:
            result.results.append(ActionResult(action, "fail",
                f"delete failed for {title}: {e}"))
            result.halted = True
            return result

    # Phase 5: delete_folder (not currently emitted by diff, but handle defensively)
    for action in by_kind.get("delete_folder", []):
        fid = action.payload.get("dmd_folder_id")
        try:
            client.delete_folder(int(fid))
            result.results.append(ActionResult(action, "ok",
                f"deleted folder {fid}"))
            report(action, f"deleted folder {fid}")
        except Exception as e:
            result.results.append(ActionResult(action, "fail", str(e)))
            result.halted = True
            return result

    return result
