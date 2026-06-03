"""Data models for both sides of the sync."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------- Stegra ----------

@dataclass
class StegraRoute:
    id: str
    name: str
    description: str
    private: bool
    created_at: str           # ISO8601 string from API
    modified_at: str          # ISO8601 string from API
    total_distance: float     # km
    total_unpaved_distance: float  # km
    total_duration: float     # seconds
    color: str                # hex "#RRGGBB"
    change_seq: int
    owner_name: str
    owner_user_id: str
    # collection_ids[] is derived from StegraCollection.route_ids[]
    collection_ids: list[str] = field(default_factory=list)

    @property
    def off_road_pct(self) -> int:
        if self.total_distance <= 0:
            return 0
        return round(100 * self.total_unpaved_distance / self.total_distance)


@dataclass
class StegraCollection:
    id: str
    name: str
    description: str
    modified_at: str
    route_ids: list[str]
    poi_ids: list[str]
    change_seq: int


@dataclass
class StegraSnapshot:
    cursor: int                                      # max_seq for next pull
    pulled_at: str                                   # ISO8601
    routes: dict[str, StegraRoute]                   # by route id
    collections: dict[str, StegraCollection]         # by collection id

    def routes_in_collection(self, cid: str) -> list[StegraRoute]:
        coll = self.collections.get(cid)
        if not coll:
            return []
        return [self.routes[rid] for rid in coll.route_ids if rid in self.routes]

    def unsorted_routes(self) -> list[StegraRoute]:
        assigned = set()
        for c in self.collections.values():
            assigned.update(c.route_ids)
        return [r for r in self.routes.values() if r.id not in assigned]


# ---------- DMD Hub ----------

@dataclass
class DmdGpx:
    id: str
    title: str
    description: str           # Public Description (rich text); holds sync state footer
    public: bool
    approved: bool
    difficulty: str
    off_road_percentage: int
    tags: str
    gpx_length_km: float
    created: int               # epoch seconds
    modified: int


@dataclass
class DmdFolder:
    id: str
    name: str
    gpx_ids: list[str]


@dataclass
class DmdSnapshot:
    pulled_at: str
    folders: dict[str, DmdFolder]
    gpx: dict[str, DmdGpx]      # by gpx id


# ---------- Sync state (embedded in DMD Public Description) ----------

SYNC_STATE_VERSION = 1

@dataclass
class SyncState:
    route_id: str            # Stegra route UUID
    collection_id: str       # Stegra collection UUID (or "" for unsorted bucket)
    modified_at: str         # Stegra modified_at at last sync
    synced_at: str           # ISO8601 of last write
    version: int = SYNC_STATE_VERSION
