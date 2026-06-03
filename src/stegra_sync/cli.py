"""CLI entry point — `sync <command>`."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import auth as auth_mod
from . import local_apply as local_apply_mod
from . import local_diff as local_diff_mod
from . import local_target as local_target_mod
from . import stegra as stegra_mod
from .plan import SyncPlan

app = typer.Typer(
    name="sync",
    help="One-way sync from Stegra.io to DMD Hub.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

DEFAULT_WORKDIR = Path("./sync-data")


def _require_auth() -> auth_mod.AuthBundle:
    bundle = auth_mod.load()
    if bundle is None:
        console.print("[yellow]No credentials. Run `stegra-sync auth` first.[/yellow]")
        raise typer.Exit(code=2)
    if bundle.stegra_token_likely_expired():
        console.print("[yellow]Stegra token has likely expired. Run `stegra-sync auth` again.[/yellow]")
        raise typer.Exit(code=2)
    return bundle


@app.command()
def auth(
    apple_events: bool = typer.Option(
        False, "--apple-events",
        help="Extract the Stegra token from a live Chrome tab via AppleScript. "
             "Requires Chrome → View → Developer → Allow JavaScript from Apple Events.",
    ),
) -> None:
    """Capture a Stegra access token and write auth.json.

    Default flow: paste the Stegra token (a DevTools console snippet that
    copies the token to your clipboard is shown on screen).

    With --apple-events: the token is pulled directly from a running
    stegra.io tab in Chrome — no paste required.
    """
    auth_mod.bootstrap(use_apple_events=apple_events)


@app.command()
def pull(
    workdir: Path = typer.Option(DEFAULT_WORKDIR, "--workdir", "-w",
                                  help="Where to write snapshots and gpx files."),
    full: bool = typer.Option(False, "--full",
                              help="Ignore cached cursor and re-pull everything."),
) -> None:
    """Pull a Stegra snapshot and download per-route GPX files."""
    bundle = _require_auth()
    snapshots_dir = workdir / "snapshots"
    gpx_dir = workdir / "gpx"

    previous = stegra_mod.read_snapshot(snapshots_dir)
    since = 0 if (full or previous is None) else previous.cursor

    cursor_label = "full" if since == 0 else f"seq={since}"
    console.print(f"[bold]→ Pulling Stegra changes ({cursor_label})...[/bold]")

    # Phase 1: paginated sync/pull
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("Fetching deltas", total=None)
        def pull_progress(phase: str, idx: int, total: int, label: str) -> None:
            progress.update(task_id, description=f"Fetching deltas — {label}")
        try:
            raw = stegra_mod.pull_all(bundle, since=since, on_progress=pull_progress)
        except stegra_mod.StegraAuthError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2)

    snapshot, delta = stegra_mod.merge(previous, raw)
    out_path = stegra_mod.write_snapshot(snapshot, snapshots_dir)

    # Delta summary line: what just changed (vs. total state, shown below)
    if delta.empty:
        console.print(
            f"  [green]✓[/green] no changes "
            f"([dim]{len(snapshot.routes)} routes, "
            f"{len(snapshot.collections)} collections already on disk[/dim])"
        )
    else:
        bits = []
        if delta.routes_added: bits.append(f"+{delta.routes_added} routes")
        if delta.routes_updated: bits.append(f"~{delta.routes_updated} routes")
        if delta.routes_deleted: bits.append(f"-{delta.routes_deleted} routes")
        if delta.collections_added: bits.append(f"+{delta.collections_added} collections")
        if delta.collections_updated: bits.append(f"~{delta.collections_updated} collections")
        if delta.collections_deleted: bits.append(f"-{delta.collections_deleted} collections")
        console.print(
            f"  [green]✓[/green] " + ", ".join(bits) +
            f" [dim](total: {len(snapshot.routes)} routes, "
            f"{len(snapshot.collections)} collections)[/dim]"
        )
    console.print(f"  [dim]→ {out_path} (cursor={snapshot.cursor})[/dim]")

    # Phase 2: per-route GPX download. Even on a no-op pull we iterate the full
    # snapshot so we can verify the cache and report what's already on disk.
    total_routes = len(snapshot.routes)
    if total_routes == 0:
        console.print("[bold]→ No routes to sync.[/bold]")
        return

    on_disk_before = sum(1 for r in snapshot.routes if (gpx_dir / f"{r}.gpx").exists())
    console.print(f"[bold]→ Verifying GPX cache ({total_routes} routes, "
                  f"{on_disk_before} already on disk)...[/bold]")
    counts = {"downloaded": 0, "skipped": 0}
    try:
        with Progress(
            TextColumn("  "),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[cyan]{task.fields[current]}[/cyan]"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task_id = progress.add_task("GPX", total=total_routes, current="")
            def gpx_progress(phase: str, idx: int, total: int, label: str) -> None:
                if label.endswith("(cached)"):
                    counts["skipped"] += 1
                    suffix = "[dim](cached)[/dim]"
                    name = label[: -len(" (cached)")]
                else:
                    counts["downloaded"] += 1
                    suffix = ""
                    name = label
                progress.update(
                    task_id, advance=1, current=f"{name} {suffix}".strip()
                )
            stegra_mod.download_gpx(
                bundle, snapshot, gpx_dir,
                previous=previous, on_progress=gpx_progress,
            )
    except stegra_mod.StegraAuthError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    if counts["downloaded"] == 0:
        console.print(
            f"  [green]✓[/green] all {counts['skipped']} GPX files up to date "
            f"[dim]→ {gpx_dir}[/dim]"
        )
    else:
        console.print(
            f"  [green]✓[/green] {counts['downloaded']} downloaded, "
            f"{counts['skipped']} unchanged [dim]→ {gpx_dir}[/dim]"
        )

    _print_overview(snapshot)


def _print_overview(snapshot) -> None:  # type: ignore[no-untyped-def]
    table = Table(title="Stegra Collections", show_lines=False)
    table.add_column("Name")
    table.add_column("Routes", justify="right")
    table.add_column("POIs", justify="right")
    table.add_column("Modified", overflow="fold")
    for c in snapshot.collections.values():
        table.add_row(c.name, str(len(c.route_ids)), str(len(c.poi_ids)), c.modified_at)
    unsorted = snapshot.unsorted_routes()
    if unsorted:
        table.add_row("[dim](Unsorted — synthetic)[/dim]", str(len(unsorted)), "—", "")
    console.print(table)


@app.command()
def inspect(
    target: Path = typer.Option(..., "--target", "-t",
        help="Path to the local target folder to inspect."),
) -> None:
    """Read the local target's manifest and show its current state."""
    target.mkdir(parents=True, exist_ok=True)
    manifest = local_target_mod.scan_target(target)
    if not manifest.entries and not manifest.folder_names:
        console.print(f"[dim]Target {target} has no manifest yet (first sync will create one).[/dim]")
        return
    by_collection: dict[str, int] = {}
    for e in manifest.entries:
        by_collection[e.collection_id] = by_collection.get(e.collection_id, 0) + 1

    console.print(
        f"  [green]✓[/green] {len(manifest.folder_names)} folder(s), "
        f"{len(manifest.entries)} entry(ies) "
        f"[dim](synced {manifest.synced_at or 'never'}, cursor={manifest.stegra_cursor})[/dim]"
    )

    table = Table(title=f"Local target: {target}", show_lines=False)
    table.add_column("Collection ID", overflow="fold")
    table.add_column("Folder")
    table.add_column("Entries", justify="right")
    for cid, name in sorted(manifest.folder_names.items(),
                              key=lambda kv: kv[1].lower()):
        count = by_collection.get(cid, 0)
        cid_label = cid if cid else "[dim](unsorted)[/dim]"
        table.add_row(cid_label, name, str(count))
    console.print(table)


