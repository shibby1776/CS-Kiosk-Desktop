"""Serial config tab.

Each NumericField writes back into config.serial.init_settings under the exact
wire-protocol key the firmware expects.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .. import serial_broker
from ..serial_emulator import EMULATED_PORT
from ..events import EventBus
from .widgets import NumericField, build_button_row


# (UI label, init-settings key, min, max, default). Defaults are the
# operator-tuned values; mirror DEFAULT_INIT_SETTINGS in config.py.
# Note: 'sortsteps' lives in the Sort arm panel above (next to slot count)
# rather than here.
INIT_FIELDS = [
    ("Feed homing offset",    "feedhomingoffset",        0,    9999, 0),
    ("Sort homing offset",    "sorthomingoffset",        0,    9999, 0),
    ("Feed speed",            "feedspeed",               0,    255,  90),
    ("Sort speed",            "sortspeed",               0,    255,  90),
    ("Feed cycle steps",      "feedsteps",               0,    9999, 70),
    ("Slot drop delay (ms)",  "slotdropdelay",           0,    9999, 300),
    ("Notification delay",    "notificationdelay",       0,    9999, 160),
    ("Motor standby (s)",     "automotorstandbytimeout", 0,    9999, 0),
    ("Feed motor current",    "feedmotorcurrent",        0,    9999, 900),
    ("Sort motor current",    "sortmotorcurrent",        0,    9999, 900),
    ("Case fan speed",        "fan",                     0,    255,  100),
    ("Debounce timeout (ms)", "debounceTimeout",         0,    9999, 500),
    ("Debounce pause (ms)",   "debounceTime",            0,    9999, 300),
    ("Camera LED level",      "cameraledlevel",          0,    255,  130),
]

AIRDROP_FIELDS = [
    ("Pre-drop delay (ms)",   "airdroppredelay",         0, 1500, 50),
    ("Signal duration (ms)",  "airdropdsignalduration",  0, 1500, 70),
    ("Post-drop delay (ms)",  "airdroppostdelay",        0, 1500, 50),
]


class SerialTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, config, bus: EventBus, app):
        super().__init__(parent)
        self.config = config
        self.bus = bus
        self.app = app
        ser_cfg = config.serial
        init_settings = ser_cfg.get("init_settings", {})

        connect = ttk.LabelFrame(self, text="Connection")
        connect.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

        ttk.Label(connect, text="USB port").grid(row=0, column=0, padx=6, pady=4, sticky=tk.W)
        existing = str(ser_cfg.get('port', ''))
        self.wifi_var = tk.BooleanVar(value=bool(ser_cfg.get('wifi_enabled', existing.startswith(('http://','https://')))))
        self.wifi_address_var = tk.StringVar(value=ser_cfg.get('wifi_address', existing if self.wifi_var.get() else ''))
        self.port_var = tk.StringVar(value=ser_cfg.get('usb_port', '' if self.wifi_var.get() else existing))
        self.port_combo = ttk.Combobox(connect, textvariable=self.port_var, width=24)
        self.port_combo.grid(row=0, column=1, padx=6, pady=4, sticky=tk.W)
        ttk.Button(connect, text="Refresh ports", command=self.refresh_ports).grid(row=0, column=2, padx=6)

        ttk.Label(connect, text="Baud").grid(row=0, column=3, padx=6, pady=4, sticky=tk.W)
        self.baud_var = tk.IntVar(value=int(ser_cfg.get("baud", 9600)))
        ttk.Spinbox(connect, from_=1200, to=2_000_000, increment=100,
                    textvariable=self.baud_var, width=10).grid(row=0, column=4, padx=6, sticky=tk.W)

        ttk.Label(connect, text="Probe timeout (s)").grid(
            row=0, column=5, padx=6, pady=4, sticky=tk.W
        )
        self.probe_timeout_var = tk.DoubleVar(
            value=float(ser_cfg.get("handshake_timeout_s", 4.0))
        )
        ttk.Spinbox(
            connect, from_=0.5, to=10.0, increment=0.5,
            textvariable=self.probe_timeout_var, width=6,
        ).grid(row=0, column=6, padx=6, sticky=tk.W)

        self.init_on_startup_var = tk.BooleanVar(
            value=bool(ser_cfg.get("init_on_startup", False))
        )
        ttk.Checkbutton(
            connect,
            text="Initialize these settings on startup",
            variable=self.init_on_startup_var,
        ).grid(row=1, column=0, columnspan=3, padx=6, sticky=tk.W)

        build_button_row(connect, [
            ("Connect", self.connect_with_selected),
            ("Disconnect", self.app.disconnect_serial),
            ("Get config from board", self.fetch_board_config),
            ("Push to board", self.push_to_board),
            ("Save", self.save),
            ("Import previous setup", self.import_setup),
        ], primary="Connect").grid(row=2, column=0, columnspan=7, padx=4, pady=4, sticky=tk.W)

        ttk.Checkbutton(connect, text='Kiosk Node (network sorter)', variable=self.wifi_var,
                        command=self._wifi_changed).grid(row=3,column=0,columnspan=2,padx=6,pady=4,sticky=tk.W)
        self.wifi_entry = ttk.Entry(connect,textvariable=self.wifi_address_var,width=28)
        self.wifi_entry.grid(row=3,column=2,columnspan=3,padx=6,sticky=tk.W)
        self._update_connection_mode()
        from ..sorter_profiles import SorterProfiles
        profiles = SorterProfiles(self.config)
        ttk.Label(connect,text='Saved sorter').grid(row=4,column=0,padx=6,sticky=tk.W)
        self.profile_var = tk.StringVar(value=profiles.active())
        self.profile_combo = ttk.Combobox(connect,textvariable=self.profile_var,values=profiles.names(),width=24)
        self.profile_combo.grid(row=4,column=1,padx=6,pady=4,sticky=tk.W)
        ttk.Button(connect,text='Save sorter',command=self._save_profile).grid(row=4,column=2,padx=6)
        ttk.Button(connect,text='Load sorter',command=self._load_profile).grid(row=4,column=3,padx=6)

        # ---- Slot count + sort-arm test ------------------------------------
        sorter_box = ttk.LabelFrame(self, text="Sort arm")
        sorter_box.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

        ttk.Label(sorter_box, text="Slot count").grid(row=0, column=0, padx=6, pady=4, sticky=tk.W)
        self.slot_count_var = tk.IntVar(value=int(ser_cfg.get("slot_quantity", 8)))
        ttk.Spinbox(
            sorter_box, from_=1, to=64, textvariable=self.slot_count_var, width=6,
        ).grid(row=0, column=1, padx=6, pady=4, sticky=tk.W)

        ttk.Label(sorter_box, text="Sort slot steps").grid(row=0, column=2, padx=6, pady=4, sticky=tk.W)
        # Pull the initial value from init_settings so the field tracks the
        # firmware param; saving here also writes back into init_settings.
        self.sort_steps_var = tk.IntVar(value=int(init_settings.get("sortsteps", 20)))
        ttk.Spinbox(
            sorter_box, from_=0, to=9999, textvariable=self.sort_steps_var, width=6,
        ).grid(row=0, column=3, padx=6, pady=4, sticky=tk.W)

        ttk.Label(sorter_box, text="Sort to slot").grid(row=1, column=0, padx=6, pady=4, sticky=tk.W)
        self.sort_to_var = tk.IntVar(value=0)
        self._sort_to_initialized = False
        ttk.Spinbox(
            sorter_box, from_=0, to=64, textvariable=self.sort_to_var, width=6,
        ).grid(row=1, column=1, padx=6, pady=4, sticky=tk.W)
        # Use trace_add so any change to the value — keyboard, arrow, paste —
        # fires sortto:N. Suppress the initial set during construction.
        self.sort_to_var.trace_add("write", lambda *_args: self._on_sort_to_changed())

        ttk.Button(
            sorter_box, text="Home sorter (sortto:0)", command=self._home_sorter,
        ).grid(row=1, column=2, padx=6, pady=4, sticky=tk.W)
        self._sort_to_initialized = True

        # ---- init-settings fields ----
        init_box = ttk.LabelFrame(self, text="Board init settings")
        init_box.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        self.init_widgets: dict[str, NumericField] = {}
        for idx, (label, key, lo, hi, dflt) in enumerate(INIT_FIELDS):
            value = int(init_settings.get(key, dflt))
            field = NumericField(init_box, label, from_=lo, to=hi, initial=value)
            field.grid_master = init_box
            field.grid(row=idx // 3, column=idx % 3, padx=6, pady=4, sticky=tk.W)
            self.init_widgets[key] = field

        # ---- airdrop ----
        airdrop = ttk.LabelFrame(self, text="Airdrop configuration")
        airdrop.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        self.airdrop_enabled_var = tk.BooleanVar(
            value=bool(int(init_settings.get("airdropenabled", 0)))
        )
        ttk.Checkbutton(airdrop, text="Airdrop enabled", variable=self.airdrop_enabled_var)\
            .grid(row=0, column=0, padx=6, pady=4, sticky=tk.W)
        for idx, (label, key, lo, hi, dflt) in enumerate(AIRDROP_FIELDS):
            value = int(init_settings.get(key, dflt))
            field = NumericField(airdrop, label, from_=lo, to=hi, initial=value)
            field.grid(row=0, column=idx + 1, padx=6, pady=4, sticky=tk.W)
            self.init_widgets[key] = field
        self.airdrop_enabled_var.trace_add('write',lambda *_:self._update_airdrop_fields())
        self._update_airdrop_fields()

        # ---- firmware information (read-only) -------------------------------
        # The board's exact firmware string is obtained during the serial
        # handshake by sending the firmware-supported `version` command.
        # Nothing here is hard-coded and this panel provides no flashing or
        # update controls.
        firmware = ttk.LabelFrame(self, text="Arduino firmware information")
        firmware.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

        ttk.Label(firmware, text="Firmware version").grid(
            row=0, column=0, padx=8, pady=6, sticky=tk.W
        )
        self.firmware_version_var = tk.StringVar(value="Not connected")
        ttk.Label(
            firmware,
            textvariable=self.firmware_version_var,
            font=("TkDefaultFont", 12, "bold"),
        ).grid(row=0, column=1, padx=8, pady=6, sticky=tk.W)

        self.after(250, self._refresh_firmware_info)

        # ---- monitor & debug ----
        monitor = ttk.LabelFrame(self, text="Serial monitor / debug")
        monitor.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)

        cmd_row = ttk.Frame(monitor)
        cmd_row.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)
        ttk.Label(cmd_row, text="Command").pack(side=tk.LEFT, padx=4)
        self.cmd_var = tk.StringVar()
        entry = ttk.Entry(cmd_row, textvariable=self.cmd_var, width=40)
        entry.pack(side=tk.LEFT, padx=4)
        entry.bind("<Return>", lambda _e: self.send_command())
        ttk.Button(cmd_row, text="Send", command=self.send_command).pack(side=tk.LEFT)

        self.log = tk.Text(monitor, height=10, wrap=tk.NONE, state=tk.DISABLED)
        self.log.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.refresh_ports()

    def _refresh_firmware_info(self) -> None:
        """Mirror the firmware string captured from the live Arduino handshake."""
        broker = getattr(self.app, "broker", None)
        if broker is not None and getattr(broker, "is_connected", False):
            reported = (getattr(broker, "firmware_version", "") or "").strip()
            self.firmware_version_var.set(reported or "Not reported")
        else:
            self.firmware_version_var.set("Not connected")
        try:
            self.after(500, self._refresh_firmware_info)
        except tk.TclError:
            pass

    # ----- handlers -----------------------------------------------------------

    def connect_with_selected(self) -> None:
        """Save the form first so config matches what's on screen, then connect
        to the port currently shown in the dropdown."""
        try:
            self.save()
        except ValueError as exc:
            messagebox.showerror('Connection',str(exc),parent=self);return
        port = self.config.serial.get('port','').strip()
        if not port:
            messagebox.showerror("No port selected",
                                 "Pick a port from the dropdown (or click Refresh ports).")
            return
        self.app.connect_serial(port=port)

    def import_setup(self):
        from tkinter import filedialog
        import json
        from pathlib import Path
        path=filedialog.askopenfilename(parent=self, title='Import previous sorter settings or report',filetypes=[('JSON settings or report','*.json')])
        if not path:return
        if not messagebox.askyesno('Import setup','Replace this application connection, camera crop and remote bin profile with the selected setup? Local models and saved layouts are retained.',parent=self):return
        try:
            result=self.app.import_connection_settings(json.loads(Path(path).read_text(encoding='utf-8-sig')))
            messagebox.showinfo('Imported',result['message'],parent=self)
        except Exception as exc:messagebox.showerror('Import',str(exc),parent=self)

    def _update_connection_mode(self):
        remote = self.wifi_var.get()
        self.port_combo.configure(state='disabled' if remote else 'normal')
        self.wifi_entry.configure(state='normal' if remote else 'disabled')

    def _update_airdrop_fields(self):
        state='normal' if self.airdrop_enabled_var.get() else 'disabled'
        for _label,key,_lo,_hi,_default in AIRDROP_FIELDS:
            self.init_widgets[key].spin.configure(state=state)

    def _wifi_changed(self):
        if self.wifi_var.get():
            value = simpledialog.askstring('Kiosk Node','Enter the Kiosk Node IP address:',
                                          initialvalue=self.wifi_address_var.get(),parent=self)
            if value is None:
                self.wifi_var.set(False)
            else:
                try:
                    from ..connections import wifi_address
                    self.wifi_address_var.set(wifi_address(value))
                    self.init_on_startup_var.set(False)
                except ValueError:
                    self.wifi_var.set(False)
                    messagebox.showerror('Invalid IP','Enter the sorter IP, for example 192.168.4.92.',parent=self)
        self._update_connection_mode()

    def load_connection_form(self):
        c=self.config.serial;port=str(c.get('port',''))
        self.wifi_var.set(bool(c.get('wifi_enabled',port.startswith(('http://','https://')))))
        self.wifi_address_var.set(c.get('wifi_address',port if self.wifi_var.get() else ''))
        self.port_var.set(c.get('usb_port','' if self.wifi_var.get() else port))
        self.baud_var.set(c.get('baud',9600));self.probe_timeout_var.set(c.get('handshake_timeout_s',4))
        self.slot_count_var.set(c.get('slot_quantity',8));self.init_on_startup_var.set(c.get('init_on_startup',False))
        init=c.get('init_settings',{})
        for key,field in self.init_widgets.items(): field.set(init.get(key,0))
        self.sort_steps_var.set(init.get('sortsteps',20));self.airdrop_enabled_var.set(bool(int(init.get('airdropenabled',0))))
        self._update_connection_mode();self.refresh_ports()

    def _save_profile(self):
        try:
            self.app.ensure_connection_idle()
            self.save()
            from ..sorter_profiles import SorterProfiles
            profiles=SorterProfiles(self.config);self.profile_var.set(profiles.save(self.profile_var.get()))
            self.profile_combo['values']=profiles.names();self.app.set_status('Sorter profile saved.')
        except Exception as exc: messagebox.showerror('Sorter profile',str(exc),parent=self)

    def _load_profile(self):
        try: self.app.select_sorter_profile(self.profile_var.get())
        except Exception as exc: messagebox.showerror('Sorter profile',str(exc),parent=self)

    def refresh_ports(self) -> None:
        ports = serial_broker.list_serial_ports() + [EMULATED_PORT]
        self.port_combo["values"] = ports
        if self.port_var.get() not in ports and ports:
            self.port_var.set(ports[0])

    def save(self) -> None:
        from ..connections import connection_settings
        self.config.data['serial'] = connection_settings(self.config.serial,
            wifi_enabled=self.wifi_var.get(),address=self.wifi_address_var.get(),usb_port=self.port_var.get())
        self.config.serial["baud"] = int(self.baud_var.get())
        self.config.serial["handshake_timeout_s"] = float(self.probe_timeout_var.get())
        self.config.serial["slot_quantity"] = int(self.slot_count_var.get())
        self.config.serial["init_on_startup"] = bool(self.init_on_startup_var.get())
        init_settings = dict(self.config.serial.get("init_settings", {}))
        for key, field in self.init_widgets.items():
            init_settings[key] = int(field.get())
        init_settings["airdropenabled"] = 1 if self.airdrop_enabled_var.get() else 0
        try:
            init_settings["sortsteps"] = int(self.sort_steps_var.get())
        except (tk.TclError, ValueError):
            pass
        self.config.serial["init_settings"] = init_settings
        self.config.save()
        self.app.set_status("Serial settings saved.")

    # ----- Sort arm test helpers --------------------------------------------

    def _on_sort_to_changed(self) -> None:
        if not getattr(self, "_sort_to_initialized", False):
            return
        try:
            slot = int(self.sort_to_var.get())
        except (tk.TclError, ValueError):
            return
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            self.app.set_status(f"Sort to {slot}: not connected.")
            return
        broker.move_sorter_to_slot(slot)
        self.app.set_status(f"sortto:{slot}")

    def _home_sorter(self) -> None:
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            messagebox.showerror("Not connected", "Connect to the board first.")
            return
        broker.move_sorter_to_slot(0)
        # Reset the spinbox to 0 so subsequent changes start from a known state.
        # Bypass the trace by toggling the init flag.
        self._sort_to_initialized = False
        self.sort_to_var.set(0)
        self._sort_to_initialized = True
        self.app.set_status("Homed sorter (sortto:0).")

    def _settings_for_board(self) -> dict[str, int]:
        """Return only settings that should be transmitted to firmware.

        The enable flag is always sent so an already-enabled board can be disabled.
        Timing fields are omitted entirely unless the operator enabled airdrop.
        """
        settings = dict(self.config.serial["init_settings"])
        enabled = bool(self.airdrop_enabled_var.get())
        settings["airdropenabled"] = 1 if enabled else 0
        if not enabled:
            for _label, key, _lo, _hi, _default in AIRDROP_FIELDS:
                settings.pop(key, None)
        return settings

    def push_to_board(self) -> None:
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            messagebox.showerror("Not connected", "Connect to the board first.")
            return
        self.save()
        self.app.set_status("Pushing init settings to board…")
        self.app.run_worker(
            lambda: broker.update_init_settings(self._settings_for_board()),
            on_done=lambda _r: self.app.set_status("Init settings pushed."),
            on_error=lambda err: messagebox.showerror("Serial error", str(err)),
        )

    def fetch_board_config(self) -> None:
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            messagebox.showerror("Not connected", "Connect to the board first.")
            return
        self.app.set_status("Requesting config from board…")
        self.app.run_worker(
            broker.get_config,
            on_done=self._apply_board_config,
            on_error=lambda err: messagebox.showerror("Serial error", str(err)),
        )

    def _apply_board_config(self, payload) -> None:
        if not payload:
            self.app.set_status("Board returned no config.")
            return
        from ..machine_settings import normalized_board_config
        payload=normalized_board_config(payload)
        if 'airdropenabled' in payload:
            self.airdrop_enabled_var.set(bool(int(payload['airdropenabled'])))
        applied = 0
        for key, value in payload.items():
            if key in self.init_widgets:
                try:
                    self.init_widgets[key].set(int(value))
                    applied += 1
                except (TypeError, ValueError):
                    pass
            elif key == "sortsteps":
                # sortsteps lives in the Sort arm panel, not init_widgets.
                try:
                    self.sort_steps_var.set(int(value))
                    applied += 1
                except (TypeError, ValueError, tk.TclError):
                    pass
        self.app.set_status(f"Loaded {applied} value(s) from board.")

    def send_command(self) -> None:
        broker = self.app.broker
        if broker is None:
            messagebox.showerror("Not connected", "Connect to the board first.")
            return
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return
        broker.send_command(cmd)
        self.cmd_var.set("")

    # ----- log API used by the App --------------------------------------------

    def append_log(self, line: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, line + "\n")
        self.log.see(tk.END)
        # Trim to last ~500 lines.
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 600:
            self.log.delete("1.0", f"{line_count - 500}.0")
        self.log.configure(state=tk.DISABLED)
