"""SQLite-backed configuration shim.

Preserves the original `Config` public surface (`config.api`, `config.serial`,
`config.image_proc`, `config.camera`, `config.headstamps`, `config.save()`)
so the existing tab code does not need to change. App-level settings live in
the `settings` table; headstamps live in their own table and are scoped to
the currently active model.

The DEFAULTS structure stays here as the canonical fallback when no settings
row exists yet.
"""
from __future__ import annotations

import copy
from typing import Any

from .db import Database
from .repository import HeadstampParentRepo, HeadstampRepo, SettingsRepo


DEFAULT_INIT_SETTINGS: dict[str, int | str] = {
    "feedhomingoffset": 0,
    "sorthomingoffset": 0,
    "feedspeed": 90,
    "sortspeed": 90,
    "feedsteps": 70,
    "sortsteps": 20,
    "slotdropdelay": 300,
    "notificationdelay": 160,
    "automotorstandbytimeout": 0,
    "feedmotorcurrent": 900,
    "sortmotorcurrent": 900,
    "fan": 100,
    "debounceTimeout": 500,
    "debounceTime": 300,
    "cameraledlevel": 15,
    "airdropenabled": 0,
    "airdroppredelay": 50,
    "airdropdsignalduration": 70,
    "airdroppostdelay": 50,
}


DEFAULTS: dict[str, Any] = {
    "api": {
        "endpoint_url": "http://localhost:8000",
        "api_key": "nokey",
        "model": "9mm",
        "prompt": "Not used for local AI Server",
        "image_quality": 100,
        "image_scale": 100,
    },
    "serial": {
        "port": "",
        "baud": 9600,
        "slot_quantity": 8,
        "handshake_timeout_s": 4.0,
        "init_on_startup": False,
        "init_settings": dict(DEFAULT_INIT_SETTINGS),
    },
    "image_proc": {
        "strategy": "hough",
        "primer_mode": "hide",
        "primer_radius": 135,
        "hough": {
            "dp": 2.0,
            "min_dist": 500,
            "param1": 100,
            "param2": 60,
            "min_radius": 150,
            "max_radius": 250,
        },
        "linescan": {
            "scan_precision": 1,
            "scan_sensitivity": 5.0,
            "padding_pct": 5,
            "bg_cliff": 0,
        },
    },
    "camera": {
        "device_index": 0,
        "device_chosen": False,
        "width": 1920,
        "height": 1080,
        "preferred_vid": "",
        "preferred_pid": "",
        "prefer_by_usb_id": True,
    },
}


_SECTIONS = ("api", "serial", "image_proc", "camera")
# AI Config mode (no active model) keeps its own headstamp list. The DB
# `headstamps` table requires a real model_id FK, so we stash AI Config
# headstamps in the key/value settings table instead.
_AI_HEADSTAMPS_KEY = "ai_config_headstamps"
# Per-model runtime toggle for parent-classification routing. Keyed by model
# id (``use_parent_runtime:<id>``); a per-installation preference, not exported.
_USE_PARENT_RUNTIME_KEY = "use_parent_runtime"

# App-level run options.
_RUN_CONFIDENCE_FLOOR_KEY = "run_confidence_floor"
_RUN_STORE_IMAGES_KEY = "run_store_images"
# Valid "store images" modes (internal value -> meaning):
#   none   never store
#   above  store only when confidence >= floor
#   below  store only when confidence < floor
#   all    store every classified case
STORE_IMAGES_MODES = ("none", "above", "below", "all")
DEFAULT_CONFIDENCE_FLOOR = 95

# Package mode (batch sorting). When on, the same headstamp may be assigned to
# several slots; the run fills one slot to `run_package_size` then advances to
# the next configured slot, halting when every slot for a headstamp is full.
# Package assignments are kept separate from the single-slot routing so a
# headstamp can live in multiple bins at once.
_RUN_PACKAGE_MODE_KEY = "run_package_mode"
_RUN_PACKAGE_SIZE_KEY = "run_package_size"
_PACKAGE_SLOTS_KEY = "package_slots"
DEFAULT_PACKAGE_SIZE = 50

