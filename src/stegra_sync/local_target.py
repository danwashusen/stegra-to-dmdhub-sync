"""Local-folder sync target.

Mirrors Stegra Collections as subdirectories. Each route becomes a `.gpx`
file alongside a sidecar `.md` with route metadata. Sync state lives in a
single manifest at `<target>/.stegra-sync-state.json`.

Layout
------
    <target>/
        .stegra-sync-state.json
        Bunyip/
            Bunyip Ridge Track Loop - Hard.gpx
            Bunyip Ridge Track Loop - Hard.md
        GSR/
            ...
        Unsorted/            # routes with no Stegra collection
            ...

Identity
--------
Composite key `(stegra_route_id, stegra_collection_id)` identifies each
synced entry. Routes in multiple collections are duplicated. Filename
collisions inside one folder are resolved by appending " (2)", " (3)".
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .models import StegraCollection, StegraRoute, StegraSnapshot

MANIFEST_FILENAME = ".stegra-sync-state.json"
UNSORTED_FOLDER_NAME = "Unsorted"
MANIFEST_VERSION = 1

STEGRA_ROUTE_URL_FMT = "https://stegra.io/routes/{route_id}"


def stegra_route_url(route_id: str) -> str:
    return STEGRA_ROUTE_URL_FMT.format(route_id=route_id)

# Characters forbidden by Windows + cleanup of leading/trailing dots/spaces
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class LocalEntry:
    """One (route, collection) → file pair on disk."""
    route_id: str
    collection_id: str         # "" for the Unsorted bucket
    relative_path: str         # e.g. "GSR/GSR1-Day1A (182kms).gpx"
    modified_at: str           # Stegra modified_at at last sync


@dataclass
class LocalManifest:
    """The full sync state stored at <target>/.stegra-sync-state.json."""
    version: int = MANIFEST_VERSION
    synced_at: str = ""
    stegra_cursor: int = 0
    # collection_id -> folder name (lets us detect collection renames)
    folder_names: dict[str, str] = field(default_factory=dict)
    entries: list[LocalEntry] = field(default_factory=list)

    def by_key(self) -> dict[tuple[str, str], LocalEntry]:
        return {(e.route_id, e.collection_id): e for e in self.entries}


# ---------- manifest IO ----------

def read_manifest(target_dir: Path) -> Optional[LocalManifest]:
    p = target_dir / MANIFEST_FILENAME
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return LocalManifest(
        version=data.get("version", MANIFEST_VERSION),
        synced_at=data.get("synced_at", ""),
        stegra_cursor=data.get("stegra_cursor", 0),
        folder_names=dict(data.get("folder_names", {})),
        entries=[LocalEntry(**e) for e in data.get("entries", [])],
    )


def write_manifest(manifest: LocalManifest, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    p = target_dir / MANIFEST_FILENAME
    payload = {
        "version": manifest.version,
        "synced_at": manifest.synced_at,
        "stegra_cursor": manifest.stegra_cursor,
        "folder_names": dict(sorted(manifest.folder_names.items())),
        "entries": [asdict(e) for e in sorted(
            manifest.entries, key=lambda e: (e.collection_id, e.relative_path)
        )],
    }
    p.write_text(json.dumps(payload, indent=2))
    return p


# ---------- filename / folder sanitisation ----------

def sanitize_name(name: str, max_len: int = 200) -> str:
    """Replace forbidden chars, trim trailing dots/spaces, enforce max length.

    Empty input returns "_". Pure-whitespace returns "_". A name that becomes
    "." or ".." after cleanup also becomes "_" to avoid path traversal.
    """
    if not name:
        return "_"
    cleaned = _FORBIDDEN_RE.sub("_", name)
    cleaned = cleaned.rstrip(". ").strip()
    if not cleaned or cleaned in (".", ".."):
        return "_"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(". ")
    return cleaned


def folder_name_for_collection(coll: Optional[StegraCollection]) -> str:
    return sanitize_name(coll.name) if coll else UNSORTED_FOLDER_NAME


def file_basename_for_route(route: StegraRoute) -> str:
    return sanitize_name(route.name)


def resolve_collision(
    folder: Path,
    base: str,
    used: set[str],
    ours: Optional[set[str]] = None,
) -> str:
    """Return a basename in `folder` that doesn't collide with `used` or
    foreign on-disk siblings. `ours` is the set of basenames the manifest
    already owns in this folder; those don't count as collisions when found
    on disk. Appends " (N)" if needed."""
    own_set = ours or set()
    candidate = base
    counter = 2
    while True:
        if candidate in used:
            candidate = f"{base} ({counter})"
            counter += 1
            continue
        on_disk = (folder / f"{candidate}.gpx").exists()
        if on_disk and candidate not in own_set:
            candidate = f"{base} ({counter})"
            counter += 1
            continue
        return candidate


# ---------- markdown sidecar ----------

def render_markdown(
    route: StegraRoute,
    collection_name: str,
    synced_at: str,
) -> str:
    """Produce the markdown sidecar contents for one (route, collection) pair."""
    distance_km = round(route.total_distance, 1)
    unpaved_km = round(route.total_unpaved_distance, 1)
    unpaved_pct = route.off_road_pct
    duration_h, duration_m = divmod(int(round(route.total_duration / 60)), 60)
    duration_str = f"{duration_h}h {duration_m:02d}m"

    desc_section = (route.description.strip()
                     if route.description and route.description.strip()
                     else "_no description_")

    color = route.color or "—"
    created = _format_iso(route.created_at)
    modified = _format_iso(route.modified_at)

    url = stegra_route_url(route.id)
    return (
        f"# {route.name}\n"
        f"\n"
        f"**Collection:** {collection_name}  \n"
        f"**Stegra:** [Open in Stegra Studio]({url})\n"
        f"\n"
        f"| Stat | Value |\n"
        f"|---|---|\n"
        f"| Distance | {distance_km} km |\n"
        f"| Duration | {duration_str} |\n"
        f"| Unpaved | {unpaved_km} km ({unpaved_pct}%) |\n"
        f"| Color | {color} |\n"
        f"| Created | {created} |\n"
        f"| Modified | {modified} |\n"
        f"\n"
        f"## Description\n"
        f"\n"
        f"{desc_section}\n"
        f"\n"
        f"---\n"
        f"<sub>route_id: `{route.id}` · collection_id: `{route.collection_ids[0] if route.collection_ids else ''}` "
        f"· synced: {synced_at}</sub>\n"
    )


def parse_markdown_sidecar(text: str) -> dict[str, str]:
    """Extract the stats table from a markdown sidecar.

    Returns a dict with keys like 'Distance', 'Duration', 'Unpaved', 'Color',
    'Created', 'Modified' (only those present). Tolerant of small format
    drift — silently skips rows it can't parse.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|") or set(line) <= {"|", "-", " "}:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 2:
            continue
        key, value = cells
        if not key or key.lower() in ("stat", "value"):
            continue
        out[key] = value
    return out


