"""Maintenance-only Saved Bins management dialog."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from .. import api_client, paths
from ..repository import CartridgeRepo, ModelRepo
from ..saved_bins import (
    SavedBinsProfile,
    SavedBinsService,
    SavedBinsStore,
    SavedBinsTarget,
    FILE_SUFFIX,
    copy_layout_file,
)
from .dialog_saved_bins_editor import SavedBinsEditor


_MODE_LABELS = {
    "headstamp": "Headstamps",
    "parent": "Parent classifications",
    "package": "Package mode",
}


class SavedBinsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, *, app, config) -> None:
        super().__init__(parent)
        self.app = app
        self.config = config
        self.db = app.db
        self.store = SavedBinsStore(paths.saved_bins_dir())
        self.service = SavedBinsService(self.db, config)
        self.cartridges = CartridgeRepo(self.db)
        self.models_repo = ModelRepo(self.db)
        self._targets_by_display: dict[str, SavedBinsTarget] = {}
        self._profiles_by_iid: dict[str, SavedBinsProfile] = {}
        self._remote_sync_inflight = False

        self.title("Saved Bins")
        width, height = self._dialog_size(
            self.winfo_screenwidth(), self.winfo_screenheight()
        )
        self.geometry(f"{width}x{height}")
        self.minsize(min(760, width), min(520, height))
        self.transient(parent.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        location = ttk.LabelFrame(outer, text="Portable Saved Bins folder")
        location.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        location.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar(value=str(self.store.ensure_directory()))
        ttk.Entry(
            location, textvariable=self.folder_var, state="readonly"
        ).grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=8)
        ttk.Button(
            location, text="Open Folder", command=self._open_folder
        ).grid(row=0, column=1, padx=(4, 8), pady=8)

        selectors = ttk.LabelFrame(outer, text="Target model")
        selectors.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        selectors.columnconfigure(1, weight=1)
        selectors.columnconfigure(3, weight=1)
        ttk.Label(selectors, text="Caliber").grid(
            row=0, column=0, padx=(8, 4), pady=8, sticky="w"
        )
        self.caliber_var = tk.StringVar()
        self.caliber_combo = ttk.Combobox(
            selectors, state="readonly", textvariable=self.caliber_var
        )
        self.caliber_combo.grid(
            row=0, column=1, padx=(4, 12), pady=8, sticky="ew"
        )
        self.caliber_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_caliber_changed()
        )
        ttk.Label(selectors, text="Model").grid(
            row=0, column=2, padx=(4, 4), pady=8, sticky="w"
        )
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(
            selectors, state="readonly", textvariable=self.model_var
        )
        self.model_combo.grid(
            row=0, column=3, padx=(4, 8), pady=8, sticky="ew"
        )
        self.model_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_selected_model()
        )

        body = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        body.grid(row=2, column=0, sticky="nsew")

        list_frame = ttk.LabelFrame(body, text="Saved layouts")
        detail_frame = ttk.LabelFrame(body, text="Compatibility preview")
        body.add(list_frame, weight=55)
        body.add(detail_frame, weight=45)

        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        columns = ("layout", "mode", "entries", "file")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("layout", text="Layout")
        self.tree.heading("mode", text="Routing")
        self.tree.heading("entries", text="Entries")
        self.tree.heading("file", text="File")
        self.tree.column("layout", width=180, stretch=True)
        self.tree.column("mode", width=110, stretch=False)
        self.tree.column("entries", width=60, anchor=tk.CENTER, stretch=False)
        self.tree.column("file", width=190, stretch=True)
        scroll = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind(
            "<<TreeviewSelect>>", lambda _event: self._show_selected_details()
        )

        detail_frame.rowconfigure(0, weight=1)
        detail_frame.columnconfigure(0, weight=1)
        self.details = tk.Text(
            detail_frame, wrap=tk.WORD, height=18, state=tk.DISABLED
        )
        detail_scroll = ttk.Scrollbar(
            detail_frame, orient=tk.VERTICAL, command=self.details.yview
        )
        self.details.configure(yscrollcommand=detail_scroll.set)
        self.details.grid(row=0, column=0, sticky="nsew")
        detail_scroll.grid(row=0, column=1, sticky="ns")

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        for column in range(4):
            actions.columnconfigure(column, weight=1, uniform="saved-bin-actions")
        action_specs = (
            ("Apply Layout", self._load_selected),
            ("Save Current as New", self._save_current),
            ("Edit Layout…", self._edit_selected),
            ("Refresh List", self._refresh_profiles),
            ("Import from USB…", self._import_from_usb),
            ("Export to USB…", self._export_to_usb),
            ("Delete Layout", self._delete_selected),
            ("Close", self.destroy),
        )
        for index, (label, command) in enumerate(action_specs):
            row, column = divmod(index, 4)
            ttk.Button(actions, text=label, command=command).grid(
                row=row,
                column=column,
                sticky="ew",
                padx=4,
                pady=4,
            )

        self.status_var = tk.StringVar(value="")
        ttk.Label(
            outer, textvariable=self.status_var, style="Muted.TLabel"
        ).grid(row=4, column=0, sticky="w", pady=(8, 0))

        self._load_model_selectors()
        self._refresh_profiles()
        self._refresh_selected_model()
        self.wait_visibility()
        self.focus_set()

    @staticmethod
    def _dialog_size(screen_width: int, screen_height: int) -> tuple[int, int]:
        """Fit the manager on smaller scaled displays without hiding actions."""
        width = max(560, min(900, int(screen_width) - 80))
        height = max(440, min(620, int(screen_height) - 100))
        return width, height

    def _load_model_selectors(self) -> None:
        cartridges = self.cartridges.list()
        names = [item.name for item in cartridges]
        profiles, _errors = self.store.scan()
        for profile in profiles:
            if profile.caliber not in names:
                names.append(profile.caliber)
        configured_model = str(self.config.api.get("model", "") or "").strip()
        if configured_model and not names:
            names.append(configured_model)
        names.sort(key=str.casefold)
        self.caliber_combo.configure(values=names)
        if not names:
            self.status_var.set("No local or remote models are configured.")
            return
        active_id = self.config.settings.get_active_model_id()
        active_model = (
            self.models_repo.get(active_id) if active_id is not None else None
        )
        active_caliber = None
        if active_model is not None:
            cartridge = self.cartridges.get(active_model.cartridge_id)
            active_caliber = cartridge.name if cartridge is not None else None
        preferred_caliber = active_caliber
        if active_id is None and configured_model in names:
            preferred_caliber = configured_model
        self.caliber_var.set(
            preferred_caliber if preferred_caliber in names else names[0]
        )
        self._populate_models(preferred_id=active_id)

    def _populate_models(self, preferred_id: int | None = None) -> None:
        cartridge = self.cartridges.find_by_name(self.caliber_var.get())
        self._targets_by_display.clear()
        displays: list[str] = []
        remote_name = str(self.config.api.get("model", "") or "").strip()
        if remote_name:
            display = f"Remote API — {remote_name}"
            displays.append(display)
            self._targets_by_display[display] = SavedBinsTarget.remote(
                self.caliber_var.get(), remote_name
            )
        if cartridge is not None and cartridge.id is not None:
            for model in self.models_repo.list_by_cartridge(cartridge.id):
                if model.id is None:
                    continue
                display = f"{model.name} (v{model.model_version}, #{model.id})"
                displays.append(display)
                self._targets_by_display[display] = SavedBinsTarget.local(
                    cartridge.name, model.name, model.id
                )
        self.model_combo.configure(values=displays)
        selected = next(
            (
                display
                for display, target in self._targets_by_display.items()
                if target.model_id == preferred_id
                and (preferred_id is not None or target.is_remote)
            ),
            displays[0] if displays else "",
        )
        self.model_var.set(selected)

    def _on_caliber_changed(self) -> None:
        self._populate_models()
        self._refresh_profiles()
        self._refresh_selected_model()

    def _refresh_selected_model(self) -> None:
        """Refresh a remote catalog once, then redraw the compatibility view."""
        target = self._selected_target()
        if target is None or not target.is_remote:
            self._show_selected_details()
            return
        self._with_target_ready(
            target, lambda _ready: self._show_selected_details()
        )

    def _selected_target(self) -> SavedBinsTarget | None:
        return self._targets_by_display.get(self.model_var.get())

    def _selected_profile(self) -> SavedBinsProfile | None:
        selected = self.tree.selection()
        return self._profiles_by_iid.get(selected[0]) if selected else None

    def _refresh_profiles(self, select_path: Path | None = None) -> None:
        profiles, errors = self.store.scan()
        caliber_key = self.caliber_var.get().strip().casefold()
        profiles = [
            item
            for item in profiles
            if item.caliber.strip().casefold() == caliber_key
        ]
        self.tree.delete(*self.tree.get_children())
        self._profiles_by_iid.clear()
        selected_iid = None
        for index, profile in enumerate(profiles):
            iid = f"profile-{index}"
            self._profiles_by_iid[iid] = profile
            self.tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    profile.layout_name,
                    _MODE_LABELS.get(profile.routing_mode, profile.routing_mode),
                    len(profile.assignments),
                    profile.path.name if profile.path is not None else "",
                ),
            )
            if (
                select_path is not None
                and profile.path is not None
                and profile.path.resolve() == select_path.resolve()
            ):
                selected_iid = iid
        if selected_iid is None and profiles:
            selected_iid = "profile-0"
        if selected_iid is not None:
            self.tree.selection_set(selected_iid)
            self.tree.focus(selected_iid)
        error_text = f"; {len(errors)} invalid file(s) ignored" if errors else ""
        self.status_var.set(
            f"{len(profiles)} layout(s) for {self.caliber_var.get()}{error_text}."
        )
        self._show_selected_details()

    def _set_details(self, text: str) -> None:
        self.details.configure(state=tk.NORMAL)
        self.details.delete("1.0", tk.END)
        self.details.insert(tk.END, text)
        self.details.configure(state=tk.DISABLED)

    def _show_selected_details(self) -> None:
        profile = self._selected_profile()
        target = self._selected_target()
        if profile is None:
            self._set_details("Select a saved layout to preview it.")
            return
        lines = [
            profile.layout_name,
            f"Caliber: {profile.caliber}",
            f"Routing: {_MODE_LABELS.get(profile.routing_mode, profile.routing_mode)}",
            f"Last model: {profile.model_hint or '—'}",
            "",
        ]
        if target is not None:
            try:
                analysis = self.service.analyse(
                    profile,
                    target,
                    int(self.config.serial.get("slot_quantity", 8)),
                )
                lines.extend([
                    f"Will apply: {analysis['matched_count']}",
                    f"New in model: {analysis['new_count']}",
                    f"Unavailable in model: {analysis['unavailable_count']}",
                    f"Unsupported bins: {analysis['unsupported_count']}",
                    "",
                ])
            except Exception as exc:
                lines.extend([f"Cannot apply: {exc}", ""])
        grouped: dict[int, list[str]] = {}
        for item in profile.assignments:
            label = item.name if item.kind == "headstamp" else f"{item.name} [parent]"
            for bin_number in item.bins:
                grouped.setdefault(bin_number, []).append(label)
        for bin_number in sorted(grouped):
            names = ", ".join(sorted(grouped[bin_number], key=str.casefold))
            lines.append(f"Bin {bin_number}: {names}")
        self._set_details("\n".join(lines))

    def _require_stopped(self) -> bool:
        controller = self.app.run_controller
        if controller is not None and controller.is_running:
            messagebox.showerror(
                "Run in progress",
                "Stop the sorter before saving, editing, or applying "
                "Saved Bins.",
                parent=self,
            )
            return False
        return True

    def _with_target_ready(self, target: SavedBinsTarget, action) -> None:
        """Synchronize remote names on demand, then continue on the UI thread."""
        if not target.is_remote:
            action(target)
            return
        if self._remote_sync_inflight:
            messagebox.showinfo(
                "Remote sync in progress",
                "The remote model classifications are already being refreshed.",
                parent=self,
            )
            return
        endpoint = str(self.config.api.get("endpoint_url", "") or "").strip()
        model = str(self.config.api.get("model", "") or "").strip()
        api_key = str(self.config.api.get("api_key", "") or "").strip()
        if not endpoint or not model:
            messagebox.showerror(
                "Remote model not configured",
                "Configure the API endpoint and model before using remote Saved Bins.",
                parent=self,
            )
            return
        self._remote_sync_inflight = True
        self.status_var.set(f"Refreshing classifications from {model}…")
        self.app.run_worker(
            lambda: api_client.get_headstamps(endpoint, model, api_key),
            on_done=lambda names: self._remote_sync_done(target, names, action),
            on_error=self._remote_sync_failed,
        )

    def _remote_sync_done(self, target, names, action) -> None:
        self._remote_sync_inflight = False
        try:
            if not self.winfo_exists():
                return
            if self._selected_target() != target:
                self.status_var.set(
                    "Remote classifications refreshed; target selection changed."
                )
                return
            result = self.config.synchronize_remote_headstamps(list(names))
            self.status_var.set(
                f"Remote model returned {result['received']} classification(s)."
            )
            self._show_selected_details()
            action(target)
        except Exception as exc:
            messagebox.showerror("Remote sync failed", str(exc), parent=self)

    def _remote_sync_failed(self, exc: Exception) -> None:
        self._remote_sync_inflight = False
        try:
            if self.winfo_exists():
                self.status_var.set("Remote classification refresh failed.")
                messagebox.showerror("Remote sync failed", str(exc), parent=self)
        except tk.TclError:
            return

    def _save_current(self) -> None:
        if not self._require_stopped():
            return
        target = self._selected_target()
        if target is None:
            messagebox.showerror("No model", "Select a model first.", parent=self)
            return
        self._with_target_ready(target, self._save_current_ready)

    def _save_current_ready(self, target: SavedBinsTarget) -> None:
        name = simpledialog.askstring(
            "Save Current as New",
            "Name the new Saved Bins layout:",
            parent=self,
        )
        if not name or not name.strip():
            return
        try:
            profile = self.service.snapshot(target, name.strip())
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)
            return
        open_editor = not profile.assignments
        if open_editor and not messagebox.askyesno(
            "Create empty layout",
            "The selected model has no assigned bins yet.\n\n"
            "Create the layout and assign its classifications now?",
            parent=self,
        ):
            return
        try:
            destination = self.store.write(profile)
        except FileExistsError as exc:
            messagebox.showerror(
                "Layout already exists",
                f"{exc.args[0]} already exists. Choose a different name, or "
                "select the existing layout and use Edit Layout.",
                parent=self,
            )
            return
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)
            return
        self._refresh_profiles(select_path=destination)
        self.app.set_status(f"Saved bin layout: {profile.layout_name}.")
        if open_editor:
            self._open_editor(profile, target)

    def _edit_selected(self) -> None:
        if not self._require_stopped():
            return
        profile = self._selected_profile()
        target = self._selected_target()
        if profile is None or profile.path is None:
            messagebox.showerror("No layout", "Select a saved layout first.", parent=self)
            return
        if target is None:
            messagebox.showerror("No model", "Select a model first.", parent=self)
            return
        self._with_target_ready(
            target, lambda ready: self._open_editor(profile, ready)
        )

    def _open_editor(
        self, profile: SavedBinsProfile, target: SavedBinsTarget
    ) -> None:
        try:
            SavedBinsEditor(
                self,
                profile=profile,
                target=target,
                service=self.service,
                store=self.store,
                slot_count=int(self.config.serial.get("slot_quantity", 8)),
                on_saved=self._editor_saved,
            )
        except Exception as exc:
            messagebox.showerror("Cannot edit layout", str(exc), parent=self)

    def _editor_saved(self, destination: Path) -> None:
        self._refresh_profiles(select_path=destination)
        self.app.set_status("Saved bin layout assignments.")

    def _choose_from_list(
        self, title: str, prompt: str, options: list[str]
    ) -> int | None:
        """Show a bounded, resizable selector and return the chosen index."""
        result: dict[str, int | None] = {"index": None}
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        screen_width = max(640, dialog.winfo_screenwidth())
        screen_height = max(480, dialog.winfo_screenheight())
        width = min(560, screen_width - 80)
        height = min(420, screen_height - 80)
        dialog.geometry(f"{width}x{height}")
        dialog.minsize(min(420, width), min(300, height))

        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text=prompt, wraplength=max(360, width - 50)).grid(
            row=0, column=0, sticky="ew", pady=(0, 10)
        )
        list_frame = ttk.Frame(frame)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        listbox = tk.Listbox(list_frame, exportselection=False)
        scrollbar = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=listbox.yview
        )
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        for option in options:
            listbox.insert(tk.END, option)
        listbox.selection_set(0)
        listbox.activate(0)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        buttons.columnconfigure((0, 1), weight=1)

        def accept(_event=None) -> None:
            selection = listbox.curselection()
            if selection:
                result["index"] = int(selection[0])
                dialog.destroy()

        ttk.Button(buttons, text="Cancel", command=dialog.destroy).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(buttons, text="Select", command=accept).grid(
            row=0, column=1, sticky="ew", padx=(4, 0)
        )
        listbox.bind("<Double-Button-1>", accept)
        listbox.bind("<Return>", accept)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        listbox.focus_set()
        self.wait_window(dialog)
        return result["index"]

    @staticmethod
    def _removable_usb_roots() -> list[Path]:
        """Return Windows removable-drive roots, checked only on demand."""
        if os.name != "nt":
            return []
        roots: list[Path] = []
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetLogicalDrives.restype = ctypes.c_uint32
            kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
            kernel32.GetDriveTypeW.restype = ctypes.c_uint
            drive_mask = int(kernel32.GetLogicalDrives())
            for index in range(26):
                if not drive_mask & (1 << index):
                    continue
                root = f"{chr(ord('A') + index)}:\\"
                path = Path(root)
                if (
                    int(kernel32.GetDriveTypeW(root)) == 2
                    and path.is_dir()
                ):
                    roots.append(path)
        except (AttributeError, OSError, TypeError, ValueError):
            return []
        return roots

    def _choose_usb_root(self) -> Path | None:
        roots = self._removable_usb_roots()
        if not roots:
            messagebox.showerror(
                "USB not available",
                "No removable USB drive was detected. Insert a USB drive and "
                "try again.",
                parent=self,
            )
            return None
        if len(roots) == 1:
            return roots[0]
        choice = self._choose_from_list(
            "Select USB drive",
            "Choose the removable USB drive whose root folder should be used.",
            [str(root) for root in roots],
        )
        return roots[choice] if choice is not None else None

    def _choose_usb_profile(
        self, profiles: list[SavedBinsProfile]
    ) -> SavedBinsProfile | None:
        if len(profiles) == 1:
            return profiles[0]
        options = [
            f"{profile.layout_name} ({profile.path.name})"
            for profile in profiles
            if profile.path is not None
        ]
        choice = self._choose_from_list(
            "Import from USB",
            "Choose a Saved Bins layout from the USB root.",
            options,
        )
        return profiles[choice] if choice is not None else None

    def _import_from_usb(self) -> None:
        root = self._choose_usb_root()
        if root is None:
            return
        profiles, errors = SavedBinsStore(root).scan()
        if not profiles:
            detail = "No valid .bins.json layouts were found in the USB root."
            if errors:
                detail += "\n\n" + "\n".join(errors[:5])
            messagebox.showerror("Nothing to import", detail, parent=self)
            return
        profile = self._choose_usb_profile(profiles)
        if profile is None or profile.path is None:
            return
        source = profile.path
        destination = self.store.path_for_name(profile.layout_name)
        overwrite = False
        if destination.exists():
            overwrite = messagebox.askyesno(
                "Replace local layout",
                f"{destination.name} already exists locally. Replace it with "
                "the selected USB copy?",
                parent=self,
            )
            if not overwrite:
                return
        try:
            copy_layout_file(source, destination, overwrite=overwrite)
            imported = self.store.read(destination)
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
            return

        # Layouts are filtered by caliber. Select the imported caliber so the
        # verified local copy is visible as soon as import completes.
        if (
            imported.caliber.strip().casefold()
            != self.caliber_var.get().strip().casefold()
        ):
            calibers = list(self.caliber_combo.cget("values"))
            if imported.caliber not in calibers:
                calibers.append(imported.caliber)
                calibers.sort(key=str.casefold)
                self.caliber_combo.configure(values=calibers)
            self.caliber_var.set(imported.caliber)
            self._populate_models()
        self._refresh_profiles(select_path=destination)
        self.app.set_status(f"Imported Saved Bins layout: {destination.name}.")
        messagebox.showinfo(
            "Import complete",
            "The layout was copied into the local Saved Bins folder.",
            parent=self,
        )

    def _export_to_usb(self) -> None:
        profile = self._selected_profile()
        if profile is None or profile.path is None:
            messagebox.showerror(
                "No layout", "Select a saved layout first.", parent=self
            )
            return
        root = self._choose_usb_root()
        if root is None:
            return
        destination = root / profile.path.name
        counter = 1
        while destination.exists():
            stem = profile.path.name[: -len(FILE_SUFFIX)]
            destination = root / f"{stem}-{counter}{FILE_SUFFIX}"
            counter += 1
        try:
            copy_layout_file(profile.path, destination)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        self.app.set_status(f"Exported Saved Bins layout: {destination.name}.")
        messagebox.showinfo(
            "Export complete",
            f"The layout was copied directly to the USB root as:\n"
            f"{destination.name}\n\nThe local Saved Bins copy remains unchanged.",
            parent=self,
        )

    def _load_selected(self) -> None:
        if not self._require_stopped():
            return
        profile = self._selected_profile()
        target = self._selected_target()
        if profile is None:
            messagebox.showerror("No layout", "Select a saved layout first.", parent=self)
            return
        if target is None:
            messagebox.showerror("No model", "Select a model first.", parent=self)
            return
        self._with_target_ready(
            target, lambda ready: self._load_selected_ready(profile, ready)
        )

    def _load_selected_ready(
        self, profile: SavedBinsProfile, target: SavedBinsTarget
    ) -> None:
        try:
            analysis = self.service.analyse(
                profile,
                target,
                int(self.config.serial.get("slot_quantity", 8)),
            )
        except Exception as exc:
            messagebox.showerror("Cannot apply layout", str(exc), parent=self)
            return
        if not analysis["matched_count"]:
            messagebox.showerror(
                "No matching classifications",
                "This layout has no classifications that can be applied to "
                "the selected model.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "Apply Layout",
            f"Apply “{profile.layout_name}” to the selected model?\n\n"
            f"Will apply: {analysis['matched_count']}\n"
            f"New in model: {analysis['new_count']}\n"
            f"Unavailable in model: {analysis['unavailable_count']}\n"
            f"Unsupported bins: {analysis['unsupported_count']}\n\n"
            "Existing bin assignments and counters for the selected model "
            "will be replaced. Unavailable names remain in the Saved Bins file.",
            parent=self,
        ):
            return
        try:
            self.service.apply(
                profile,
                target,
                int(self.config.serial.get("slot_quantity", 8)),
            )
        except Exception as exc:
            messagebox.showerror("Apply failed", str(exc), parent=self)
            return
        self.config.reload_headstamps_for_active_model()
        self.app.refresh_saved_bins_runtime()
        models_tab = getattr(self.app, "models_tab", None)
        if models_tab is not None and hasattr(models_tab, "refresh"):
            models_tab.refresh()
        self.app.set_status(f"Applied Saved Bins layout: {profile.layout_name}.")
        messagebox.showinfo(
            "Layout applied",
            f"Applied {analysis['matched_count']} classification(s). "
            f"{analysis['unavailable_count']} unavailable classification(s) "
            "remain preserved in the file.",
            parent=self,
        )
        self._show_selected_details()

    def _delete_selected(self) -> None:
        profile = self._selected_profile()
        if profile is None or profile.path is None:
            messagebox.showerror("No layout", "Select a saved layout first.", parent=self)
            return
        if not messagebox.askyesno(
            "Delete Saved Bins file",
            f"Delete “{profile.layout_name}”?\n\nThis removes only the portable "
            "Saved Bins file. It does not change any model assignments.",
            parent=self,
        ):
            return
        try:
            if profile.path.parent.resolve() != self.store.directory.resolve():
                raise ValueError("Saved Bins file is outside the Saved Bins folder.")
            profile.path.unlink()
        except Exception as exc:
            messagebox.showerror("Delete failed", str(exc), parent=self)
            return
        self._refresh_profiles()

    def _open_folder(self) -> None:
        folder = self.store.ensure_directory()
        try:
            if os.name == "nt":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror("Open Folder failed", str(exc), parent=self)