# Auto-select trays: when on, an above-floor headstamp that isn't assigned to
# any slot is auto-routed to the first empty slot.
_RUN_AUTO_SELECT_KEY = "run_auto_select_trays"

# Sort While Training: send xf:<slot> for a labelled case instead of xf:0
# during training.
_SORT_WHILE_TRAINING_KEY = "sort_while_training"
_SOFTWARE_PROFILE_KEY = "software_profile"
SOFTWARE_PROFILES = ("classification_only", "full")


def _merge_defaults(defaults: Any, loaded: Any) -> Any:
    """Recursive default merge: any key missing in `loaded` falls back to defaults."""
    if isinstance(defaults, dict) and isinstance(loaded, dict):
        out: dict[str, Any] = {}
        for k, v in defaults.items():
            out[k] = _merge_defaults(v, loaded.get(k, v))
        for k in loaded:
            if k not in out:
                out[k] = loaded[k]
        return out
    return loaded if loaded is not None else defaults


class Config:
    """In-memory mirror of the persisted app settings.

    Headstamps live in their own SQLite table (managed by HeadstampRepo) and
    are read fresh on every access — they are NOT part of the cached
    settings snapshot, because they get mutated through several call paths
    (Models tab editor, Train tab Save, Community import) and caching a
    stale snapshot here used to silently wipe rows whenever any other tab
    happened to call ``config.save()``.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.settings = SettingsRepo(db)
        self.headstamps_repo = HeadstampRepo(db)
        self.parents_repo = HeadstampParentRepo(db)
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)

    def load(self) -> "Config":
        for key in _SECTIONS:
            stored = self.settings.get(key)
            if stored is not None:
                self.data[key] = _merge_defaults(
                    copy.deepcopy(DEFAULTS[key]), stored
                )
            else:
                self.data[key] = copy.deepcopy(DEFAULTS[key])
        return self

    def reload_headstamps_for_active_model(self) -> None:
        """No-op kept for callers (`tab_models._activate`).

        Headstamps are now always read fresh from the DB so there is no
        cached copy to invalidate. The method stays so existing call sites
        don't need to change.
        """
        return

    def save(self) -> None:
        """Persist the cached settings sections. Does NOT touch headstamps."""
        with self.db.transaction() as _:
            for key in _SECTIONS:
                self.settings.set(key, self.data[key])

    # --- public surface (matches old JSON Config) ---------------------------

    @property
    def api(self) -> dict[str, Any]:
        return self.data["api"]

    @property
    def headstamps(self) -> list[dict[str, Any]]:
        """Always returns a freshly-read list of headstamps for the active model.

        In AI Config mode (no active local model) headstamps live in a
        settings entry instead of the model-scoped headstamps table.
        """
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            return list(self._read_ai_headstamps())
        rows = self.headstamps_repo.list_for_model(active_id)
        return [{"name": r.name, "slot": r.slot} for r in rows]

    # ----- AI Config-mode headstamp storage ---------------------------------

    def _read_ai_headstamps(self) -> list[dict[str, Any]]:
        raw = self.settings.get(_AI_HEADSTAMPS_KEY) or []
        out: list[dict[str, Any]] = []
        for entry in raw:
            name = (entry or {}).get("name") if isinstance(entry, dict) else None
            if not name:
                continue
            out.append({"name": str(name), "slot": int((entry or {}).get("slot", 0))})
        return out

    def _write_ai_headstamps(self, entries: list[dict[str, Any]]) -> None:
        self.settings.set(_AI_HEADSTAMPS_KEY, entries)

    def remote_headstamps(self) -> list[dict[str, Any]]:
        """Return the saved runtime labels for the remote API model.

        This is independent of the active local-model selection so Maintenance
        tools can prepare the remote runtime before switching to AI Config.
        """
        return list(self._read_ai_headstamps())

    def synchronize_remote_headstamps(self, names: list[str]) -> dict[str, int]:
        """Replace the remote label set with the server-advertised names.

        Existing slot assignments are retained by case-insensitive name.
        Labels no longer advertised are removed from the runtime list; portable
        Saved Bins files remain untouched and can preserve those names.
        """
        previous = {
            str(item["name"]).strip().casefold(): int(item.get("slot", 0))
            for item in self._read_ai_headstamps()
            if str(item.get("name") or "").strip()
        }
        synchronized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_name in names:
            name = str(raw_name or "").strip()
            key = name.casefold()
            if not name or key in seen:
                continue
            synchronized.append({"name": name, "slot": previous.get(key, 0)})
            seen.add(key)
        self._write_ai_headstamps(synchronized)
        return {
            "received": len(synchronized),
            "added": len(seen - previous.keys()),
            "removed": len(previous.keys() - seen),
        }

    def set_remote_headstamp_slots(self, slots_by_name: dict[str, int]) -> None:
        """Replace remote runtime slots while retaining its synchronized names."""
        wanted = {
            str(name).strip().casefold(): int(slot)
            for name, slot in slots_by_name.items()
            if str(name or "").strip()
        }
        entries = self._read_ai_headstamps()
        for entry in entries:
            key = str(entry.get("name") or "").strip().casefold()
            entry["slot"] = wanted.get(key, 0)
        self._write_ai_headstamps(entries)

    # ----- headstamp mutations (write straight through to the repo) ---------

    def add_headstamp(self, name: str, slot: int = 0) -> bool:
        """Add a headstamp for the active context. Returns False if `name`
        is empty or already present.
        """
        if not name:
            return False
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            current = self._read_ai_headstamps()
            if any(e["name"] == name for e in current):
                return False
            current.append({"name": name, "slot": int(slot)})
            self._write_ai_headstamps(current)
            return True
        existing = {h.name for h in self.headstamps_repo.list_for_model(active_id)}
        if name in existing:
            return False
        try:
            self.headstamps_repo.add(active_id, name, slot)
        except Exception:
            return False
        return True

    def remove_headstamp(self, name: str) -> bool:
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            current = self._read_ai_headstamps()
            remaining = [e for e in current if e["name"] != name]
            if len(remaining) == len(current):
                return False
            self._write_ai_headstamps(remaining)
            return True
        for h in self.headstamps_repo.list_for_model(active_id):
            if h.name == name:
                self.headstamps_repo.delete(h.id)
                return True
        return False

    def clear_headstamps(self) -> None:
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            self._write_ai_headstamps([])
            return
        self.headstamps_repo.clear_for_model(active_id)

    def set_headstamps(self, entries: list[dict[str, Any]]) -> None:
        """Replace all headstamps for the active context (model or AI Config)."""
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            normalised = [
                {"name": str(e["name"]), "slot": int(e.get("slot", 0))}
                for e in entries if e.get("name")
            ]
            self._write_ai_headstamps(normalised)
            return
        self.headstamps_repo.replace_for_model(active_id, entries)

    def set_headstamp_slot(self, name: str, slot: int) -> bool:
        """Update the slot assignment for a single headstamp. Returns False if
        the headstamp doesn't exist for the active context. Used by the Run
        tab's slot-details checkboxes — those used to mutate the dicts
        returned by ``config.headstamps`` directly, but that's a no-op now
        that the property reads fresh on every access.
        """
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            current = self._read_ai_headstamps()
            for entry in current:
                if entry["name"] == name:
                    entry["slot"] = int(slot)
                    self._write_ai_headstamps(current)
                    return True
            return False
        for h in self.headstamps_repo.list_for_model(active_id):
            if h.name == name:
                self.headstamps_repo.update_slot(h.id, int(slot))
                return True
        return False

    @property
    def serial(self) -> dict[str, Any]:
        return self.data["serial"]

    @property
    def image_proc(self) -> dict[str, Any]:
        return self.data["image_proc"]

    @property
    def camera(self) -> dict[str, Any]:
        return self.data["camera"]

    def slot_for_headstamp(self, name: str) -> int | None:
        """Resolve the physical bin for a classified label.

        In parent-classification mode a child label routes to *its parent's*
        slot, while an orphan (parentless) headstamp routes to its own slot.
        Otherwise routing is the standard per-headstamp lookup. Returns None
        when the label maps to nothing (caller falls back to catch-all).
        """
        mid = self.settings.get_active_model_id()
        if mid is not None and self.use_parent_classifications:
            parents = {p.id: p for p in self.parents_repo.list_for_model(mid)}
            if parents:
                headstamps = self.headstamps_repo.list_for_model(mid)
                hs = next((h for h in headstamps if h.name == name), None)
                if hs is not None:
                    if hs.parent_id is not None and hs.parent_id in parents:
                        return int(parents[hs.parent_id].slot)
                    return int(hs.slot)
                # The label may already be a parent name (parent-trained model).
                parent = next((p for p in parents.values() if p.name == name), None)
                return int(parent.slot) if parent is not None else None

        wanted = str(name or "").strip().casefold()
        for entry in self.headstamps:
            stored = str(entry.get("name") or "").strip()
            if stored.casefold() == wanted:
                return int(entry.get("slot", 0))
        return None

    # ----- parent classifications --------------------------------------------

    def model_has_parents(self) -> bool:
        """True when the active local model has at least one parent defined.

        Drives whether the "Use Parent Classifications" run option is shown.
        Always False in AI Config mode (those headstamps have no parents).
        """
        mid = self.settings.get_active_model_id()
        if mid is None:
            return False
        return bool(self.parents_repo.list_for_model(mid))

    @property
    def use_parent_classifications(self) -> bool:
        """Per-model runtime preference. False in AI Config mode."""
        mid = self.settings.get_active_model_id()
        if mid is None:
            return False
        return bool(self.settings.get(f"{_USE_PARENT_RUNTIME_KEY}:{mid}", False))

    def set_use_parent_classifications(self, value: bool) -> bool:
        mid = self.settings.get_active_model_id()
        if mid is None:
            return False
        self.settings.set(f"{_USE_PARENT_RUNTIME_KEY}:{mid}", bool(value))
        return True

    def parents_with_slots(self) -> list[dict[str, Any]]:
        """[{id, name, slot}] for the active model's parents (empty in AI mode)."""
        mid = self.settings.get_active_model_id()
        if mid is None:
            return []
        return [
            {"id": p.id, "name": p.name, "slot": int(p.slot)}
            for p in self.parents_repo.list_for_model(mid)
        ]

    def headstamps_with_parents(self) -> list[dict[str, Any]]:
        """[{name, slot, parent_id}] for the active model (parent_id None in AI mode)."""
        mid = self.settings.get_active_model_id()
        if mid is None:
            return [
                {"name": e["name"], "slot": int(e.get("slot", 0)), "parent_id": None}
                for e in self._read_ai_headstamps()
            ]
        return [
            {"name": h.name, "slot": int(h.slot), "parent_id": h.parent_id}
            for h in self.headstamps_repo.list_for_model(mid)
        ]

    def set_parent_slot(self, parent_id: int, slot: int) -> bool:
        """Assign a parent classification to a physical slot. Local models only."""
        mid = self.settings.get_active_model_id()
        if mid is None:
            return False
        self.parents_repo.update_slot(int(parent_id), int(slot))
        return True

    def clear_slot_assignments(self) -> None:
        """Clear all saved routing while preserving the headstamp definitions."""
        mid = self.settings.get_active_model_id()
        with self.db.transaction() as conn:
            if mid is None:
                entries = self._read_ai_headstamps()
                for entry in entries:
                    entry["slot"] = 0
                self._write_ai_headstamps(entries)
            else:
                conn.execute(
                    "UPDATE headstamps SET slot = 0 WHERE model_id = ?", (mid,)
                )
                conn.execute(
                    "UPDATE headstamp_parents SET slot = 0 WHERE model_id = ?",
                    (mid,),
                )
            self.settings.set(self._package_slots_key(), {})

    def parent_for_headstamp(self, name: str) -> str | None:
        """The parent classification name for a child headstamp, or None.

        Returns None in AI Config mode, for orphan (parentless) headstamps, or
        for unknown labels. Independent of the runtime toggle — callers decide
        whether to surface it.
        """
        mid = self.settings.get_active_model_id()
        if mid is None:
            return None
        hs = next(
            (h for h in self.headstamps_repo.list_for_model(mid) if h.name == name),
            None,
        )
        if hs is None or hs.parent_id is None:
            return None
        parent = self.parents_repo.get(hs.parent_id)
        return parent.name if parent else None

    # ----- run options (app-level) -------------------------------------------

    @property
    def run_confidence_floor(self) -> int:
        """Minimum confidence (%) a prediction must reach to leave the catch-all.

        Predictions below this route to slot 0. 0 disables the floor.
        """
        try:
            return int(self.settings.get(_RUN_CONFIDENCE_FLOOR_KEY, DEFAULT_CONFIDENCE_FLOOR))
        except (TypeError, ValueError):
            return DEFAULT_CONFIDENCE_FLOOR

    def set_run_confidence_floor(self, value: int) -> None:
        self.settings.set(_RUN_CONFIDENCE_FLOOR_KEY, max(0, min(100, int(value))))

    @property
    def run_store_images(self) -> str:
        """One of STORE_IMAGES_MODES; controls run-image capture."""
        value = self.settings.get(_RUN_STORE_IMAGES_KEY, "none")
        return value if value in STORE_IMAGES_MODES else "none"

    def set_run_store_images(self, mode: str) -> None:
        if mode in STORE_IMAGES_MODES:
            self.settings.set(_RUN_STORE_IMAGES_KEY, mode)

    # ----- package mode (batch sorting) --------------------------------------

    @property
    def run_package_mode(self) -> bool:
        return bool(self.settings.get(_RUN_PACKAGE_MODE_KEY, False))

    def set_run_package_mode(self, value: bool) -> None:
        self.settings.set(_RUN_PACKAGE_MODE_KEY, bool(value))

    @property
    def run_package_size(self) -> int:
        try:
            value = int(self.settings.get(_RUN_PACKAGE_SIZE_KEY, DEFAULT_PACKAGE_SIZE))
        except (TypeError, ValueError):
            value = DEFAULT_PACKAGE_SIZE
        return value if value > 0 else DEFAULT_PACKAGE_SIZE

    def set_run_package_size(self, value: int) -> None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.settings.set(_RUN_PACKAGE_SIZE_KEY, max(1, value))

    def _package_slots_key(self) -> str:
        """Settings key for the active context's package slot assignments.

        Package assignments are model-scoped (each model has its own bins),
        with a single shared bucket for AI Config mode where there is no model.
        """
        mid = self.settings.get_active_model_id()
        return f"{_PACKAGE_SLOTS_KEY}:{mid if mid is not None else 'ai'}"

    def package_slot_map(self) -> dict[int, list[str]]:
        """slot -> [headstamp names] for the active context's package config."""
        raw = self.settings.get(self._package_slots_key()) or {}
        out: dict[int, list[str]] = {}
        if isinstance(raw, dict):
            for k, names in raw.items():
                try:
                    slot = int(k)
                except (TypeError, ValueError):
                    continue
                out[slot] = [str(n) for n in (names or []) if n]
        return out

    def headstamps_in_package_slot(self, slot: int) -> list[str]:
        return list(self.package_slot_map().get(int(slot), []))

    def slots_for_headstamp_package(self, name: str) -> list[int]:
        """Every (non-catch-all) slot the headstamp is assigned to in package mode."""
        wanted = str(name or "").strip().casefold()
        return sorted(
            s for s, names in self.package_slot_map().items()
            if s > 0 and any(
                str(saved).strip().casefold() == wanted for saved in names
            )
        )

    def set_package_slot_headstamp(self, slot: int, name: str, enabled: bool) -> None:
        """Add/remove a headstamp from a package slot's assignment list.

        Unlike the single-slot routing this is many-to-many: a headstamp may be
        ticked into several slots so the run can fill them in batches.
        """
        if int(slot) <= 0 or not name:
            return
        raw = self.settings.get(self._package_slots_key()) or {}
        if not isinstance(raw, dict):
            raw = {}
        key = str(int(slot))
        names = [str(n) for n in (raw.get(key) or []) if n]
        if enabled:
            if name not in names:
                names.append(name)
        else:
            names = [n for n in names if n != name]
        raw[key] = names
        self.settings.set(self._package_slots_key(), raw)

    # ----- auto-select / sort-while-training toggles -------------------------

    @property
    def run_auto_select_trays(self) -> bool:
        return bool(self.settings.get(_RUN_AUTO_SELECT_KEY, False))

    def set_run_auto_select_trays(self, value: bool) -> None:
        self.settings.set(_RUN_AUTO_SELECT_KEY, bool(value))

    @property
    def sort_while_training(self) -> bool:
        return bool(self.settings.get(_SORT_WHILE_TRAINING_KEY, False))

    def set_sort_while_training(self, value: bool) -> None:
        self.settings.set(_SORT_WHILE_TRAINING_KEY, bool(value))

    @property
    def software_profile(self) -> str:
        value = str(self.settings.get(_SOFTWARE_PROFILE_KEY, "full") or "full")
        return value if value in SOFTWARE_PROFILES else "full"

    def set_software_profile(self, value: str) -> None:
        if value not in SOFTWARE_PROFILES:
            raise ValueError("Software profile must be classification_only or full.")
        self.settings.set(_SOFTWARE_PROFILE_KEY, value)

    @property
    def training_tools_visible(self) -> bool:
        """Training exists only for a local model in the Full profile."""
        return (
            self.software_profile == "full"
            and self.settings.get_active_model_id() is not None
        )

    # ----- empty-slot discovery (auto-select trays) --------------------------

    def first_empty_slot(self, *, package: bool | None = None) -> int | None:
        """The lowest slot number (>0) with no headstamp/parent assigned.

        Honours `serial.slot_quantity` for the upper bound. In package mode the
        package assignment map is consulted; otherwise the single-slot routing
        plus any parent-slot assignments are considered "occupied".
        """
        if package is None:
            package = self.run_package_mode
        slot_count = int(self.serial.get("slot_quantity", 8))
        occupied: set[int] = set()
        if package:
            for s, names in self.package_slot_map().items():
                if names:
                    occupied.add(int(s))
        elif self.use_parent_classifications:
            # Parent mode: parent groups and ungrouped headstamps occupy slots.
            for p in self.parents_with_slots():
                if int(p["slot"]) > 0:
                    occupied.add(int(p["slot"]))
            for h in self.headstamps_with_parents():
                if h["parent_id"] is None and int(h["slot"]) > 0:
                    occupied.add(int(h["slot"]))
        else:
            # Child mode: only per-headstamp slots matter. Parent-slot
            # assignments belong to the other runtime mode and must not push
            # auto-select past empty child slots.
            for entry in self.headstamps:
                slot = int(entry.get("slot", 0))
                if slot > 0:
                    occupied.add(slot)
        for slot in range(1, max(1, slot_count)):
            if slot not in occupied:
                return slot
        return None

    def assign_headstamp_to_empty_slot(self, name: str) -> int | None:
        """Route an unassigned headstamp to the first empty slot. Returns the
        slot it landed in, or None when there is no free slot.

        Respects existing assignments and only ever places one headstamp into
        an empty slot.
        """
        if not name:
            return None
        package = self.run_package_mode
        if package:
            if self.slots_for_headstamp_package(name):
                return None  # already assigned somewhere
        elif self.slot_for_headstamp(name):
            return None
        slot = self.first_empty_slot(package=package)
        if slot is None:
            return None
        if package:
            self.set_package_slot_headstamp(slot, name, True)
        else:
            self.set_headstamp_slot(name, slot)
        return slot