def read_markdown_sidecar(md_path: Path) -> Optional[dict[str, str]]:
    """Best-effort read+parse. Returns None if the file is missing or empty."""
    if not md_path.exists():
        return None
    try:
        return parse_markdown_sidecar(md_path.read_text())
    except OSError:
        return None


def _format_iso(s: str) -> str:
    """Render an ISO timestamp as `YYYY-MM-DD HH:MM UTC`. Returns input on
    parse failure."""
    if not s:
        return "—"
    # Normalise: drop fractional seconds (any digit count), swap trailing Z
    normalized = re.sub(r"\.\d+", "", s).replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return s


# ---------- target scanning ----------

def scan_target(target_dir: Path) -> LocalManifest:
    """Read manifest (or return an empty one if missing)."""
    existing = read_manifest(target_dir)
    if existing is not None:
        return existing
    return LocalManifest()


# ---------- tree rendering (used by `inspect` and the tail of `auto`) ----------

# Layout columns (every line is prefixed with `_GLOBAL_INDENT` to align with
# the leading `✓` of the summary line):
#   global indent:  2 spaces
#   folder line:    "├── " or "└── " (4 chars) → folder name at col 6
#   route line:     "│   " (or 4 spaces under the last folder)
#                   + "├── " or "└── " → route name at col 10
_GLOBAL_INDENT = "  "
_ROUTE_PREFIX_INNER = "│   "
_ROUTE_PREFIX_LAST = "    "


def render_tree(target_dir: Path, manifest: LocalManifest, console) -> None:  # type: ignore[no-untyped-def]
    """Print a tree view of the target folder with per-route metadata."""
    if not manifest.entries and not manifest.folder_names:
        console.print(
            f"\n[dim]Target {target_dir} has no manifest yet "
            "(first sync will create one).[/dim]"
        )
        return

    console.print(
        f"  [green]✓[/green] {len(manifest.folder_names)} folder(s), "
        f"{len(manifest.entries)} entry(ies) "
        f"[dim](synced {manifest.synced_at or 'never'}, "
        f"cursor={manifest.stegra_cursor})[/dim]"
    )
    console.print()
    console.print(f"{_GLOBAL_INDENT}[bold]{target_dir}[/bold]")

    entries_by_folder: dict[str, list[LocalEntry]] = {}
    for e in manifest.entries:
        head, _, _ = e.relative_path.partition("/")
        entries_by_folder.setdefault(head, []).append(e)

    folder_names = sorted(manifest.folder_names.values(), key=str.lower)
    for i, fname in enumerate(folder_names):
        is_last_folder = i == len(folder_names) - 1
        folder_connector = "└── " if is_last_folder else "├── "
        entries = sorted(
            entries_by_folder.get(fname, []),
            key=lambda x: x.relative_path.lower(),
        )
        count_label = (
            f"({len(entries)} {'route' if len(entries) == 1 else 'routes'})"
        )
        console.print(
            f"{_GLOBAL_INDENT}{folder_connector}"
            f"[cyan]{fname}/[/cyan] [dim]{count_label}[/dim]"
        )
        sub_prefix = _ROUTE_PREFIX_LAST if is_last_folder else _ROUTE_PREFIX_INNER
        for j, entry in enumerate(entries):
            is_last_route = j == len(entries) - 1
            route_connector = "└── " if is_last_route else "├── "
            console.print(
                f"{_GLOBAL_INDENT}{sub_prefix}{route_connector}"
                f"{_route_line(target_dir, entry)}"
            )