@app.command()
def plan(
    target: Path = typer.Option(..., "--target", "-t",
        help="Path to the local target folder."),
    workdir: Path = typer.Option(DEFAULT_WORKDIR, "--workdir", "-w"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Also list each action's reason in the preview.",
    ),
) -> None:
    """Diff Stegra snapshot vs the local target's manifest. Emits a plan."""
    snapshots_dir = workdir / "snapshots"
    plans_dir = workdir / "plans"

    stegra_snapshot = stegra_mod.read_snapshot(snapshots_dir)
    if stegra_snapshot is None:
        console.print("[yellow]No Stegra snapshot. Run `stegra-sync pull` first.[/yellow]")
        raise typer.Exit(code=2)

    target.mkdir(parents=True, exist_ok=True)
    manifest = local_target_mod.scan_target(target)

    console.print("[bold]→ Computing sync plan...[/bold]")
    plan_obj = local_diff_mod.compute(stegra_snapshot, manifest, target)

    plans_dir.mkdir(parents=True, exist_ok=True)
    ts = plan_obj.generated_at.replace(":", "").replace("-", "")[:15]
    out_path = plans_dir / f"plan-{ts}.json"
    _write_plan(plan_obj, out_path)

    if not plan_obj.actions:
        console.print(f"  [green]✓[/green] no actions — {target} is already in sync with Stegra")
        console.print(f"  [dim]→ {out_path}[/dim]")
        return

    summary = plan_obj.summary()
    parts = [f"{n} {k}" for k, n in summary.items()]
    console.print(
        f"  [green]✓[/green] {len(plan_obj.actions)} action(s): "
        + ", ".join(parts)
    )
    console.print(f"  [dim]→ {out_path}[/dim]")
    _print_plan_preview(plan_obj, verbose=verbose)


