"""Maintenance controls for the optional browser-based sorter interface."""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from ..network_info import primary_local_ipv4_addresses
from ..web_settings import WebSettings, normalize_sorter_name


class WebAccessTab(ttk.Frame):
    """Configure Web Interface without starting resources until enabled."""

    def __init__(self, parent, *, config, bus, app) -> None:
        super().__init__(parent, padding=18)
        self.config = config
        self.bus = bus
        self.app = app
        self.columnconfigure(1, weight=1)
        self._build()
        self.refresh()

    def _build(self) -> None:
        ttk.Label(self, text="LAN Access", style="Header.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        ttk.Label(
            self,
            text=(
                "Use the complete sorter interface from a browser "
                "on this machine's private network. The local monitor may remain "
                "available or be turned off. Browser access is restricted to "
                "private-network clients."
            ),
            style="KioskBody.TLabel",
            wraplength=780,
            justify=tk.LEFT,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))

        self.enabled_var = tk.BooleanVar()
        self.desktop_enabled_var = tk.BooleanVar()
        self.sorter_name_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.status_var = tk.StringVar()

        ttk.Checkbutton(
            self,
            text="Enable Web Interface",
            variable=self.enabled_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(
            self,
            text="Keep the Desktop Interface available on this PC",
            variable=self.desktop_enabled_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=3)

        fields = (
            ("Sorter name", self.sorter_name_var),
            ("Web port", self.port_var),
        )
        for row, (label, variable) in enumerate(fields, start=4):
            ttk.Label(self, text=label, style="KioskCaption.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 22), pady=5
            )
            ttk.Entry(
                self,
                textvariable=variable,
                width=38,
            ).grid(row=row, column=1, sticky="w", pady=5)

        address_box = ttk.LabelFrame(self, text="Browser Addresses", padding=10)
        address_box.grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=(10, 10)
        )
        address_box.columnconfigure(0, weight=1)
        ttk.Label(
            address_box,
            textvariable=self.status_var,
            style="KioskBody.TLabel",
            wraplength=780,
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky="w")

        actions = ttk.Frame(self)
        actions.grid(row=7, column=0, columnspan=2, sticky="w")
        ttk.Button(
            actions,
            text="Save LAN Access Settings",
            command=self.save,
            style="Accent.TButton",
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="Open Web Interface",
            command=self.open_interface,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions,
            text="Refresh Addresses",
            command=self.refresh,
        ).pack(side=tk.LEFT, padx=(8, 0))

    def _status_text(self, settings: WebSettings) -> str:
        state = "Enabled" if settings.enabled else "Disabled"
        running = "Running" if self.app.web_server is not None else "Stopped"
        direct = [
            f"http://{address}:{settings.port}"
            for address in primary_local_ipv4_addresses()
        ]
        return (
            f"Web Interface: {state} · service: {running}\n"
            f"Friendly address: http://{settings.sorter_name}.local:{settings.port}\n"
            f"Primary LAN address: {' · '.join(direct) if direct else 'Unavailable'}"
        )

    def refresh(self) -> None:
        settings = WebSettings.load(self.config.settings)
        self.enabled_var.set(settings.enabled)
        self.desktop_enabled_var.set(settings.desktop_enabled)
        self.sorter_name_var.set(settings.sorter_name)
        self.port_var.set(str(settings.port))
        self.status_var.set(self._status_text(settings))

    def save(self) -> None:
        try:
            name = normalize_sorter_name(self.sorter_name_var.get())
            port = int(self.port_var.get())
            if not 1024 <= port <= 65535:
                raise ValueError("Web port must be between 1024 and 65535.")
            if not self.desktop_enabled_var.get() and not self.enabled_var.get():
                raise ValueError("Desktop and Web Interface cannot both be disabled.")

            previous = WebSettings.load(self.config.settings)
            updated = WebSettings(
                enabled=self.enabled_var.get(),
                desktop_enabled=self.desktop_enabled_var.get(),
                sorter_name=name,
                port=port,
            )

            endpoint_changed = bool(
                previous.port != updated.port
                or previous.sorter_name != updated.sorter_name
            )
            if (
                self.app.web_server is not None
                and (not updated.enabled or endpoint_changed)
                and self.app.web_server.operations.web_training_active
            ):
                raise RuntimeError(
                    "Finish or cancel browser-started training before changing LAN Access."
                )
            updated.save(self.config.settings)

            if self.app.web_server is not None and (
                not updated.enabled or endpoint_changed
            ):
                self.app.stop_web_interface()
            if updated.enabled and self.app.web_server is None:
                self.app.start_web_interface(updated)

            self.status_var.set(self._status_text(updated))
            if updated.enabled:
                messagebox.showinfo(
                    "Web Interface enabled",
                    f"Friendly address:\nhttp://{updated.sorter_name}.local:{updated.port}\n\n"
                    "The direct IP addresses are shown on this tab.",
                    parent=self,
                )
        except Exception as exc:
            messagebox.showerror("LAN Access settings", str(exc), parent=self)

    def open_interface(self) -> None:
        current = WebSettings.load(self.config.settings)
        if not current.enabled:
            messagebox.showinfo(
                "Web Interface disabled",
                "Enable and save LAN Access before opening it.",
                parent=self,
            )
            return
        if self.app.web_server is None:
            try:
                self.app.start_web_interface(current)
            except Exception as exc:
                messagebox.showerror(
                    "Web Interface unavailable", str(exc), parent=self
                )
                return
        # The local loopback address is deterministic on the host PC even when
        # mDNS is unavailable or Windows has selected the wrong adapter.
        webbrowser.open(f"http://127.0.0.1:{current.port}")