def _route_line(target_dir: Path, entry: LocalEntry) -> str:
    gpx_path = target_dir / entry.relative_path
    md_path = gpx_path.with_suffix(".md")
    name = gpx_path.stem

    parts: list[str] = [f"[white]{name}.gpx[/white]"]
    if not gpx_path.exists():
        parts.append("[red][missing .gpx][/red]")

    meta = read_markdown_sidecar(md_path)
    if meta is None:
        parts.append("[dim yellow](no sidecar)[/dim yellow]")
    else:
        if "Created" in meta:
            parts.append(f"[dim]created {meta['Created']}[/dim]")
        if "Modified" in meta:
            parts.append(f"[dim]modified {meta['Modified']}[/dim]")

    url = stegra_route_url(entry.route_id)
    parts.append(f"[link={url}][cyan]Open in Stegra[/cyan][/link]")
    return " · ".join(parts)


# ---------- helper: expected layout from Stegra ----------

@dataclass
class PlannedEntry:
    """What we'd want on disk for one (route, collection) pair."""
    route_id: str
    collection_id: str
    folder_name: str
    file_basename: str           # without extension
    modified_at: str

    @property
    def gpx_relpath(self) -> str:
        return f"{self.folder_name}/{self.file_basename}.gpx"

    @property
    def md_relpath(self) -> str:
        return f"{self.folder_name}/{self.file_basename}.md"


def plan_entries(
    stegra: StegraSnapshot,
    target_dir: Path,
    existing: Optional[LocalManifest] = None,
) -> tuple[dict[tuple[str, str], PlannedEntry], dict[str, str]]:
    """Compute the desired (route, collection) → PlannedEntry map.

    Returns:
      - entries: keyed by (route_id, collection_id)
      - folder_names: collection_id -> folder name (for rename detection)

    Reuses existing manifest file_basename where stable to avoid renaming
    every file on each sync; only chooses new basenames for new entries.
    """
    existing_lookup = existing.by_key() if existing else {}
    out: dict[tuple[str, str], PlannedEntry] = {}
    folder_names: dict[str, str] = {}

    # collection_id -> folder name (Unsorted handled below as cid="")
    for cid, coll in stegra.collections.items():
        folder_names[cid] = folder_name_for_collection(coll)
    folder_names[""] = UNSORTED_FOLDER_NAME

    # Build the set of basenames already owned by the manifest in each
    # destination folder. When the planner reuses an existing entry's
    # basename, the file on disk is ours — not a collision.
    own_basenames_by_folder: dict[str, set[str]] = {}
    if existing:
        for entry in existing.entries:
            head, _, tail = entry.relative_path.partition("/")
            if not tail:
                continue
            target_folder = folder_names.get(entry.collection_id, head)
            own_basenames_by_folder.setdefault(target_folder, set()).add(Path(tail).stem)

    by_folder_used: dict[str, set[str]] = {}

    for route in stegra.routes.values():
        coll_ids: list[str] = list(route.collection_ids) if route.collection_ids else [""]
        for cid in coll_ids:
            folder_name = folder_names.get(cid, UNSORTED_FOLDER_NAME)
            used = by_folder_used.setdefault(folder_name, set())
            ours = own_basenames_by_folder.get(folder_name, set())

            prior = existing_lookup.get((route.id, cid))
            desired = (Path(prior.relative_path).stem if prior
                        else file_basename_for_route(route))
            base = resolve_collision(target_dir / folder_name, desired, used, ours=ours)
            used.add(base)
            out[(route.id, cid)] = PlannedEntry(
                route_id=route.id,
                collection_id=cid,
                folder_name=folder_name,
                file_basename=base,
                modified_at=route.modified_at,
            )

    return out, folder_names


def collection_name(stegra: StegraSnapshot, cid: str) -> str:
    """Friendly name for a Stegra collection_id (Unsorted for empty)."""
    if not cid:
        return UNSORTED_FOLDER_NAME
    coll = stegra.collections.get(cid)
    return coll.name if coll else f"(unknown collection {cid})"
