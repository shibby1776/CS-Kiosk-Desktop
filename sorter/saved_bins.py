"""Portable, caliber-scoped Saved Bins profiles.

Files are read only when Maintenance opens the Saved Bins dialog. Profiles use
classification names instead of database ids so they can move between models
and installations. Model synchronization never rewrites a profile; unavailable
entries remain preserved until a user explicitly edits and saves the layout.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .repository import (
    CartridgeRepo,
    HeadstampParentRepo,
    HeadstampRepo,
    ModelRepo,
    SettingsRepo,
)


FILE_TYPE = "shibbyprints.saved_bins"
SCHEMA_VERSION = 1
FILE_SUFFIX = ".bins.json"
MAX_FILE_BYTES = 1_048_576
ROUTING_MODES = ("headstamp", "parent", "package")

_PACKAGE_SLOTS_PREFIX = "package_slots"
_RUN_PACKAGE_MODE_KEY = "run_package_mode"
_USE_PARENT_RUNTIME_PREFIX = "use_parent_runtime"


def _normalise_name(value: object) -> str:
    return str(value or "").strip().casefold()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_filename(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("._-")
    return (stem or "Saved-Bins") + FILE_SUFFIX


def _is_layout_filename(path: Path) -> bool:
    name = Path(path).name
    return len(name) > len(FILE_SUFFIX) and name.endswith(FILE_SUFFIX)


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.digest()


def copy_layout_file(
    source: Path,
    destination: Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Validate and atomically copy one portable layout without moving it."""
    source = Path(source)
    destination = Path(destination)
    if not _is_layout_filename(source) or not _is_layout_filename(destination):
        raise ValueError(f"Saved Bins filenames must end in {FILE_SUFFIX}.")
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination are the same Saved Bins file.")

    # Validate the complete schema and size before creating anything at the
    # destination. This also rejects legacy filename suffixes.
    SavedBinsStore(source.parent).read(source)
    if destination.exists() and not overwrite:
        raise FileExistsError(destination.name)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_name(f"{destination.name}.partial")
    source_hash = hashlib.sha256()
    copied_bytes = 0
    try:
        with source.open("rb") as read_handle, temporary.open("wb") as write_handle:
            while True:
                chunk = read_handle.read(65_536)
                if not chunk:
                    break
                source_hash.update(chunk)
                copied_bytes += len(chunk)
                write_handle.write(chunk)
            write_handle.flush()
            os.fsync(write_handle.fileno())

        if copied_bytes != source.stat().st_size:
            raise OSError("Saved Bins copy size verification failed.")
        copied_hash = _file_digest(temporary)
        if copied_hash != source_hash.digest():
            raise OSError("Saved Bins copy checksum verification failed.")

        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


@dataclass(frozen=True)
class SavedBinAssignment:
    name: str
    bins: tuple[int, ...]
    kind: str = "headstamp"

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, _normalise_name(self.name)

    @classmethod
    def from_dict(cls, raw: object) -> "SavedBinAssignment":
        if not isinstance(raw, dict):
            raise ValueError("Every assignment must be a JSON object.")
        kind = str(raw.get("kind") or "headstamp").strip().lower()
        if kind not in ("headstamp", "parent"):
            raise ValueError(f"Unsupported classification kind: {kind!r}")
        name = str(raw.get("headstamp") or raw.get("name") or "").strip()
        if not name:
            raise ValueError("Every assignment requires a classification name.")
        if "bins" in raw:
            values = raw.get("bins")
            if not isinstance(values, list):
                raise ValueError(f"Bins for {name!r} must be a list.")
        else:
            values = [raw.get("bin")]
        bins: list[int] = []
        for value in values:
            if isinstance(value, bool):
                raise ValueError(f"Invalid bin for {name!r}.")
            try:
                bin_number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid bin for {name!r}.") from exc
            if bin_number < 1 or bin_number > 255:
                raise ValueError(f"Bin for {name!r} must be between 1 and 255.")
            if bin_number not in bins:
                bins.append(bin_number)
        if not bins:
            raise ValueError(f"Assignment for {name!r} has no bins.")
        return cls(name=name, bins=tuple(sorted(bins)), kind=kind)

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "headstamp" if self.kind == "headstamp" else "name": self.name,
        }
        if self.kind != "headstamp":
            out["kind"] = self.kind
        if len(self.bins) == 1:
            out["bin"] = self.bins[0]
        else:
            out["bins"] = list(self.bins)
        return out


