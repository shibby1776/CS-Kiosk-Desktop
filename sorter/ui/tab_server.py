"""Maintenance tab for the optional integrated multi-model API server."""
from __future__ import annotations

import secrets
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from ..api_server_settings import ApiServerSettingsRepo
from ..network_info import primary_local_ipv4_addresses
from ..repository import ApiModelAliasRepo, CartridgeRepo, ModelRepo
from .widgets import NumericField


_HOST_LABELS = {
    "This computer only": "127.0.0.1",
    "Local network": "0.0.0.0",
}


class ServerTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, config, bus, app) -> None:
        super().__init__(parent)
        self.config = config
        self.bus = bus
        self.app = app
        self.db = app.db
        self.aliases = ApiModelAliasRepo(self.db)
        self.models = ModelRepo(self.db)
        self.cartridges = CartridgeRepo(self.db)
        self.settings_repo = ApiServerSettingsRepo(self.db)
        self.runtime = app.get_api_server_runtime()
        self._setting_key_field = False
        self._key_edited = False
        self._build()
        self.bus.subscribe("api_server/status", self._on_status)
        self.refresh()
        pending = getattr(app, "_pending_server_model_id", None)
        app._pending_server_model_id = None
        if pending is not None:
            self.after_idle(lambda mid=int(pending): self.assign_model(mid))

    def _build(self) -> None:
        status = ttk.LabelFrame(self, text="Integrated API Server", padding=10)
        status.pack(fill=tk.X, padx=8, pady=(8, 4))
        self.status_var = tk.StringVar(value="Server stopped.")
        self.address_var = tk.StringVar(value="")
        self.network_addresses_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.status_var, style="Header.TLabel").pack(anchor=tk.W)
        ttk.Label(status, textvariable=self.address_var, style="Muted.TLabel").pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(
            status,
            textvariable=self.network_addresses_var,
            style="Muted.TLabel",
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 0))
        actions = ttk.Frame(status)
        actions.pack(fill=tk.X, pady=(8, 0))
        self.start_btn = ttk.Button(
            actions, text="Start Server", style="Accent.TButton", command=self.start_server,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.stop_btn = ttk.Button(actions, text="Stop Server", command=self.stop_server)
        self.stop_btn.pack(side=tk.LEFT)

        network = ttk.LabelFrame(self, text="Network and Capacity", padding=10)
        network.pack(fill=tk.X, padx=8, pady=4)
        row = ttk.Frame(network)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Access", width=18).pack(side=tk.LEFT)
        self.host_var = tk.StringVar()
        ttk.Combobox(
            row, textvariable=self.host_var, values=list(_HOST_LABELS),
            state="readonly", width=22,
        ).pack(side=tk.LEFT, padx=(0, 12))
        self.port = NumericField(row, "Port", from_=1, to=65535, initial=8000)
        self.port.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row, text="On-demand cache").pack(side=tk.LEFT)
        self.cache_var = tk.StringVar(value="1")
        ttk.Combobox(
            row, textvariable=self.cache_var,
            values=["0", "1", "2", "3", "4"], state="readonly", width=5,
        ).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(row, text="Remote queue").pack(side=tk.LEFT)
        self.queue_var = tk.StringVar(value="4")
        ttk.Combobox(
            row, textvariable=self.queue_var,
            values=["1", "2", "4", "6", "8"], state="readonly", width=5,
        ).pack(side=tk.LEFT, padx=(6, 0))

        key_box = ttk.LabelFrame(self, text="API Key", padding=10)
        key_box.pack(fill=tk.X, padx=8, pady=4)
        self.key_var = tk.StringVar()
        self.key_var.trace_add("write", self._on_key_edited)
        ttk.Entry(key_box, textvariable=self.key_var, show="*", width=48).pack(
            side=tk.LEFT, fill=tk.X, expand=True,
        )
        self.generate_key_btn = ttk.Button(
            key_box, text="Generate New Key", command=self.generate_key
        )
        self.generate_key_btn.pack(side=tk.LEFT, padx=(8, 4))
        ttk.Button(key_box, text="Copy", command=self.copy_key).pack(side=tk.LEFT)
        ttk.Label(
            key_box,
            text=(
                "Any key length is allowed. Delete the field contents and save to "
                "allow access without an API key. Saved keys are stored as one-way hashes."
            ),
            style="Muted.TLabel",
        ).pack(side=tk.BOTTOM, anchor=tk.W, pady=(6, 0))

        models_box = ttk.LabelFrame(self, text="Models Available to Network Clients", padding=8)
        models_box.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        columns = ("alias", "model", "cartridge", "preload", "status")
        self.tree = ttk.Treeview(models_box, columns=columns, show="headings", height=9)
        headings = {
            "alias": "API Name", "model": "Installed Model", "cartridge": "Caliber",
            "preload": "Preload", "status": "Status",
        }
        widths = {"alias": 150, "model": 280, "cartridge": 120, "preload": 80, "status": 130}
        for name in columns:
            self.tree.heading(name, text=headings[name])
            self.tree.column(name, width=widths[name], anchor=tk.W)
        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(models_box)
        buttons.pack(fill=tk.X, pady=(8, 0))
        self.assign_btn = ttk.Button(buttons, text="Assign Model", command=self.assign_model)
        self.assign_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.preload_btn = ttk.Button(buttons, text="Toggle Preload", command=self.toggle_preload)
        self.preload_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.remove_btn = ttk.Button(buttons, text="Remove Assignment", command=self.remove_assignment)
        self.remove_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Upload Pending Feedback", command=self.upload_feedback).pack(side=tk.LEFT)
        self.save_btn = ttk.Button(buttons, text="Save Settings", command=self.save_settings)
        self.save_btn.pack(side=tk.RIGHT)

    def _settings_from_ui(self):
        settings = self.settings_repo.load()
        settings.host = _HOST_LABELS.get(self.host_var.get(), "127.0.0.1")
        settings.port = int(self.port.get())
        settings.on_demand_cache_slots = int(self.cache_var.get())
        settings.remote_queue_limit = int(self.queue_var.get())
        entered = self.key_var.get().strip()
        if self._key_edited:
            settings.set_api_key(entered)
            if entered:
                reporter = getattr(self.app, "crash_reporter", None)
                if reporter is not None:
                    reporter.register_secret(entered)
        settings.validate()
        return settings

    def save_settings(self) -> bool:
        if self.runtime.is_running:
            messagebox.showinfo(
                "Stop the server first",
                "Stop the integrated API server before changing its network settings.",
                parent=self,
            )
            return False
        try:
            settings = self._settings_from_ui()
            self.settings_repo.save(settings)
        except Exception as exc:
            messagebox.showerror("Invalid server settings", str(exc), parent=self)
            return False
        self.app.set_status("API server settings saved.")
        self._key_edited = False
        return True

    def generate_key(self) -> None:
        if self.runtime.is_running:
            messagebox.showinfo(
                "Stop the server first",
                "Stop the integrated API server before replacing its API key.",
                parent=self,
            )
            return
        token = secrets.token_urlsafe(24)
        self.key_var.set(token)
        self.copy_key()
        self.save_settings()
        messagebox.showinfo(
            "New API key",
            "The new API key was copied to the clipboard. Enter it on each remote sorter. "
            "For security, Kiosk cannot display it again after this tab closes.",
            parent=self,
        )

    def copy_key(self) -> None:
        key = self.key_var.get().strip()
        if not key or not self._key_edited:
            messagebox.showinfo(
                "API key unavailable",
                "Generate a new key if the previously saved key is no longer available.",
                parent=self,
            )
            return
        self.clipboard_clear()
        self.clipboard_append(key)

    def _on_key_edited(self, *_args: Any) -> None:
        if not self._setting_key_field:
            self._key_edited = True

    def _selected_alias(self) -> str | None:
        selection = self.tree.selection()
        return str(selection[0]) if selection else None

    def assign_model(self, model_id: int | None = None) -> None:
        if self.runtime.is_running:
            messagebox.showinfo(
                "Stop the server first",
                "Stop the integrated API server before changing model assignments.",
                parent=self,
            )
            return
        trained = [m for m in self.models.list() if m.model_path]
        if not trained:
            messagebox.showinfo("No trained models", "Install or train a local model first.", parent=self)
            return
        dialog = _AssignmentDialog(
            self, trained, self.cartridges.list(), selected_model_id=model_id,
        )
        self.wait_window(dialog)
        if dialog.result is None:
            return
        alias, chosen_id, preload = dialog.result
        existing = self.aliases.get(alias)
        if existing is not None and existing.model_id != chosen_id:
            current = self.models.get(existing.model_id)
            replacement = self.models.get(chosen_id)
            if not messagebox.askyesno(
                "Replace API assignment?",
                f'"{alias}" currently uses {current.name if current else "another model"}.\n\n'
                f"Replace it with {replacement.name if replacement else 'the selected model'}?",
                parent=self,
            ):
                return
        try:
            with self.db.transaction():
                self.aliases.assign(alias, chosen_id, preload=preload)
        except Exception as exc:
            messagebox.showerror("Assignment failed", str(exc), parent=self)
            return
        self.refresh()
        self.app.set_status(f'API name "{alias}" assigned.')

    def toggle_preload(self) -> None:
        if self.runtime.is_running:
            messagebox.showinfo(
                "Stop the server first",
                "Stop the integrated API server before changing preload selections.",
                parent=self,
            )
            return
        alias = self._selected_alias()
        if alias is None:
            return
        item = self.aliases.get(alias)
        if item is None:
            return
        self.aliases.set_preload(alias, not item.preload)
        self.refresh()

    def remove_assignment(self) -> None:
        if self.runtime.is_running:
            messagebox.showinfo(
                "Stop the server first",
                "Stop the integrated API server before removing model assignments.",
                parent=self,
            )
            return
        alias = self._selected_alias()
        if alias is None:
            return
        if not messagebox.askyesno(
            "Remove API assignment?",
            f'Remove "{alias}" from the models available to network clients?\n\n'
            "The installed model and its local settings will not be deleted.",
            parent=self,
        ):
            return
        self.aliases.remove(alias)
        self.refresh()

    def start_server(self) -> None:
        if not self.save_settings():
            return
        settings = self.settings_repo.load()
        self.start_btn.configure(state=tk.DISABLED)
        self.app.set_status("Starting integrated API server…")
        self.app.run_worker(
            lambda: self.runtime.start(settings),
            on_done=lambda _value: self._start_finished(),
            on_error=self._start_failed,
        )

    def _start_finished(self) -> None:
        self.start_btn.configure(state=tk.NORMAL)
        self.refresh()
        self.app.set_status("Integrated API server ready.")

    def _start_failed(self, exc: Exception) -> None:
        self.start_btn.configure(state=tk.NORMAL)
        self.refresh()
        messagebox.showerror("Server did not start", str(exc), parent=self)

    def stop_server(self) -> None:
        self.stop_btn.configure(state=tk.DISABLED)
        self.app.run_worker(
            self.runtime.stop,
            on_done=lambda _value: self._stop_finished(),
            on_error=lambda exc: self._stop_failed(exc),
        )

    def _stop_finished(self) -> None:
        self.stop_btn.configure(state=tk.NORMAL)
        self.refresh()
        self.app.set_status("Integrated API server stopped.")

    def _stop_failed(self, exc: Exception) -> None:
        self.stop_btn.configure(state=tk.NORMAL)
        messagebox.showerror("Server stop failed", str(exc), parent=self)

    def upload_feedback(self) -> None:
        alias = self._selected_alias()
        if alias is None:
            return
        assignment = self.aliases.get(alias)
        if assignment is None:
            return
        if self.runtime.upload_feedback(assignment.model_id):
            self.app.set_status(f"Uploading pending feedback for {alias}…")
        else:
            messagebox.showinfo(
                "Community sign-in required",
                "Sign in to the Community panel on the server host before uploading feedback.",
                parent=self,
            )

    def _on_status(self, _payload: Any) -> None:
        self.refresh()

    def refresh(self) -> None:
        settings = self.settings_repo.load()
        if not self._key_edited:
            self._setting_key_field = True
            try:
                self.key_var.set("•" * 16 if settings.api_key_hash else "")
            finally:
                self._setting_key_field = False
        self.host_var.set(next((label for label, host in _HOST_LABELS.items() if host == settings.host), "This computer only"))
        self.port.var.set(str(settings.port))
        self.cache_var.set(str(settings.on_demand_cache_slots))
        self.queue_var.set(str(settings.remote_queue_limit))
        snap = self.runtime.snapshot()
        self.status_var.set(snap["message"])
        self.address_var.set(snap["address"])
        addresses = primary_local_ipv4_addresses()
        if settings.host == "127.0.0.1":
            self.network_addresses_var.set(
                f"Local endpoint: http://127.0.0.1:{settings.port}"
            )
        elif addresses:
            self.network_addresses_var.set(
                "Network endpoints: "
                + " · ".join(
                    f"http://{address}:{settings.port}"
                    for address in addresses
                )
            )
        else:
            self.network_addresses_var.set(
                "Network endpoint unavailable: no LAN IPv4 address detected."
            )
        running = snap["state"] in {"running", "starting", "stopping"}
        self.start_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.configure(state=tk.NORMAL if running else tk.DISABLED)
        configuration_state = tk.DISABLED if running else tk.NORMAL
        for button in (
            self.generate_key_btn,
            self.assign_btn,
            self.preload_btn,
            self.remove_btn,
            self.save_btn,
        ):
            button.configure(state=configuration_state)
        loaded = {name.casefold() for name in snap["loaded_aliases"]}
        errors = snap["errors"]
        carts = {item.id: item.name for item in self.cartridges.list()}
        for item in self.tree.get_children():
            self.tree.delete(item)
        for assignment in self.aliases.list():
            model = self.models.get(assignment.model_id)
            status = errors.get(assignment.alias, "Loaded" if assignment.alias.casefold() in loaded else "On demand")
            self.tree.insert(
                "", tk.END, iid=assignment.alias,
                values=(
                    assignment.alias,
                    model.name if model else "Missing model",
                    carts.get(model.cartridge_id, "—") if model else "—",
                    "Yes" if assignment.preload else "No",
                    status,
                ),
            )