def _write_plan(plan_obj: SyncPlan, path: Path) -> None:
    import json
    from dataclasses import asdict
    path.write_text(json.dumps({
        "dry_run": plan_obj.dry_run,
        "generated_at": plan_obj.generated_at,
        "summary": plan_obj.summary(),
        "actions": [asdict(a) for a in plan_obj.actions],
    }, indent=2))


def _print_plan_preview(plan_obj: SyncPlan, verbose: bool) -> None:
    # Group by action kind for a tidy table
    by_kind: dict[str, list] = {}
    for action in plan_obj.actions:
        by_kind.setdefault(action.kind, []).append(action)

    icons = {
        "create_folder": "📁＋",
        "rename_folder": "📁✎",
        "delete_folder": "📁－",
        "upload_gpx": "⬆ ",
        "update_gpx_metadata": "✎ ",
        "delete_gpx": "🗑 ",
    }

    table = Table(title="Sync Plan (dry-run)", show_lines=False)
    table.add_column("")
    table.add_column("Item", overflow="fold")
    if verbose:
        table.add_column("Reason", overflow="fold")

    for kind in ("create_folder", "rename_folder", "delete_folder",
                  "upload_gpx", "update_gpx_metadata", "delete_gpx"):
        actions = by_kind.get(kind, [])
        for a in actions:
            label = _describe_action(a)
            row = [icons.get(kind, "•"), label]
            if verbose:
                row.append(f"[dim]{a.reason}[/dim]")
            table.add_row(*row)
    console.print(table)


def _describe_action(a) -> str:  # type: ignore[no-untyped-def]
    p = a.payload
    if a.kind == "create_folder":
        return f"Create folder [cyan]{p.get('name')}[/cyan]"
    if a.kind == "rename_folder":
        return f"Rename folder [dim]{p.get('old_name')}[/dim] → [cyan]{p.get('new_name')}[/cyan]"
    if a.kind == "delete_folder":
        return f"Delete folder [red]{p.get('folder_name', p.get('name'))}[/red]"
    if a.kind == "upload_gpx":
        replaces = p.get("replaces_relative_path")
        action = "Update" if replaces else "Write"
        return (f"{action} [cyan]{p.get('stegra_route_name')}[/cyan] → "
                f"[dim]{p.get('relative_path')}[/dim]")
    if a.kind == "delete_gpx":
        return f"Delete [red]{p.get('relative_path')}[/red] [dim](orphan)[/dim]"
    return f"{a.kind}: {p}"