@dataclass
class SavedBinsProfile:
    layout_name: str
    caliber: str
    routing_mode: str
    assignments: list[SavedBinAssignment]
    model_hint: str = ""
    created_utc: str = field(default_factory=_utc_now)
    updated_utc: str = field(default_factory=_utc_now)
    path: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_dict(
        cls, raw: object, *, path: Path | None = None
    ) -> "SavedBinsProfile":
        if not isinstance(raw, dict):
            raise ValueError("Saved Bins file must contain a JSON object.")
        if raw.get("file_type") != FILE_TYPE:
            raise ValueError("File is not a ShibbyPrints Saved Bins file.")
        if int(raw.get("schema_version", 0) or 0) != SCHEMA_VERSION:
            raise ValueError("Unsupported Saved Bins schema version.")
        layout_name = str(raw.get("layout_name") or "").strip()
        caliber = str(raw.get("caliber") or "").strip()
        routing_mode = str(raw.get("routing_mode") or "headstamp").strip().lower()
        if not layout_name:
            raise ValueError("Saved Bins file has no layout name.")
        if not caliber:
            raise ValueError("Saved Bins file has no caliber.")
        if routing_mode not in ROUTING_MODES:
            raise ValueError(f"Unsupported routing mode: {routing_mode!r}")
        raw_assignments = raw.get("assignments")
        if not isinstance(raw_assignments, list):
            raise ValueError("Saved Bins assignments must be a list.")
        assignments = [
            SavedBinAssignment.from_dict(item) for item in raw_assignments
        ]
        keys = [item.key for item in assignments]
        if len(keys) != len(set(keys)):
            raise ValueError("Saved Bins file contains duplicate classifications.")
        return cls(
            layout_name=layout_name,
            caliber=caliber,
            routing_mode=routing_mode,
            assignments=assignments,
            model_hint=str(raw.get("model_hint") or "").strip(),
            created_utc=str(raw.get("created_utc") or _utc_now()),
            updated_utc=str(raw.get("updated_utc") or _utc_now()),
            path=path,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "file_type": FILE_TYPE,
            "schema_version": SCHEMA_VERSION,
            "layout_name": self.layout_name,
            "caliber": self.caliber,
            "routing_mode": self.routing_mode,
            "match_policy": "case_insensitive_exact",
            "merge_policy": "preserve_unavailable",
            "model_hint": self.model_hint,
            "created_utc": self.created_utc,
            "updated_utc": self.updated_utc,
            "assignments": [
                item.to_dict()
                for item in sorted(
                    self.assignments,
                    key=lambda item: (min(item.bins), item.name.casefold()),
                )
            ],
        }


class SavedBinsStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def path_for_name(self, layout_name: str) -> Path:
        """Return the canonical local filename for a layout name."""
        return self.directory / _safe_filename(layout_name)

    def ensure_directory(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory

    def read(self, path: Path) -> SavedBinsProfile:
        path = Path(path)
        if not _is_layout_filename(path):
            raise ValueError(f"Saved Bins filenames must end in {FILE_SUFFIX}.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"{path.name} exceeds the Saved Bins size limit.")
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return SavedBinsProfile.from_dict(raw, path=path)

    def scan(self) -> tuple[list[SavedBinsProfile], list[str]]:
        self.ensure_directory()
        profiles: list[SavedBinsProfile] = []
        errors: list[str] = []
        for path in sorted(
            self.directory.glob(f"*{FILE_SUFFIX}"),
            key=lambda item: item.name.casefold(),
        ):
            if not _is_layout_filename(path):
                continue
            try:
                profiles.append(self.read(path))
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        return profiles, errors

    def write(
        self,
        profile: SavedBinsProfile,
        *,
        path: Path | None = None,
        overwrite: bool = False,
    ) -> Path:
        self.ensure_directory()
        destination = (
            Path(path)
            if path is not None
            else self.path_for_name(profile.layout_name)
        )
        if destination.parent.resolve() != self.directory.resolve():
            raise ValueError("Saved Bins files must remain in the Saved Bins folder.")
        if not _is_layout_filename(destination):
            raise ValueError(f"Saved Bins filenames must end in {FILE_SUFFIX}.")
        if destination.exists() and not overwrite:
            raise FileExistsError(destination.name)
        profile.updated_utc = _utc_now()
        payload = json.dumps(
            profile.to_dict(), indent=2, ensure_ascii=False
        ) + "\n"
        temporary = destination.with_name(f"{destination.name}.partial")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        profile.path = destination
        return destination


@dataclass(frozen=True)
class SavedBinsTarget:
    """A local model or the configured remote API runtime."""

    kind: str
    caliber: str
    name: str
    model_id: int | None = None

    @classmethod
    def remote(cls, caliber: str, model_name: str) -> "SavedBinsTarget":
        return cls(kind="remote", caliber=caliber, name=model_name)

    @classmethod
    def local(
        cls, caliber: str, model_name: str, model_id: int
    ) -> "SavedBinsTarget":
        return cls(
            kind="local",
            caliber=caliber,
            name=model_name,
            model_id=int(model_id),
        )

    @property
    def is_remote(self) -> bool:
        return self.kind == "remote"


class SavedBinsService:
    """Build, edit, analyse and apply Saved Bins profiles."""

    def __init__(self, db: Database, config: Config | None = None) -> None:
        self.db = db
        self.config = config
        self.settings = SettingsRepo(db)
        self.models = ModelRepo(db)
        self.cartridges = CartridgeRepo(db)
        self.headstamps = HeadstampRepo(db)
        self.parents = HeadstampParentRepo(db)

    def local_target(self, model_id: int) -> SavedBinsTarget:
        model = self.models.get(int(model_id))
        if model is None or model.id is None:
            raise ValueError("Selected model no longer exists.")
        cartridge = self.cartridges.get(model.cartridge_id)
        if cartridge is None:
            raise ValueError("Selected model has no caliber.")
        return SavedBinsTarget.local(cartridge.name, model.name, model.id)

    def _target(self, target: SavedBinsTarget | int) -> SavedBinsTarget:
        if isinstance(target, SavedBinsTarget):
            if target.kind not in {"local", "remote"}:
                raise ValueError("Unsupported Saved Bins target.")
            if target.is_remote and self.config is None:
                raise ValueError("Remote API Saved Bins requires application config.")
            if not target.is_remote and target.model_id is None:
                raise ValueError("Local Saved Bins target has no model id.")
            return target
        return self.local_target(int(target))

    def model_identity(
        self, target: SavedBinsTarget | int
    ) -> tuple[str, str]:
        resolved = self._target(target)
        return resolved.caliber, resolved.name

    @staticmethod
    def _package_key(target: SavedBinsTarget) -> str:
        suffix = "ai" if target.is_remote else str(int(target.model_id))
        return f"{_PACKAGE_SLOTS_PREFIX}:{suffix}"

    @staticmethod
    def _parent_key(target: SavedBinsTarget) -> str:
        if target.model_id is None:
            raise ValueError("Remote API models do not support parent routing.")
        return f"{_USE_PARENT_RUNTIME_PREFIX}:{int(target.model_id)}"

    def _package_map(self, target: SavedBinsTarget) -> dict[int, list[str]]:
        raw = self.settings.get(self._package_key(target), {}) or {}
        out: dict[int, list[str]] = {}
        if not isinstance(raw, dict):
            return out
        for raw_bin, raw_names in raw.items():
            try:
                bin_number = int(raw_bin)
            except (TypeError, ValueError):
                continue
            out[bin_number] = [
                str(name) for name in (raw_names or []) if str(name).strip()
            ]
        return out

    def routing_mode_for_target(
        self, target: SavedBinsTarget | int
    ) -> str:
        resolved = self._target(target)
        if bool(self.settings.get(_RUN_PACKAGE_MODE_KEY, False)):
            return "package"
        if (
            not resolved.is_remote
            and bool(self.settings.get(self._parent_key(resolved), False))
        ):
            return "parent"
        return "headstamp"

    def _known_for_mode(
        self, target: SavedBinsTarget, routing_mode: str
    ) -> dict[tuple[str, str], tuple[str, int | None]]:
        if target.is_remote:
            if routing_mode == "parent":
                raise ValueError(
                    "Remote API models do not support parent-classification layouts."
                )
            assert self.config is not None
            return {
                ("headstamp", _normalise_name(item["name"])): (item["name"], None)
                for item in self.config.remote_headstamps()
                if str(item.get("name") or "").strip()
            }

        model_id = int(target.model_id)
        headstamps = self.headstamps.list_for_model(model_id)
        if routing_mode == "parent":
            out = {
                ("parent", _normalise_name(parent.name)): (parent.name, parent.id)
                for parent in self.parents.list_for_model(model_id)
            }
            out.update({
                ("headstamp", _normalise_name(item.name)): (item.name, item.id)
                for item in headstamps
                if item.parent_id is None
            })
            return out
        return {
            ("headstamp", _normalise_name(item.name)): (item.name, item.id)
            for item in headstamps
        }

    def _current_assignments(
        self, target: SavedBinsTarget, routing_mode: str
    ) -> tuple[dict[tuple[str, str], SavedBinAssignment], set[tuple[str, str]]]:
        known = self._known_for_mode(target, routing_mode)
        assigned: dict[tuple[str, str], SavedBinAssignment] = {}
        if routing_mode == "package":
            bins_by_name: dict[str, set[int]] = {}
            display_by_name: dict[str, str] = {}
            for bin_number, names in self._package_map(target).items():
                if bin_number <= 0:
                    continue
                for name in names:
                    normalised = _normalise_name(name)
                    key = ("headstamp", normalised)
                    if key not in known:
                        continue
                    bins_by_name.setdefault(normalised, set()).add(bin_number)
                    display_by_name[normalised] = known[key][0]
            for normalised, bins in bins_by_name.items():
                item = SavedBinAssignment(
                    name=display_by_name[normalised],
                    bins=tuple(sorted(bins)),
                )
                assigned[item.key] = item
            return assigned, set(known)

        if target.is_remote:
            assert self.config is not None
            for entry in self.config.remote_headstamps():
                if int(entry.get("slot", 0)) > 0:
                    item = SavedBinAssignment(
                        name=str(entry["name"]),
                        bins=(int(entry["slot"]),),
                    )
                    assigned[item.key] = item
            return assigned, set(known)

        model_id = int(target.model_id)
        if routing_mode == "parent":
            for parent in self.parents.list_for_model(model_id):
                if int(parent.slot) > 0:
                    item = SavedBinAssignment(
                        name=parent.name,
                        bins=(int(parent.slot),),
                        kind="parent",
                    )
                    assigned[item.key] = item
            for headstamp in self.headstamps.list_for_model(model_id):
                if headstamp.parent_id is None and int(headstamp.slot) > 0:
                    item = SavedBinAssignment(
                        name=headstamp.name,
                        bins=(int(headstamp.slot),),
                    )
                    assigned[item.key] = item
            return assigned, set(known)

        for headstamp in self.headstamps.list_for_model(model_id):
            if int(headstamp.slot) > 0:
                item = SavedBinAssignment(
                    name=headstamp.name,
                    bins=(int(headstamp.slot),),
                )
                assigned[item.key] = item
        return assigned, set(known)

    def snapshot(
        self,
        target: SavedBinsTarget | int,
        layout_name: str,
        *,
        routing_mode: str | None = None,
    ) -> SavedBinsProfile:
        resolved = self._target(target)
        caliber, model_name = self.model_identity(resolved)
        mode = routing_mode or self.routing_mode_for_target(resolved)
        assigned, _known = self._current_assignments(resolved, mode)
        return SavedBinsProfile(
            layout_name=layout_name.strip(),
            caliber=caliber,
            routing_mode=mode,
            assignments=list(assigned.values()),
            model_hint=model_name,
        )

    def edit_rows(
        self, profile: SavedBinsProfile, target: SavedBinsTarget | int
    ) -> list[dict[str, object]]:
        """Return the union of model classifications and preserved file entries."""
        resolved = self._target(target)
        self._validate_caliber(profile, resolved)
        known = self._known_for_mode(resolved, profile.routing_mode)
        saved = {item.key: item for item in profile.assignments}
        rows: list[dict[str, object]] = []
        for key, (display, _record_id) in sorted(
            known.items(), key=lambda item: item[1][0].casefold()
        ):
            item = saved.get(key)
            rows.append({
                "key": key,
                "name": display,
                "kind": key[0],
                "bins": item.bins if item is not None else (),
                "available": True,
            })
        for item in sorted(profile.assignments, key=lambda value: value.name.casefold()):
            if item.key in known:
                continue
            rows.append({
                "key": item.key,
                "name": item.name,
                "kind": item.kind,
                "bins": item.bins,
                "available": False,
            })
        return rows

    def replace_target_assignments(
        self,
        profile: SavedBinsProfile,
        target: SavedBinsTarget | int,
        assignments: list[SavedBinAssignment],
        slot_count: int,
    ) -> SavedBinsProfile:
        """Save edited target rows while preserving unavailable file entries."""
        resolved = self._target(target)
        self._validate_caliber(profile, resolved)
        known = self._known_for_mode(resolved, profile.routing_mode)
        maximum_bin = max(0, int(slot_count) - 1)
        replacement: dict[tuple[str, str], SavedBinAssignment] = {}
        for item in assignments:
            if item.key not in known:
                raise ValueError(f"{item.name!r} is not available in the target model.")
            if any(bin_number > maximum_bin for bin_number in item.bins):
                raise ValueError(
                    f"{item.name!r} uses a bin unavailable on this sorter."
                )
            display, _record_id = known[item.key]
            replacement[item.key] = SavedBinAssignment(
                name=display, bins=item.bins, kind=item.kind
            )
        preserved = [
            item for item in profile.assignments if item.key not in known
        ]
        profile.assignments = preserved + list(replacement.values())
        profile.model_hint = resolved.name
        return profile

    def _validate_caliber(
        self, profile: SavedBinsProfile, target: SavedBinsTarget
    ) -> None:
        if _normalise_name(profile.caliber) != _normalise_name(target.caliber):
            raise ValueError(
                f"Saved caliber {profile.caliber!r} does not match "
                f"{target.caliber!r}."
            )

    def analyse(
        self,
        profile: SavedBinsProfile,
        target: SavedBinsTarget | int,
        slot_count: int,
    ) -> dict[str, object]:
        resolved = self._target(target)
        self._validate_caliber(profile, resolved)
        known = self._known_for_mode(resolved, profile.routing_mode)
        profile_keys = {item.key for item in profile.assignments}
        matched: list[SavedBinAssignment] = []
        unavailable: list[SavedBinAssignment] = []
        unsupported: list[SavedBinAssignment] = []
        maximum_bin = max(0, int(slot_count) - 1)
        for item in profile.assignments:
            if item.key not in known:
                unavailable.append(item)
            elif any(bin_number > maximum_bin for bin_number in item.bins):
                unsupported.append(item)
            else:
                matched.append(item)
        new_names = [
            display
            for key, (display, _record_id) in known.items()
            if key not in profile_keys
        ]
        return {
            "matched": matched,
            "unavailable": unavailable,
            "unsupported": unsupported,
            "new_names": sorted(new_names, key=str.casefold),
            "matched_count": len(matched),
            "unavailable_count": len(unavailable),
            "unsupported_count": len(unsupported),
            "new_count": len(new_names),
        }

    def apply(
        self,
        profile: SavedBinsProfile,
        target: SavedBinsTarget | int,
        slot_count: int,
    ) -> dict[str, object]:
        resolved = self._target(target)
        analysis = self.analyse(profile, resolved, slot_count)
        matched = list(analysis["matched"])
        if not matched:
            raise ValueError("No saved classifications match the selected model.")
        known = self._known_for_mode(resolved, profile.routing_mode)
        package_map: dict[str, list[str]] = {}

        with self.db.transaction() as conn:
            if resolved.is_remote:
                assert self.config is not None
                self.config.set_remote_headstamp_slots({})
            else:
                model_id = int(resolved.model_id)
                conn.execute(
                    "UPDATE headstamps SET slot = 0 WHERE model_id = ?",
                    (model_id,),
                )
                conn.execute(
                    "UPDATE headstamp_parents SET slot = 0 WHERE model_id = ?",
                    (model_id,),
                )
            self.settings.set(self._package_key(resolved), {})

            remote_slots: dict[str, int] = {}
            for item in matched:
                display, record_id = known[item.key]
                if profile.routing_mode == "package":
                    for bin_number in item.bins:
                        package_map.setdefault(str(bin_number), []).append(display)
                elif item.kind == "parent":
                    conn.execute(
                        "UPDATE headstamp_parents SET slot = ? WHERE id = ?",
                        (int(item.bins[0]), int(record_id)),
                    )
                elif resolved.is_remote:
                    remote_slots[display] = int(item.bins[0])
                else:
                    conn.execute(
                        "UPDATE headstamps SET slot = ? WHERE id = ?",
                        (int(item.bins[0]), int(record_id)),
                    )

            if resolved.is_remote:
                assert self.config is not None
                self.config.set_remote_headstamp_slots(remote_slots)
            if profile.routing_mode == "package":
                self.settings.set(self._package_key(resolved), package_map)
            self.settings.set(
                _RUN_PACKAGE_MODE_KEY, profile.routing_mode == "package"
            )
            if resolved.is_remote:
                self.settings.clear_active_model()
            else:
                self.settings.set(
                    self._parent_key(resolved), profile.routing_mode == "parent"
                )
                self.settings.set_active_model_id(int(resolved.model_id))

            actual, _known = self._current_assignments(
                resolved, profile.routing_mode
            )
            expected = {item.key: item.bins for item in matched}
            if any(
                actual.get(key) is None or actual[key].bins != bins
                for key, bins in expected.items()
            ):
                raise RuntimeError(
                    "Saved Bins verification failed; no routing changes were kept."
                )
        return analysis