class _AssignmentDialog(tk.Toplevel):
    def __init__(self, parent, models, cartridges, *, selected_model_id=None) -> None:
        super().__init__(parent)
        self.title("Assign Model to API Server")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        self.result = None
        self._models = {f"{m.name} (#{m.id})": m for m in models}
        carts = {item.id: item.name for item in cartridges}
        chosen = next((label for label, model in self._models.items() if model.id == selected_model_id), next(iter(self._models)))
        body = ttk.Frame(self, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Installed local model").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.model_var = tk.StringVar(value=chosen)
        combo = ttk.Combobox(body, textvariable=self.model_var, values=list(self._models), state="readonly", width=44)
        combo.grid(row=0, column=1, sticky=tk.EW, padx=(8, 0), pady=4)
        ttk.Label(body, text="API model name").grid(row=1, column=0, sticky=tk.W, pady=4)
        selected = self._models[chosen]
        self.alias_var = tk.StringVar(value=carts.get(selected.cartridge_id, selected.name))
        ttk.Entry(body, textvariable=self.alias_var, width=30).grid(row=1, column=1, sticky=tk.EW, padx=(8, 0), pady=4)
        self.preload_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="Preload when server starts", variable=self.preload_var).grid(row=2, column=1, sticky=tk.W, padx=(8, 0), pady=4)
        combo.bind("<<ComboboxSelected>>", lambda _event: self._suggest_alias(carts))
        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Assign", style="Accent.TButton", command=self._accept).pack(side=tk.RIGHT, padx=(0, 6))

    def _suggest_alias(self, carts) -> None:
        model = self._models[self.model_var.get()]
        self.alias_var.set(carts.get(model.cartridge_id, model.name))

    def _accept(self) -> None:
        alias = self.alias_var.get().strip()
        if not alias:
            messagebox.showerror("Missing name", "API model name is required.", parent=self)
            return
        model = self._models[self.model_var.get()]
        self.result = (alias, int(model.id), bool(self.preload_var.get()))
        self.destroy()
