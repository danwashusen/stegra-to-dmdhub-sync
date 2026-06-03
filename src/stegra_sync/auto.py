"""`stegra-sync auto` — one-command guided sync.

First run: prompts for target folder, workdir, and auth preference, saves
the answers to `~/.config/stegra-sync/config.json`.

Every run: refresh the Stegra token if needed, pull a fresh snapshot,
compute the plan, and execute it. Destructive actions get a single y/N
confirmation; everything else just goes.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
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

from . import auth as auth_mod
from . import local_apply as local_apply_mod
from . import local_diff as local_diff_mod
from . import local_target as local_target_mod
from . import stegra as stegra_mod

CONFIG_DIR = Path(os.environ.get("STEGRA_SYNC_CONFIG_DIR") or
                  Path.home() / ".config" / "stegra-sync")
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_WORKDIR = Path.home() / ".cache" / "stegra-sync"


@dataclass
class AutoConfig:
    target: str                       # absolute path string
    workdir: str                      # absolute path string
    use_apple_events: bool = False
    schema_version: int = 1


# ---------- persistence ----------

def load_config() -> Optional[AutoConfig]:
    if not CONFIG_PATH.exists():
        return None
    data = json.loads(CONFIG_PATH.read_text())
    return AutoConfig(
        target=data["target"],
        workdir=data.get("workdir", str(DEFAULT_WORKDIR)),
        use_apple_events=bool(data.get("use_apple_events", False)),
        schema_version=int(data.get("schema_version", 1)),
    )


def save_config(config: AutoConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(config), indent=2))
    CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------- first-run setup ----------

def first_run_setup(console: Console) -> AutoConfig:
    """Interactively gather config from the user."""
    console.print("[bold]First-time setup for stegra-sync.[/bold]\n")

    default_target = Path.home() / "Documents" / "Stegra Sync"
    console.print("[bold]Where should your synced routes go?[/bold]")
    console.print(
        "  Examples: [cyan]~/Google Drive/My Drive/Rides/[/cyan], "
        "an iCloud or Dropbox path, an external volume, etc."
    )
    target_raw = typer.prompt(
        "Target folder", default=str(default_target), show_default=True,
    )
    target = _expand_path(target_raw)
    if not target.is_absolute():
        console.print(f"[red]Path must be absolute: {target}[/red]")
        raise typer.Exit(code=2)

    console.print(
        f"\n[bold]Where should snapshots and the GPX cache live?[/bold]\n"
        f"  These are internal scratch files; you don't need to touch them."
    )
    workdir_raw = typer.prompt(
        "Workdir", default=str(DEFAULT_WORKDIR), show_default=True
    )
    workdir = _expand_path(workdir_raw)
    if not workdir.is_absolute():
        console.print(f"[red]Path must be absolute: {workdir}[/red]")
        raise typer.Exit(code=2)

    use_apple_events = False
    if sys.platform == "darwin":
        console.print(
            "\n[bold]Capture Stegra token from a running Chrome tab?[/bold]\n"
            "  Zero-paste. Requires [cyan]Chrome → View → Developer →\n"
            "  Allow JavaScript from Apple Events[/cyan] enabled (one-time)."
        )
        use_apple_events = typer.confirm("Use AppleScript?", default=True)

    config = AutoConfig(
        target=str(target),
        workdir=str(workdir),
        use_apple_events=use_apple_events,
    )
    save_config(config)
    console.print(f"\n[green]Saved {CONFIG_PATH}[/green]")
    return config


def _expand_path(s: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(s.strip()))).resolve()


# ---------- the full run ----------

def run_auto(config: AutoConfig, console: Console) -> int:
    """Refresh auth → pull → plan → apply. Returns CLI exit code."""
    target = Path(config.target)
    workdir = Path(config.workdir)
    snapshots_dir = workdir / "snapshots"
    plans_dir = workdir / "plans"
    gpx_dir = workdir / "gpx"

    target.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. Auth refresh
    bundle = _ensure_auth(config, console)

    # 2. Pull from Stegra
    console.print()
    previous = stegra_mod.read_snapshot(snapshots_dir)
    since = previous.cursor if previous else 0
    cursor_label = "full" if since == 0 else f"seq={since}"
    console.print(f"[bold]→ Pulling Stegra changes ({cursor_label})...[/bold]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("Fetching deltas", total=None)
        def on_progress(phase: str, idx: int, total: int, label: str) -> None:
            progress.update(task_id, description=f"Fetching deltas — {label}")
        try:
            raw = stegra_mod.pull_all(bundle, since=since, on_progress=on_progress)
        except stegra_mod.StegraAuthError as e:
            console.print(f"[red]{e}[/red]")
            return 2

    snapshot, delta = stegra_mod.merge(previous, raw)
    stegra_mod.write_snapshot(snapshot, snapshots_dir)
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

    # 3. Refresh GPX cache for changed routes
    total_routes = len(snapshot.routes)
    if total_routes:
        on_disk_before = sum(1 for r in snapshot.routes if (gpx_dir / f"{r}.gpx").exists())
        console.print(
            f"[bold]→ Verifying GPX cache ({total_routes} routes, "
            f"{on_disk_before} already on disk)...[/bold]"
        )
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
                tid = progress.add_task("GPX", total=total_routes, current="")
                def gpx_progress(phase: str, idx: int, total: int, label: str) -> None:
                    if label.endswith("(cached)"):
                        counts["skipped"] += 1
                        name = label[: -len(" (cached)")]
                        suffix = "[dim](cached)[/dim]"
                    else:
                        counts["downloaded"] += 1
                        name = label
                        suffix = ""
                    progress.update(tid, advance=1,
                                     current=f"{name} {suffix}".strip())
                stegra_mod.download_gpx(
                    bundle, snapshot, gpx_dir,
                    previous=previous, on_progress=gpx_progress,
                )
        except stegra_mod.StegraAuthError as e:
            console.print(f"[red]{e}[/red]")
            return 2
        if counts["downloaded"] == 0:
            console.print(
                f"  [green]✓[/green] all {counts['skipped']} GPX files up to date"
            )
        else:
            console.print(
                f"  [green]✓[/green] {counts['downloaded']} downloaded, "
                f"{counts['skipped']} unchanged"
            )

    # 4. Plan
    console.print()
    console.print(f"[bold]→ Computing sync plan for {target}...[/bold]")
    manifest = local_target_mod.scan_target(target)
    plan_obj = local_diff_mod.compute(snapshot, manifest, target)

    plans_dir.mkdir(parents=True, exist_ok=True)
    ts = plan_obj.generated_at.replace(":", "").replace("-", "")[:15]
    plan_path = plans_dir / f"plan-{ts}.json"
    _write_plan(plan_obj, plan_path)

    if not plan_obj.actions:
        console.print(
            f"  [green]✓[/green] no actions — {target} is already in sync"
        )
        local_target_mod.render_tree(target, manifest, console)
        return 0

    summary = plan_obj.summary()
    parts = [f"{n} {k}" for k, n in summary.items()]
    console.print(f"  [green]✓[/green] {len(plan_obj.actions)} action(s): "
                   + ", ".join(parts))
    console.print(f"  [dim]→ {plan_path}[/dim]")

    # 5. Destructive confirmation (single prompt)
    destructive = summary.get("delete_gpx", 0) + summary.get("delete_folder", 0)
    if destructive:
        console.print(
            f"\n[red]This will perform {destructive} destructive action(s) on "
            f"[bold]{target}[/bold].[/red]"
        )
        if not typer.confirm("Proceed?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            return 1

    # 6. Apply
    console.print()
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
        tid = progress.add_task("apply", total=len(plan_obj.actions), current="")
        def on_progress(kind: str, idx: int, total: int, label: str) -> None:
            progress.update(tid, advance=1, current=label)
        result = local_apply_mod.execute_plan(
            plan_obj, snapshot, manifest, target, gpx_dir,
            on_progress=on_progress,
        )
    for r in result.results:
        counts[r.status] += 1

    if result.halted:
        console.print(
            f"  [red]✗ halted after {counts['fail']} failure(s)[/red] — "
            f"{counts['ok']} ok, {counts['skip']} skipped, "
            f"{len(plan_obj.actions) - sum(counts.values())} not attempted"
        )
        for r in result.results:
            if r.status == "fail":
                console.print(f"    [red]✗ {r.action.kind}:[/red] {r.message}")
        console.print(
            "[dim]Resolve the failure and re-run `stegra-sync auto`.[/dim]"
        )
        return 1

    console.print(
        f"  [green]✓ Applied {counts['ok']} action(s) to {target}[/green]"
        + (f" ({counts['skip']} skipped)" if counts["skip"] else "")
    )

    # Final step: render the target as a tree (re-scan to pick up the writes
    # the executor just performed).
    local_target_mod.render_tree(
        target, local_target_mod.scan_target(target), console
    )
    return 0


def _ensure_auth(config: AutoConfig, console: Console) -> auth_mod.AuthBundle:
    bundle = auth_mod.load()
    needs_refresh = (
        bundle is None
        or bundle.stegra_token_likely_expired()
    )
    if not needs_refresh:
        return bundle

    if bundle is None:
        console.print("[bold]→ Capturing Stegra access token...[/bold]")
    else:
        console.print("[bold]→ Stegra token expired — refreshing...[/bold]")
    try:
        return auth_mod.bootstrap(
            use_apple_events=config.use_apple_events,
            allow_paste=True,
        )
    except auth_mod.AuthError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)


def _write_plan(plan_obj, path: Path) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import asdict
    path.write_text(json.dumps({
        "dry_run": plan_obj.dry_run,
        "generated_at": plan_obj.generated_at,
        "summary": plan_obj.summary(),
        "actions": [asdict(a) for a in plan_obj.actions],
    }, indent=2))