@app.command()
def apply(
    target: Path = typer.Option(..., "--target", "-t",
        help="Path to the local target folder."),
    workdir: Path = typer.Option(DEFAULT_WORKDIR, "--workdir", "-w"),
    plan_file: Optional[Path] = typer.Option(
        None, "--plan", help="Specific plan JSON to execute (default: latest in plans/).",
    ),
    execute: bool = typer.Option(
        False, "--execute",
        help="Actually perform the writes (default: dry-run preview only).",
    ),
) -> None:
    """Execute a sync plan against the local target (default: dry-run preview)."""
    snapshots_dir = workdir / "snapshots"
    plans_dir = workdir / "plans"
    gpx_dir = workdir / "gpx"

    if plan_file is None:
        plan_file = _latest_plan_file(plans_dir)
        if plan_file is None:
            console.print("[yellow]No plan found. Run `stegra-sync plan` first.[/yellow]")
            raise typer.Exit(code=2)
    if not plan_file.exists():
        console.print(f"[red]Plan file not found: {plan_file}[/red]")
        raise typer.Exit(code=2)
    plan_obj = _load_plan(plan_file)
    console.print(f"[dim]Plan: {plan_file}[/dim]")

    if not plan_obj.actions:
        console.print("  [green]✓[/green] plan has no actions — nothing to apply")
        return

    summary = plan_obj.summary()
    parts = [f"{n} {k}" for k, n in summary.items()]
    console.print(f"[bold]{len(plan_obj.actions)} action(s):[/bold] " + ", ".join(parts))
    _print_plan_preview(plan_obj, verbose=False)

    if not execute:
        console.print(
            "[yellow]Dry-run only.[/yellow] Re-run with [bold]--execute[/bold] to apply."
        )
        return

    stegra_snapshot = stegra_mod.read_snapshot(snapshots_dir)
    if stegra_snapshot is None:
        console.print("[red]Stegra snapshot missing. Run `pull` again.[/red]")
        raise typer.Exit(code=2)
    target.mkdir(parents=True, exist_ok=True)
    manifest = local_target_mod.scan_target(target)

    destructive = summary.get("delete_gpx", 0) + summary.get("delete_folder", 0)
    if destructive:
        console.print(
            f"[red]Performing {destructive} destructive action(s) on "
            f"[bold]{target}[/bold][/red]"
        )

    console.print(f"[bold]→ Applying plan to {target}...[/bold]")
    counts = {"ok": 0, "fail": 0, "skip": 0}
    with Progress(
        TextColumn("  "),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[cyan]{task.fields[current]}[/cyan]"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("apply", total=len(plan_obj.actions), current="")

        def on_progress(kind: str, idx: int, total: int, label: str) -> None:
            progress.update(task_id, advance=1, current=label)

        result = local_apply_mod.execute_plan(
            plan_obj, stegra_snapshot, manifest, target, gpx_dir,
            on_progress=on_progress,
        )
    for r in result.results:
        counts[r.status] += 1

    table = Table(title="Apply results", show_lines=False)
    table.add_column("")
    table.add_column("Action")
    table.add_column("Detail", overflow="fold")
    for r in result.results:
        icon = {"ok": "[green]✓[/green]", "fail": "[red]✗[/red]", "skip": "[yellow]-[/yellow]"}[r.status]
        table.add_row(icon, r.action.kind, r.message)
    console.print(table)

    if result.halted:
        console.print(
            f"[red]Halted after {counts['fail']} failure(s).[/red] "
            f"{counts['ok']} succeeded, {counts['skip']} skipped, "
            f"{len(plan_obj.actions) - sum(counts.values())} not attempted."
        )
        console.print(
            "[dim]Re-run `plan` to compute a fresh plan, then `apply` again.[/dim]"
        )
        raise typer.Exit(code=1)

    console.print(
        f"[green]✓ Applied {counts['ok']} action(s) to {target}[/green]"
        + (f" ({counts['skip']} skipped)" if counts['skip'] else "")
    )
    console.print("[dim]Run `plan` again to verify drift is zero.[/dim]")


def _latest_plan_file(plans_dir: Path) -> Optional[Path]:
    if not plans_dir.exists():
        return None
    plans = sorted(plans_dir.glob("plan-*.json"))
    return plans[-1] if plans else None


def _load_plan(path: Path) -> SyncPlan:
    import json
    from .plan import PlanAction
    data = json.loads(path.read_text())
    return SyncPlan(
        dry_run=data.get("dry_run", True),
        generated_at=data.get("generated_at", ""),
        actions=[PlanAction(**a) for a in data.get("actions", [])],
    )


if __name__ == "__main__":
    app()
