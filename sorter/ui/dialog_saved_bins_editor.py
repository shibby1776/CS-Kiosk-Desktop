"""Small editor for one portable Saved Bins layout."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ..saved_bins import (
    SavedBinAssignment,
    SavedBinsProfile,
    SavedBinsService,
    SavedBinsStore,
    SavedBinsTarget,
)

_MIXED_BINS = "Multiple values"


class SavedBinsEditor(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        profile: SavedBinsProfile,
        target: SavedBinsTarget,
        service: SavedBinsService,
        store: SavedBinsStore,
        slot_count: int,
        on_saved,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        self.target = target
        self.service = service
        self.store = store
        self.slot_count = int(slot_count)
        self.on_saved = on_saved
        self._rows: dict[str, dict[str, object]] = {}
        self._edited_bins: dict[tuple[str, str], tuple[int, ...]] = {}

        self.title(f"Edit Saved Bins — {profile.layout_name}")
        self.geometry("720x560")
        self.minsize(620, 460)
        self.transient(parent.winfo_toplevel())

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        ttk.Label(
            outer,
            text=(
                f"Target: {target.name}  •  Caliber: {target.caliber}\n"
                "Select one or more classifications, choose a bin, then press "
                "Set Selected. Use Ctrl-click or Shift-click to select multiple. "
                "New model classifications start unassigned. Unavailable names "
                "remain preserved. Only Save Changes modifies this layout."
            ),
            wraplength=680,
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 10))

        columns = ("name", "bin", "status")
        self.tree = ttk.Treeview(
            outer, columns=columns, show="headings", selectmode="extended"
        )
        self.tree.heading("name", text="Classification")
        self.tree.heading("bin", text="Saved bin")
        self.tree.heading("status", text="Target status")
        self.tree.column("name", width=310, stretch=True)
        self.tree.column("bin", width=110, anchor=tk.CENTER, stretch=False)
        self.tree.column("status", width=150, stretch=False)
        scroll = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_selected)

        editor = ttk.Frame(outer)
        editor.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(editor, text="Bin:").pack(side=tk.LEFT)
        self.bin_var = tk.StringVar()
        values = ["Unassigned"] + [
            str(number) for number in range(1, max(1, self.slot_count))
        ]
        self.bin_combo = ttk.Combobox(
            editor,
            textvariable=self.bin_var,
            values=values,
            state="readonly",
            width=14,
        )
        self.bin_combo.pack(side=tk.LEFT, padx=(6, 8))
        ttk.Button(
            editor, text="Set Selected", command=self._set_selected
        ).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(editor, text="Set Unassigned", command=self._clear_selected).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(editor, text="Save Changes", command=self._save).pack(
            side=tk.RIGHT
        )
        ttk.Button(editor, text="Cancel", command=self.destroy).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

        self._populate()
        self.wait_visibility()
        self.focus_set()

    def _populate(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        self._edited_bins.clear()
        for index, row in enumerate(self.service.edit_rows(self.profile, self.target)):
            iid = f"row-{index}"
            key = row["key"]
            bins = tuple(row["bins"])
            self._rows[iid] = row
            self._edited_bins[key] = bins
            self.tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    row["name"],
                    self._format_bins(bins),
                    "Available" if row["available"] else "Preserved; not in model",
                ),
            )
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])
            self._on_selected()

    @staticmethod
    def _format_bins(bins: tuple[int, ...]) -> str:
        return ", ".join(str(number) for number in bins) if bins else "Unassigned"

    def _selected_rows(self) -> list[tuple[str, dict[str, object]]]:
        return [
            (iid, self._rows[iid])
            for iid in self.tree.selection()
            if iid in self._rows
        ]

    def _on_selected(self, _event=None) -> None:
        selected = [
            row for _iid, row in self._selected_rows() if row["available"]
        ]
        if not selected:
            self.bin_combo.configure(state=tk.DISABLED)
            self.bin_var.set("Unavailable")
            return
        selected_bins = {
            self._edited_bins.get(row["key"], ()) for row in selected
        }
        bins = next(iter(selected_bins)) if len(selected_bins) == 1 else None
        if self.profile.routing_mode == "package":
            self.bin_combo.configure(state=tk.NORMAL)
            self.bin_var.set(
                ", ".join(str(number) for number in bins)
                if bins is not None
                else _MIXED_BINS
            )
        else:
            self.bin_combo.configure(state="readonly")
            if bins is None:
                self.bin_var.set(_MIXED_BINS)
            else:
                self.bin_var.set(str(bins[0]) if bins else "Unassigned")

    def _parse_bins(self) -> tuple[int, ...]:
        text = self.bin_var.get().strip()
        if text == _MIXED_BINS:
            raise ValueError("Choose a bin before setting multiple classifications.")
        if not text or text.casefold() == "unassigned":
            return ()
        parts = [part.strip() for part in text.split(",")]
        try:
            bins = tuple(sorted({int(part) for part in parts if part}))
        except ValueError as exc:
            raise ValueError("Bins must be numbers separated by commas.") from exc
        maximum = max(0, self.slot_count - 1)
        if not bins or any(number < 1 or number > maximum for number in bins):
            raise ValueError(f"Bins must be between 1 and {maximum}.")
        if self.profile.routing_mode != "package" and len(bins) != 1:
            raise ValueError("This routing mode allows one bin per classification.")
        return bins

    def _set_selected(self) -> bool:
        selected = self._selected_rows()
        if not selected:
            return True
        available = [
            (iid, row) for iid, row in selected if row["available"]
        ]
        if not available:
            messagebox.showinfo(
                "Classification unavailable",
                "The selected names are preserved in the file but cannot be edited for "
                "the selected target model.",
                parent=self,
            )
            return False
        try:
            bins = self._parse_bins()
        except ValueError as exc:
            messagebox.showerror("Invalid bin", str(exc), parent=self)
            return False
        for iid, row in available:
            self._edited_bins[row["key"]] = bins
            values = list(self.tree.item(iid, "values"))
            values[1] = self._format_bins(bins)
            self.tree.item(iid, values=values)
        return True

    def _clear_selected(self) -> None:
        self.bin_var.set("Unassigned")
        self._set_selected()

    def _save(self) -> None:
        selected = self._selected_rows()
        if (
            any(row["available"] for _iid, row in selected)
            and self.bin_var.get() != _MIXED_BINS
        ):
            if not self._set_selected():
                return
        assignments: list[SavedBinAssignment] = []
        for row in self._rows.values():
            if not row["available"]:
                continue
            bins = self._edited_bins.get(row["key"], ())
            if not bins:
                continue
            assignments.append(
                SavedBinAssignment(
                    name=str(row["name"]),
                    bins=bins,
                    kind=str(row["kind"]),
                )
            )
        try:
            self.service.replace_target_assignments(
                self.profile,
                self.target,
                assignments,
                self.slot_count,
            )
            destination = self.store.write(
                self.profile, path=self.profile.path, overwrite=True
            )
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)
            return
        self.on_saved(destination)
        self.destroy()
