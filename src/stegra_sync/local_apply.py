"""Execute a local-target SyncPlan against a folder on disk.

Ordering:
  1. create_folder       — mkdir
  2. rename_folder       — mv dir
  3. upload_gpx          — write .gpx + .md (handle replaces_relative_path)
  4. delete_gpx          — rm .gpx + .md
  5. delete_folder       — rmdir (only if empty)

Halts on first failure. Updates and writes the manifest after each successful
action so that re-runs after a partial failure pick up where they left off.
"""
from __future__ import annotations

import datetime as _dt
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .local_target import (
    LocalEntry,
    LocalManifest,
    MANIFEST_VERSION,
    render_markdown,
    write_manifest,
)
from .models import StegraSnapshot
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


def execute_plan(
    plan: SyncPlan,
    stegra: StegraSnapshot,
    manifest: LocalManifest,
    target_dir: Path,
    gpx_dir: Path,
    on_progress: Optional[ProgressFn] = None,
) -> ExecutionResult:
    target_dir.mkdir(parents=True, exist_ok=True)
    result = ExecutionResult()
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()

    # Mutate a copy of the manifest as we go
    entries_by_key: dict[tuple[str, str], LocalEntry] = manifest.by_key()
    folder_names: dict[str, str] = dict(manifest.folder_names)

    total = len(plan.actions)
    done = 0

    def report(label: str) -> None:
        nonlocal done
        done += 1
        if on_progress:
            on_progress("local", done, total, label)

    def persist() -> None:
        manifest.entries = list(entries_by_key.values())
        manifest.folder_names = folder_names
        manifest.synced_at = now_iso
        manifest.stegra_cursor = stegra.cursor
        manifest.version = MANIFEST_VERSION
        write_manifest(manifest, target_dir)

    by_kind: dict[str, list[PlanAction]] = {}
    for a in plan.actions:
        by_kind.setdefault(a.kind, []).append(a)

    # 1. create_folder
    for action in by_kind.get("create_folder", []):
        name = action.payload.get("name", "")
        cid = action.payload.get("stegra_collection_id", "")
        path = target_dir / name
        try:
            path.mkdir(parents=True, exist_ok=True)
            folder_names[cid] = name
            persist()
            result.results.append(ActionResult(action, "ok",
                f"created {path}"))
            report(f"mkdir {name}")
        except Exception as e:
            result.results.append(ActionResult(action, "fail", str(e)))
            result.halted = True
            return result

    # 2. rename_folder
    for action in by_kind.get("rename_folder", []):
        cid = action.payload.get("stegra_collection_id", "")
        old_name = action.payload.get("old_name", "")
        new_name = action.payload.get("new_name", "")
        old_path = target_dir / old_name
        new_path = target_dir / new_name
        try:
            if old_path.exists() and not new_path.exists():
                old_path.rename(new_path)
            elif not old_path.exists() and not new_path.exists():
                # Neither exists; just create the new one (entries will fill it).
                new_path.mkdir(parents=True, exist_ok=True)
            # If both exist, that's a conflict we can't resolve safely.
            elif old_path.exists() and new_path.exists():
                raise RuntimeError(
                    f"both '{old_name}' and '{new_name}' exist — "
                    "resolve manually and re-run"
                )
            folder_names[cid] = new_name
            # Update entries that lived in the old folder
            for key, entry in list(entries_by_key.items()):
                if entry.relative_path.startswith(old_name + "/"):
                    rest = entry.relative_path[len(old_name) + 1:]
                    entries_by_key[key] = LocalEntry(
                        route_id=entry.route_id,
                        collection_id=entry.collection_id,
                        relative_path=f"{new_name}/{rest}",
                        modified_at=entry.modified_at,
                    )
            persist()
            result.results.append(ActionResult(action, "ok",
                f"renamed {old_name} → {new_name}"))
            report(f"rename {old_name} → {new_name}")
        except Exception as e:
            result.results.append(ActionResult(action, "fail", str(e)))
            result.halted = True
            return result

    # 3. upload_gpx (writes .gpx + .md)
    for action in by_kind.get("upload_gpx", []):
        rid = action.payload.get("stegra_route_id", "")
        cid = action.payload.get("stegra_collection_id", "")
        rel_gpx = action.payload.get("relative_path", "")
        rel_md = action.payload.get("md_relative_path", "")
        replaces = action.payload.get("replaces_relative_path")

        route = stegra.routes.get(rid)
        if route is None:
            result.results.append(ActionResult(action, "fail",
                f"route {rid} missing from Stegra snapshot"))
            result.halted = True
            return result

        src = gpx_dir / f"{rid}.gpx"
        if not src.exists():
            result.results.append(ActionResult(action, "fail",
                f"GPX missing from local cache: {src}"))
            result.halted = True
            return result

        dst_gpx = target_dir / rel_gpx
        dst_md = target_dir / rel_md
        try:
            dst_gpx.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst_gpx)
            md_text = render_markdown(
                route,
                collection_name=action.payload.get("stegra_collection_name", ""),
                synced_at=now_iso,
            )
            dst_md.write_text(md_text)

            # Remove old files at replaces_relative_path (if different)
            if replaces and replaces != rel_gpx:
                old_gpx = target_dir / replaces
                old_md = old_gpx.with_suffix(".md")
                if old_gpx.exists():
                    old_gpx.unlink()
                if old_md.exists():
                    old_md.unlink()

            entries_by_key[(rid, cid)] = LocalEntry(
                route_id=rid,
                collection_id=cid,
                relative_path=rel_gpx,
                modified_at=action.payload.get("stegra_modified_at", ""),
            )
            persist()
            result.results.append(ActionResult(action, "ok",
                f"wrote {rel_gpx}"))
            report(f"write {route.name}")
        except Exception as e:
            result.results.append(ActionResult(action, "fail", str(e)))
            result.halted = True
            return result

    # 4. delete_gpx
    for action in by_kind.get("delete_gpx", []):
        rid = action.payload.get("stegra_route_id", "")
        cid = action.payload.get("stegra_collection_id", "")
        rel = action.payload.get("relative_path", "")
        gpx_path = target_dir / rel
        md_path = gpx_path.with_suffix(".md")
        try:
            if gpx_path.exists():
                gpx_path.unlink()
            if md_path.exists():
                md_path.unlink()
            entries_by_key.pop((rid, cid), None)
            persist()
            result.results.append(ActionResult(action, "ok",
                f"deleted {rel}"))
            report(f"delete {rel}")
        except Exception as e:
            result.results.append(ActionResult(action, "fail", str(e)))
            result.halted = True
            return result

    # 5. delete_folder (only if empty)
    for action in by_kind.get("delete_folder", []):
        name = action.payload.get("folder_name", "")
        cid = action.payload.get("stegra_collection_id", "")
        path = target_dir / name
        try:
            if path.exists():
                # Refuse to delete a non-empty folder; the user might have put
                # other files in there.
                if any(path.iterdir()):
                    result.results.append(ActionResult(action, "skip",
                        f"folder '{name}' is not empty; left in place"))
                    report(f"skip rmdir {name} (not empty)")
                    continue
                path.rmdir()
            folder_names.pop(cid, None)
            persist()
            result.results.append(ActionResult(action, "ok",
                f"removed folder {name}"))
            report(f"rmdir {name}")
        except Exception as e:
            result.results.append(ActionResult(action, "fail", str(e)))
            result.halted = True
            return result

    persist()
    return result
