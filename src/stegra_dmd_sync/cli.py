"""CLI entry point — `sync <command>`."""
from __future__ import annotations

from pathlib import Path

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
from . import dmd as dmd_mod
from . import stegra as stegra_mod

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
        console.print("[yellow]No credentials. Run `stegra-to-dmdhub-sync auth` first.[/yellow]")
        raise typer.Exit(code=2)
    if bundle.stegra_token_likely_expired():
        console.print("[yellow]Stegra token has likely expired. Run `stegra-to-dmdhub-sync auth` again.[/yellow]")
        raise typer.Exit(code=2)
    return bundle


@app.command()
def auth(
    apple_events: bool = typer.Option(
        False, "--apple-events",
        help="Extract the Stegra token from a live Chrome tab via AppleScript. "
             "Requires Chrome → View → Developer → Allow JavaScript from Apple Events.",
    ),
    paste_cookies: bool = typer.Option(
        False, "--paste-cookies",
        help="Skip auto-reading DMD cookies from Chrome's store; prompt for a "
             "DevTools-copied Cookie header instead. Useful when macOS Chrome's "
             "encryption blocks browser-cookie3.",
    ),
) -> None:
    """Capture Stegra token + DMD cookies, write auth.json.

    Default flow: paste the Stegra token (DevTools snippet shown on screen),
    DMD cookies are read automatically from Chrome's cookie store.

    With --apple-events: token is also pulled automatically — no paste at all.
    With --paste-cookies: skip the cookie-store read and use a DevTools paste.
    """
    auth_mod.bootstrap(
        use_apple_events=apple_events,
        paste_cookies=paste_cookies,
    )


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
    workdir: Path = typer.Option(DEFAULT_WORKDIR, "--workdir", "-w"),
) -> None:
    """Enumerate DMD Hub folders + GPX records into snapshots/dmd.json."""
    bundle = _require_auth()
    snapshots_dir = workdir / "snapshots"

    console.print("[bold]→ Enumerating DMD Hub library...[/bold]")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("Listing", total=None)
        def on_progress(phase: str, idx: int, total: int, label: str) -> None:
            progress.update(task_id, description=f"Listing — {label}")
        try:
            snapshot = dmd_mod.fetch_snapshot(bundle, on_progress=on_progress)
        except dmd_mod.DmdAuthError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2)

    out_path = dmd_mod.write_snapshot(snapshot, snapshots_dir)

    # Summary counts (excluding the synthetic Root folder, which is always there)
    user_folder_count = max(0, len(snapshot.folders) - 1)
    managed = sum(1 for g in snapshot.gpx.values() if g.sync_state is not None)
    unmanaged = len(snapshot.gpx) - managed
    console.print(
        f"  [green]✓[/green] {user_folder_count} folders, {len(snapshot.gpx)} GPX files "
        f"([cyan]{managed}[/cyan] managed, [dim]{unmanaged} unmanaged[/dim])"
    )
    console.print(f"  [dim]→ {out_path}[/dim]")

    _print_dmd_overview(snapshot)


def _print_dmd_overview(snapshot) -> None:  # type: ignore[no-untyped-def]
    from .models import ROOT_FOLDER_ID
    table = Table(title="DMD Hub Folders", show_lines=False)
    table.add_column("Folder")
    table.add_column("GPX", justify="right")
    table.add_column("Managed", justify="right")

    def row(folder) -> None:
        managed = sum(1 for gid in folder.gpx_ids
                       if snapshot.gpx.get(gid) and snapshot.gpx[gid].sync_state)
        name = "[dim]Root[/dim]" if folder.id == ROOT_FOLDER_ID else folder.name
        table.add_row(name, str(len(folder.gpx_ids)), str(managed))

    # Root first, then sorted user folders
    if ROOT_FOLDER_ID in snapshot.folders:
        row(snapshot.folders[ROOT_FOLDER_ID])
    for f in sorted(
        (f for fid, f in snapshot.folders.items() if fid != ROOT_FOLDER_ID),
        key=lambda x: x.name.lower(),
    ):
        row(f)
    console.print(table)


@app.command()
def plan(
    workdir: Path = typer.Option(DEFAULT_WORKDIR, "--workdir", "-w"),
) -> None:
    """[STUB] Diff Stegra snapshot vs DMD snapshot, emit a sync plan."""
    console.print("[yellow]plan: depends on `inspect` (not yet implemented).[/yellow]")
    raise typer.Exit(code=1)


@app.command()
def apply(
    dry_run: bool = typer.Option(True, "--dry-run/--execute"),
    workdir: Path = typer.Option(DEFAULT_WORKDIR, "--workdir", "-w"),
) -> None:
    """[DISABLED in v1] Execute a sync plan."""
    if not dry_run:
        console.print("[red]Real writes are disabled in v1.[/red]")
        raise typer.Exit(code=1)
    console.print("[yellow]apply --dry-run: depends on `plan` (not yet implemented).[/yellow]")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
