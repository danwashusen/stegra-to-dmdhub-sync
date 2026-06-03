"""Sync plan — declarative description of intended writes.

The plan is what `sync plan` emits and `sync apply` would execute. Keeping it
serialisable means real writes can later be implemented without changing the
planning logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ActionKind = Literal[
    "create_folder",
    "rename_folder",
    "delete_folder",
    "upload_gpx",
    "update_gpx_metadata",
    "delete_gpx",
]


@dataclass
class PlanAction:
    kind: ActionKind
    reason: str
    # Loosely-typed payload keyed by action; documented per kind in the README.
    payload: dict


@dataclass
class SyncPlan:
    dry_run: bool = True
    generated_at: str = ""
    actions: list[PlanAction] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            out[a.kind] = out.get(a.kind, 0) + 1
        return out
