"""Run tab.

Layout:
  +-----------------------------------+-----------------+
  | left panel (66%)                  | right panel     |
  | +-------------------------------+ | (34%)           |
  | | SlotGrid                      | | run controls   |
  | | (flow layout)                 | | Start / Stop   |
  | |                               | | Manual feed    |
  | +-------------------------------+ | Master + reset |
  | | slot details                  | | Last result    |
  | | header + per-headstamp        | | Last cropped   |
  | | checkbox + count              | |                |
  | +-------------------------------+ +----------------+
  +-----------------------------------+-----------------+

Outer split is horizontal (resizable); left side is itself a vertical
PanedWindow so the slot-details panel can be resized.

Each slot card is clickable; clicking it shows that slot's details below.
Slot 0 is the catch-all and cannot be configured — anything classified into
an unmapped headstamp ends up there.
"""
from __future__ import annotations

import tkinter as tk
from collections import defaultdict
from tkinter import messagebox, ttk
from typing import Callable

from ..events import EventBus, plan_assignment_refresh, post_assignment_changed
from ..feedback import FeedbackService, debug_log, is_feedback_model
from ..repository import ModelRepo
from .theme import PALETTE
from .widgets import ImagePanel


CARD_WIDTH = 240
CARD_MIN_HEIGHT = 110
HEADSTAMP_CELL_WIDTH = 200

# Store-images dropdown: display label <-> persisted mode (see Config).
_STORE_IMAGES_LABELS = {
    "none": "None",
    "above": "Above Confidence Floor",
    "below": "Below Confidence Floor",
    "all": "All Images",
}
_STORE_IMAGES_BY_LABEL = {label: mode for mode, label in _STORE_IMAGES_LABELS.items()}


# ----- Flow-layout container --------------------------------------------------


class FlowGrid(ttk.Frame):
    """Container that re-arranges its children in a left-to-right flow on resize.

    Used for both the slot card grid (wider cells, expand=True) and the
    headstamp checkbox grid inside the slot details panel (narrower cells,
    left-aligned).
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        cell_width: int = CARD_WIDTH,
        gutter: int = 8,
        expand_cells: bool = True,
    ):
        super().__init__(parent)
        self._cells: list[tk.Widget] = []
        self._cell_width = cell_width
        self._gutter = gutter
        self._expand_cells = expand_cells
        self._cols = 0
        self.bind("<Configure>", self._on_configure)

    def add(self, cell: tk.Widget) -> None:
        """Add a cell without repeatedly laying out all preceding cells."""
        self._cells.append(cell)

    def finish(self) -> None:
        """Lay out a completed batch of cells exactly once."""
        self._reflow(force=True)

    def clear(self) -> None:
        for cell in self._cells:
            cell.destroy()
        self._cells.clear()
        self._cols = 0
        # Reset column weights so leftover configuration doesn't linger.
        for c in range(64):
            self.grid_columnconfigure(c, weight=0, minsize=0)

    def _on_configure(self, _event=None) -> None:
        self._reflow()

    def _reflow(self, *, force: bool = False) -> None:
        if not self._cells:
            return
        width = max(1, self.winfo_width())
        cols = max(1, width // (self._cell_width + self._gutter))
        if not force and cols == self._cols:
            return
        self._cols = cols
        for i, cell in enumerate(self._cells):
            row, col = divmod(i, cols)
            cell.grid(
                row=row, column=col,
                padx=self._gutter // 2, pady=self._gutter // 2,
                sticky=tk.NSEW if self._expand_cells else tk.NW,
            )
        if self._expand_cells:
            for c in range(cols):
                self.grid_columnconfigure(c, weight=1, minsize=self._cell_width)
        else:
            for c in range(cols):
                self.grid_columnconfigure(c, weight=0, minsize=self._cell_width)


# Back-compat alias used by tests and earlier consumers.
SlotGrid = FlowGrid


# ----- Per-slot card ----------------------------------------------------------


class SlotCard(ttk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        slot_number: int,
        on_click: Callable[[int], None],
        on_reset: Callable[[int], None] | None = None,
    ):
        super().__init__(parent, style="Card.TFrame", padding=12)
        self.slot_number = slot_number
        self._on_click = on_click
        self._on_reset = on_reset
        self._selected = False

        self._title_var = tk.StringVar()
        self._headstamps_var = tk.StringVar(value="(no headstamps)")
        self._count_var = tk.StringVar(value="0")
        self._set_title()

        self.title_label = ttk.Label(
            self, textvariable=self._title_var,
            style="CardTitle.TLabel",
        )
        self.title_label.pack(anchor=tk.W)

        self.headstamps_label = ttk.Label(
            self, textvariable=self._headstamps_var,
            wraplength=CARD_WIDTH - 36,
            style="CardMuted.TLabel",
        )
        self.headstamps_label.pack(anchor=tk.W, pady=(6, 8))

        count_row = ttk.Frame(self, style="Card.TFrame")
        count_row.pack(fill=tk.X)
        self.count_caption = ttk.Label(
            count_row, text="Count",
            style="CardSubtle.TLabel",
        )
        self.count_caption.pack(side=tk.LEFT)
        self.count_label = ttk.Label(
            count_row, textvariable=self._count_var,
            style="CardTitle.TLabel",
        )
        self.count_label.pack(side=tk.RIGHT)

        # Live batch-reset (package mode only, never the catch-all). Lets the
        # operator dump a full bin and zero its counter while other slots keep
        # filling. Hidden until set_package_mode(True) is called.
        self._reset_row = ttk.Frame(self, style="Card.TFrame")
        self._reset_btn = ttk.Button(
            self._reset_row, text="⟲ Reset count",
            command=self._reset_clicked, width=16,
        )
        self._reset_btn.pack(side=tk.LEFT, pady=(8, 0))

        self._clickable_children = (
            self, self.title_label, self.headstamps_label,
            count_row, self.count_caption, self.count_label,
        )
        for w in self._clickable_children:
            w.bind("<Button-1>", lambda _e: self._on_click(self.slot_number))
        # Hover only on the outer frame — Tk fires <Leave> on the parent
        # whenever the pointer moves into a child, so we check the actual
        # pointer position to decide if it's a real "leave".
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _set_title(self) -> None:
        self._title_var.set(
            "Catch-All" if self.slot_number == 0 else f"Slot #{self.slot_number}"
        )

    def set_headstamps(self, names: list[str]) -> None:
        if names:
            self._headstamps_var.set(", ".join(names))
        else:
            self._headstamps_var.set(
                "Unclassified / unmapped" if self.slot_number == 0 else "(no headstamps)"
            )

    def set_count(self, value: int) -> None:
        self._count_var.set(str(value))

    def set_package_mode(self, enabled: bool) -> None:
        """Show the live batch-reset button (package mode, non-catch-all only)."""
        if enabled and self.slot_number > 0:
            self._reset_row.pack(fill=tk.X)
        else:
            self._reset_row.pack_forget()

    def _reset_clicked(self) -> None:
        if self._on_reset is not None:
            self._on_reset(self.slot_number)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_card_style(hover=False)

    def _on_enter(self, _event=None) -> None:
        if not self._selected:
            self._apply_card_style(hover=True)

    def _on_leave(self, event=None) -> None:
        if self._selected:
            return
        # The pointer fires <Leave> on the parent when it crosses into a
        # child widget. Ignore those so the hover style doesn't flicker.
        if event is not None:
            x = event.x_root - self.winfo_rootx()
            y = event.y_root - self.winfo_rooty()
            if 0 <= x < self.winfo_width() and 0 <= y < self.winfo_height():
                return
        self._apply_card_style(hover=False)

    def _apply_card_style(self, *, hover: bool) -> None:
        if self._selected:
            frame_style = "CardSel.TFrame"
            title_style = "CardSelTitle.TLabel"
            muted_style = "CardSelMuted.TLabel"
            subtle_style = "CardSelSubtle.TLabel"
        elif hover:
            frame_style = "CardHover.TFrame"
            title_style = "CardHoverTitle.TLabel"
            muted_style = "CardHoverMuted.TLabel"
            subtle_style = "CardHoverSubtle.TLabel"
        else:
            frame_style = "Card.TFrame"
            title_style = "CardTitle.TLabel"
            muted_style = "CardMuted.TLabel"
            subtle_style = "CardSubtle.TLabel"

        self.configure(style=frame_style)
        self.title_label.configure(style=title_style)
        self.headstamps_label.configure(style=muted_style)
        self.count_label.configure(style=title_style)
        self.count_caption.configure(style=subtle_style)
        # The "count" row frame also needs to track the card background.
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                try:
                    child.configure(style=frame_style)
                except tk.TclError:
                    pass


# ----- Slot-details panel -----------------------------------------------------


class HeadstampCell(ttk.Frame):
    """Single checkbox + count + optional assignment hint, sized for the flow grid.

    ``sources`` are the classification labels whose run-counts sum into this
    cell's displayed count — one name for a headstamp cell, every child name
    for a parent cell.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        label: str,
        checked: bool,
        state: str,
        count: int,
        hint: str,
        sources: list[str],
        on_toggle: Callable[["tk.BooleanVar"], None],
        bold: bool = False,
    ):
        super().__init__(parent, padding=2)
        self.sources = sources
        self.var = tk.BooleanVar(value=checked)

        self.cb = ttk.Checkbutton(
            self, text=label, variable=self.var, state=state,
            style="Group.TCheckbutton" if bold else "TCheckbutton",
            command=lambda: on_toggle(self.var),
        )
        self.cb.pack(side=tk.LEFT)

        self.count_label = ttk.Label(self, text=f"({count})", style="Muted.TLabel")
        self.count_label.pack(side=tk.LEFT, padx=4)

        if hint:
            ttk.Label(self, text=hint, style="Subtle.TLabel")\
                .pack(side=tk.LEFT, padx=2)

    def update_count(self, value: int) -> None:
        self.count_label.config(text=f"({value})")


class SlotDetailsPanel(ttk.LabelFrame):
    """Shows per-headstamp checkboxes for the currently selected slot."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        config,
        on_assignment_change: Callable[[dict], None],
    ):
        super().__init__(parent, text="Slot details", padding=8)
        self.config_obj = config
        self.on_assignment_change = on_assignment_change

        self.current_slot: int | None = None

        # Header row: slot title on the left, compact filter on the right.
        # Use a tk.Label as the clear button so the row stays close to the
        # title's height — a full ttk.Button has so much padding that it
        # left a visible empty strip on the right side of the panel.
        header_row = ttk.Frame(self)
        header_row.pack(fill=tk.X, pady=(0, 2))

        self.header_var = tk.StringVar(value="Click a slot above to configure.")
        ttk.Label(
            header_row, textvariable=self.header_var, style="Header.TLabel",
        ).pack(side=tk.LEFT, anchor=tk.W)

        search_box = ttk.Frame(header_row)
        search_box.pack(side=tk.RIGHT)
        ttk.Label(search_box, text="Filter", style="Muted.TLabel")\
            .pack(side=tk.LEFT, padx=(0, 4))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search_changed())
        ttk.Entry(search_box, textvariable=self._search_var, width=20)\
            .pack(side=tk.LEFT)
        clear_btn = tk.Label(
            search_box, text="✕", cursor="hand2",
            bg=PALETTE["bg_input"], fg=PALETTE["text_muted"],
            padx=8, pady=2,
        )
        clear_btn.pack(side=tk.LEFT, padx=(2, 0))
        clear_btn.bind("<Button-1>", lambda _e: self._clear_search())
        clear_btn.bind(
            "<Enter>", lambda _e: clear_btn.config(fg=PALETTE["text"]),
        )
        clear_btn.bind(
            "<Leave>", lambda _e: clear_btn.config(fg=PALETTE["text_muted"]),
        )

        self.hint_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.hint_var, style="Muted.TLabel")\
            .pack(anchor=tk.W, pady=(0, 2))

        # Scrollable area that hosts the slot body. The body is rebuilt on
        # every show_slot — a flat flow-grid in child/parent mode, or a
        # vertical stack of collapsible parent groups in grouped mode.
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, pady=6)
        self._canvas = tk.Canvas(
            body, highlightthickness=0, bg=PALETTE["bg_surface"], borderwidth=0,
        )
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self._canvas.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.configure(yscrollcommand=scroll.set)
        self._body_inner = ttk.Frame(self._canvas)
        self._inner_id = self._canvas.create_window(
            (0, 0), window=self._body_inner, anchor=tk.NW,
        )
        self._body_inner.bind(
            "<Configure>",
            lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(self._inner_id, width=e.width),
        )

        # Per-label counters: counters[slot][label] = int (label = predicted
        # headstamp name). A parent cell sums its children's counters.
        self._counters: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # label -> cells whose displayed count includes it (live-count updates).
        self._cells_by_source: dict[str, list[HeadstampCell]] = defaultdict(list)
        # Parent names whose group is collapsed (grouped mode only).
        self._collapsed: set[str] = set()

    # ----- render -------------------------------------------------------------

    def show_slot(self, slot_num: int) -> None:
        self.current_slot = slot_num
        self.header_var.set(
            "Catch-All (cannot be configured)"
            if slot_num == 0
            else f"Slot #{slot_num}"
        )

        package_mode = bool(getattr(self.config_obj, "run_package_mode", False))
        parents = self.config_obj.parents_with_slots()
        has_parents = bool(parents)
        # Package mode uses its own many-to-many assignment store and ignores
        # parent grouping (mirrors the separate PackageMode SlotConfig list).
        parent_mode = (not package_mode) and has_parents and self.config_obj.use_parent_classifications
        grouped_mode = (not package_mode) and has_parents and not parent_mode

        if slot_num == 0:
            self.hint_var.set(
                "Anything we can't classify or that isn't mapped to a slot ends up here."
            )
        elif package_mode:
            self.hint_var.set(
                "Tick a headstamp to batch it into this slot. The same headstamp "
                "can fill several slots — they're filled one batch at a time."
            )
        elif parent_mode:
            self.hint_var.set("Tick a parent group (or an ungrouped headstamp) to route it here.")
        else:
            self.hint_var.set(
                "Tick a headstamp to route it to this slot. "
                "Headstamps assigned to a different slot are greyed out."
            )

        for child in list(self._body_inner.winfo_children()):
            child.destroy()
        self._cells_by_source.clear()

        headstamps = self.config_obj.headstamps_with_parents()
        needle = self._search_var.get().strip().casefold()
        if package_mode:
            self._build_package_mode(slot_num, headstamps, needle)
        elif parent_mode:
            self._build_parent_mode(slot_num, parents, headstamps, needle)
        elif grouped_mode:
            self._build_grouped_mode(slot_num, parents, headstamps, needle)
        else:
            self._build_child_mode(slot_num, headstamps, needle)

    def _new_grid(self, *, indent: int = 0) -> "FlowGrid":
        grid = FlowGrid(
            self._body_inner, cell_width=HEADSTAMP_CELL_WIDTH, gutter=4, expand_cells=True,
        )
        grid.pack(fill=tk.X, expand=True, padx=(indent, 0))
        return grid

    @staticmethod
    def _cell_state(slot_num: int, assigned: int) -> tuple[bool, str, str]:
        """(checked, widget-state, hint) for an item assigned to ``assigned``.

        A headstamp belongs to exactly one slot. Catch-all (slot 0) is
        read-only, and an item already assigned to another slot is disabled
        here (greyed, with an "in slot #N" hint) — it must be unassigned from
        its current slot before it can be moved.
        """
        if slot_num == 0:
            return False, "disabled", ""
        if assigned == slot_num:
            return True, "normal", ""
        if assigned != 0:
            return False, "disabled", f"in slot #{assigned}"
        return False, "normal", ""

    def _count_for(self, slot_num: int, sources: list[str]) -> int:
        counters = self._counters.get(slot_num, {})
        return sum(counters.get(s, 0) for s in sources)

    def _make_cell(
        self, grid: "FlowGrid", *, label: str, sources: list[str], slot_num: int,
        assigned: int, on_toggle: Callable[["tk.BooleanVar"], None], bold: bool = False,
    ) -> None:
        checked, state, hint = self._cell_state(slot_num, assigned)
        cell = HeadstampCell(
            grid, label=label, checked=checked, state=state,
            count=self._count_for(slot_num, sources), hint=hint,
            sources=sources, on_toggle=on_toggle, bold=bold,
        )
        grid.add(cell)
        for s in sources:
            self._cells_by_source[s].append(cell)

    def _build_child_mode(
        self, slot_num: int, headstamps: list[dict], needle: str,
    ) -> None:
        grid = self._new_grid()
        for hs in sorted(headstamps, key=lambda h: h["name"].casefold()):
            name = hs["name"]
            if needle and needle not in name.casefold():
                continue
            self._make_cell(
                grid, label=name, sources=[name], slot_num=slot_num,
                assigned=int(hs["slot"]),
                on_toggle=lambda var, n=name: self._toggle_headstamp(n, var),
            )
        grid.finish()

    def _build_package_mode(
        self, slot_num: int, headstamps: list[dict], needle: str,
    ) -> None:
        """Flat checkbox grid where a headstamp may be ticked into many slots.

        Catch-all (slot 0) is read-only here too. Nothing is greyed out — the
        whole point of package mode is one headstamp across multiple bins.
        """
        assigned = set(self.config_obj.headstamps_in_package_slot(slot_num))
        grid = self._new_grid()
        for hs in sorted(headstamps, key=lambda h: h["name"].casefold()):
            name = hs["name"]
            if needle and needle not in name.casefold():
                continue
            state = "disabled" if slot_num == 0 else "normal"
            cell = HeadstampCell(
                grid, label=name, checked=name in assigned, state=state,
                count=self._count_for(slot_num, [name]), hint="",
                sources=[name],
                on_toggle=lambda var, n=name: self._toggle_package_headstamp(n, var),
            )
            grid.add(cell)
            self._cells_by_source[name].append(cell)
        grid.finish()

    def _toggle_package_headstamp(self, name: str, var: tk.BooleanVar) -> None:
        if self.current_slot is None or self.current_slot == 0:
            return
        self.config_obj.set_package_slot_headstamp(self.current_slot, name, var.get())
        self.on_assignment_change({
            "full_refresh": False,
            "kind": "package_headstamp",
            "name": name,
            "slot": self.current_slot,
        })

    def _build_parent_mode(
        self, slot_num: int, parents: list[dict], headstamps: list[dict], needle: str,
    ) -> None:
        grid = self._new_grid()
        children_by_parent: dict[int, list[str]] = defaultdict(list)
        for h in headstamps:
            if h["parent_id"] is not None:
                children_by_parent[h["parent_id"]].append(h["name"])
        for p in sorted(parents, key=lambda x: x["name"].casefold()):
            name = p["name"]
            if needle and needle not in name.casefold():
                continue
            self._make_cell(
                grid, label=name, sources=children_by_parent.get(p["id"], []),
                slot_num=slot_num, assigned=int(p["slot"]),
                on_toggle=lambda var, pid=p["id"]: self._toggle_parent(pid, var),
            )
        for hs in sorted(
            (h for h in headstamps if h["parent_id"] is None),
            key=lambda h: h["name"].casefold(),
        ):
            name = hs["name"]
            if needle and needle not in name.casefold():
                continue
            self._make_cell(
                grid, label=name, sources=[name], slot_num=slot_num,
                assigned=int(hs["slot"]),
                on_toggle=lambda var, n=name: self._toggle_headstamp(n, var),
            )
        grid.finish()

    def _build_grouped_mode(
        self, slot_num: int, parents: list[dict], headstamps: list[dict], needle: str,
    ) -> None:
        children_by_parent: dict[int, list[dict]] = defaultdict(list)
        orphans: list[dict] = []
        for h in headstamps:
            if h["parent_id"] is None:
                orphans.append(h)
            else:
                children_by_parent[h["parent_id"]].append(h)

        for p in sorted(parents, key=lambda x: x["name"].casefold()):
            kids = sorted(children_by_parent.get(p["id"], []), key=lambda h: h["name"].casefold())
            name_match = bool(needle) and needle in p["name"].casefold()
            visible_kids = (
                kids if (not needle or name_match)
                else [k for k in kids if needle in k["name"].casefold()]
            )
            if needle and not name_match and not visible_kids:
                continue
            self._build_group_header(slot_num, p, kids)
            if p["name"] not in self._collapsed and visible_kids:
                grid = self._new_grid(indent=22)
                for k in visible_kids:
                    self._make_cell(
                        grid, label=k["name"], sources=[k["name"]], slot_num=slot_num,
                        assigned=int(k["slot"]),
                        on_toggle=lambda var, n=k["name"]: self._toggle_headstamp(n, var),
                    )
                grid.finish()

        visible_orphans = sorted(
            (o for o in orphans if not needle or needle in o["name"].casefold()),
            key=lambda h: h["name"].casefold(),
        )
        if visible_orphans:
            self._build_section_label("Other headstamps")
            grid = self._new_grid(indent=22)
            for o in visible_orphans:
                self._make_cell(
                    grid, label=o["name"], sources=[o["name"]], slot_num=slot_num,
                    assigned=int(o["slot"]),
                    on_toggle=lambda var, n=o["name"]: self._toggle_headstamp(n, var),
                )
            grid.finish()

    def _build_group_header(self, slot_num: int, parent: dict, kids: list[dict]) -> None:
        name = parent["name"]
        expanded = name not in self._collapsed
        header = ttk.Frame(self._body_inner)
        header.pack(fill=tk.X, pady=(6, 0))

        tri = tk.Label(
            header, text="▼" if expanded else "▶", width=2, cursor="hand2",
            bg=PALETTE["bg_surface"], fg=PALETTE["text_muted"],
        )
        tri.pack(side=tk.LEFT)
        tri.bind("<Button-1>", lambda _e, n=name: self._toggle_collapse(n))

        # The header checkbox only governs children that aren't locked to
        # another slot (mirrors the per-child grey-out); checked means all of
        # those assignable children are routed here.
        assignable = [k for k in kids if int(k["slot"]) in (0, slot_num)]
        all_here = bool(assignable) and all(int(k["slot"]) == slot_num for k in assignable)
        state = "normal" if (slot_num != 0 and assignable) else "disabled"
        var = tk.BooleanVar(value=all_here)
        ttk.Checkbutton(
            header, text=name, variable=var, state=state, style="Group.TCheckbutton",
            command=lambda v=var, ks=kids: self._toggle_group(ks, v),
        ).pack(side=tk.LEFT)
        count = self._count_for(slot_num, [k["name"] for k in kids])
        ttk.Label(header, text=f"({count})", style="Muted.TLabel").pack(side=tk.LEFT, padx=4)

    def _build_section_label(self, text: str) -> None:
        ttk.Separator(self._body_inner, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(8, 2))
        ttk.Label(self._body_inner, text=text, style="Subtle.TLabel")\
            .pack(anchor=tk.W, pady=(0, 2))

    # ----- interaction --------------------------------------------------------

    def _on_search_changed(self) -> None:
        """Re-render the active slot with the current search filter applied."""
        if self.current_slot is not None:
            self.show_slot(self.current_slot)

    def _clear_search(self) -> None:
        """Empty the filter entry; the trace re-renders the slot."""
        self._search_var.set("")

    def _toggle_headstamp(self, name: str, var: tk.BooleanVar) -> None:
        if self.current_slot is None or self.current_slot == 0:
            return
        new_slot = self.current_slot if var.get() else 0
        if not self.config_obj.set_headstamp_slot(name, new_slot):
            var.set(not var.get())
            return
        self.on_assignment_change({
            "full_refresh": False,
            "kind": "headstamp",
            "name": name,
            "slot": self.current_slot,
        })

    def _toggle_parent(self, parent_id: int, var: tk.BooleanVar) -> None:
        if self.current_slot is None or self.current_slot == 0:
            return
        new_slot = self.current_slot if var.get() else 0
        if not self.config_obj.set_parent_slot(parent_id, new_slot):
            var.set(not var.get())
            return
        self.on_assignment_change({
            "full_refresh": False,
            "kind": "parent",
            "slot": self.current_slot,
        })

    def _toggle_group(self, kids: list[dict], var: tk.BooleanVar) -> None:
        """Assign/clear a parent group's children for this slot.

        Children locked to a different slot are left untouched — the group
        header is not a back door around the one-slot-per-headstamp rule.
        """
        if self.current_slot is None or self.current_slot == 0:
            return
        target = self.current_slot if var.get() else 0
        for k in kids:
            assigned = int(k["slot"])
            if assigned != 0 and assigned != self.current_slot:
                continue  # locked elsewhere
            self.config_obj.set_headstamp_slot(k["name"], target)
        self.show_slot(self.current_slot)
        self.on_assignment_change({
            "full_refresh": False,
            "kind": "headstamp_group",
            "slot": self.current_slot,
        })

    def _toggle_collapse(self, name: str) -> None:
        if name in self._collapsed:
            self._collapsed.discard(name)
        else:
            self._collapsed.add(name)
        if self.current_slot is not None:
            self.show_slot(self.current_slot)

    def increment_headstamp(self, slot: int, name: str) -> None:
        self._counters[slot][name] = self._counters[slot].get(name, 0) + 1
        if slot == self.current_slot:
            for cell in self._cells_by_source.get(name, []):
                cell.update_count(self._count_for(slot, cell.sources))

    def reset_counters(self) -> None:
        self._counters.clear()
        if self.current_slot is not None:
            self.show_slot(self.current_slot)


# ----- Run tab ----------------------------------------------------------------


class RunTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, config, bus: EventBus, app):
        super().__init__(parent)
        self.config = config
        self.bus = bus
        self.app = app

        self._slot_cards: list[SlotCard] = []
        self._slot_counts: dict[int, int] = defaultdict(int)
        self._master_count = 0
        self._is_running = False
        self._store_warning_shown = False
        self._monitor_window = None
        self._saved_bins_dialog = None
        self._assignments_dirty = False

        # Community feedback-loop upload state.
        self._feedback = (
            FeedbackService(self.app.db)
            if getattr(self.app, "db", None) is not None else None
        )
        self._feedback_upload_inflight = False
        self._feedback_declined_models: set[int] = set()

        # Outer horizontal split: left = slot grid + slot details (vertical),
        # right = run controls. Initial sash is placed at 66% once the tab
        # has been laid out (PanedWindow sizes itself from child requests
        # otherwise, which doesn't honour the ratio).
        h_split = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        h_split.pack(fill=tk.BOTH, expand=True)

        left_pane = ttk.Frame(h_split)
        right_pane = ttk.Frame(h_split)
        h_split.add(left_pane, weight=66)
        h_split.add(right_pane, weight=34)
        self._h_split = h_split
        self._h_split_initialised = False
        h_split.bind("<Configure>", self._apply_initial_h_sash)

        # Left side: vertical split, slot grid on top, slot details below.
        # We continually pin the sash to the slot grid's natural height so the
        # details panel claims every remaining pixel; weights here only
        # influence behaviour while we wait for the first measurement.
        v_split = ttk.PanedWindow(left_pane, orient=tk.VERTICAL)
        v_split.pack(fill=tk.BOTH, expand=True)

        top_pane = ttk.Frame(v_split)
        bottom_pane = ttk.Frame(v_split)
        v_split.add(top_pane, weight=0)
        v_split.add(bottom_pane, weight=1)
        self._v_split = v_split
        self._top_pane = top_pane
        self._v_sash_pending = False

        grid_box = ttk.LabelFrame(top_pane, text="Slots")
        grid_box.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self.slot_grid = SlotGrid(grid_box)
        self.slot_grid.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Whenever the left side resizes the slot grid may reflow to a new
        # column count, changing its natural height. Re-pin the v sash so the
        # details panel keeps as much vertical space as possible.
        left_pane.bind("<Configure>", self._schedule_v_sash_adjust)

        # ----- Right: run controls -------------------------------------------
        controls = ttk.LabelFrame(right_pane, text="Run")
        controls.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # "Use Parent Classifications" sits above Start and is only shown when
        # the active model actually has parent groups defined.
        self._parent_opt_frame = ttk.Frame(controls)
        self._parent_opt_frame.pack(fill=tk.X, padx=12, pady=(10, 0))
        self._use_parent_var = tk.BooleanVar(value=False)
        self._parent_opt = ttk.Checkbutton(
            self._parent_opt_frame, text="Use Parent Classifications",
            variable=self._use_parent_var, command=self._on_toggle_parent_mode,
        )

        # Run options — store-images mode + confidence floor (above Start).
        run_opts = ttk.Frame(controls)
        run_opts.pack(fill=tk.X, padx=12, pady=(8, 0))

        store_row = ttk.Frame(run_opts)
        store_row.pack(fill=tk.X, pady=2)
        ttk.Label(store_row, text="Store images", style="Muted.TLabel", width=14, anchor=tk.W)\
            .pack(side=tk.LEFT)
        self._store_images_var = tk.StringVar(
            value=_STORE_IMAGES_LABELS.get(self.config.run_store_images, "None")
        )
        self._store_combo = ttk.Combobox(
            store_row, state="readonly", textvariable=self._store_images_var,
            values=list(_STORE_IMAGES_LABELS.values()),
        )
        self._store_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._store_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._on_store_images_changed()
        )

        floor_row = ttk.Frame(run_opts)
        floor_row.pack(fill=tk.X, pady=2)
        ttk.Label(floor_row, text="Confidence floor", style="Muted.TLabel", width=14, anchor=tk.W)\
            .pack(side=tk.LEFT)
        self._floor_var = tk.IntVar(value=self.config.run_confidence_floor)
        self._floor_spin = ttk.Spinbox(
            floor_row, from_=0, to=100, width=6, textvariable=self._floor_var,
            command=self._on_floor_changed,
        )
        self._floor_spin.pack(side=tk.LEFT)
        self._floor_spin.bind("<FocusOut>", lambda _e: self._on_floor_changed())
        self._floor_spin.bind("<Return>", lambda _e: self._on_floor_changed())
        ttk.Label(floor_row, text="%", style="Muted.TLabel").pack(side=tk.LEFT, padx=(4, 0))

        # Package Mode — batch the same headstamp across several slots. The
        # Batch Size row only appears while it's on (mirrors panel_run_packageMode).
        pkg_row = ttk.Frame(run_opts)
        pkg_row.pack(fill=tk.X, pady=2)
        self._package_var = tk.BooleanVar(value=self.config.run_package_mode)
        ttk.Checkbutton(
            pkg_row, text="Package Mode", variable=self._package_var,
            command=self._on_toggle_package_mode,
        ).pack(side=tk.LEFT)

        self._batch_row = ttk.Frame(run_opts)
        ttk.Label(self._batch_row, text="Batch size", style="Muted.TLabel",
                  width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._batch_var = tk.IntVar(value=self.config.run_package_size)
        self._batch_spin = ttk.Spinbox(
            self._batch_row, from_=1, to=999999, width=8, textvariable=self._batch_var,
            command=self._on_batch_size_changed,
        )
        self._batch_spin.pack(side=tk.LEFT)
        self._batch_spin.bind("<FocusOut>", lambda _e: self._on_batch_size_changed())
        self._batch_spin.bind("<Return>", lambda _e: self._on_batch_size_changed())
        if self.config.run_package_mode:
            self._batch_row.pack(fill=tk.X, pady=2)

        # Automatically Select Trays — assign an unmapped headstamp to the
        # first empty slot during a run (mirrors checkBox_autosort).
        auto_row = ttk.Frame(run_opts)
        self._auto_row = auto_row
        auto_row.pack(fill=tk.X, pady=2)
        self._auto_select_var = tk.BooleanVar(value=self.config.run_auto_select_trays)
        ttk.Checkbutton(
            auto_row, text="Automatically Select Trays",
            variable=self._auto_select_var,
            command=self._on_toggle_auto_select,
        ).pack(side=tk.LEFT)

        self.start_btn = ttk.Button(
            controls, text="Start", width=20,
            command=self._toggle_run, style="Accent.TButton",
        )
        self.start_btn.pack(padx=12, pady=(8, 4), fill=tk.X)

        # Monitor — open the live image-history window.
        ttk.Button(
            controls, text="Monitor", width=20,
            command=self._open_monitor,
        ).pack(padx=12, pady=2, fill=tk.X)

        ttk.Button(
            controls, text="Feed One", width=20,
            command=self._manual_feed,
        ).pack(padx=12, pady=2, fill=tk.X)

        ttk.Button(
            controls, text="Force feed", width=20,
            command=self._force_feed,
        ).pack(padx=12, pady=2, fill=tk.X)

        # "Upload Feedback Images" — Manual-mode community models only. Held in a
        # fixed-position frame so toggling visibility doesn't reorder controls.
        self._feedback_btn_frame = ttk.Frame(controls)
        self._feedback_btn_frame.pack(fill=tk.X)
        self._feedback_btn = ttk.Button(
            self._feedback_btn_frame, text="Upload Feedback Images", width=20,
            command=self._upload_feedback_clicked,
        )

        ttk.Separator(controls, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12, pady=8)

        master_row = ttk.Frame(controls)
        master_row.pack(fill=tk.X, padx=12)
        ttk.Label(master_row, text="Master count", style="Muted.TLabel")\
            .pack(side=tk.LEFT)
        self.master_count_var = tk.StringVar(value="0")
        ttk.Label(
            master_row, textvariable=self.master_count_var,
            style="Accent.TLabel",
        ).pack(side=tk.RIGHT, padx=6)
        ttk.Button(
            controls, text="Reset counters", width=20,
            command=self._reset_counters,
        ).pack(padx=12, pady=(4, 2), fill=tk.X)
        ttk.Button(
            controls, text="Clear Slots", width=20,
            command=self.app.confirm_clear_slots,
        ).pack(padx=12, pady=2, fill=tk.X)
        ttk.Button(
            controls, text="Load Slot Config", width=20,
            command=self._open_saved_bins,
        ).pack(padx=12, pady=(2, 8), fill=tk.X)

        ttk.Separator(controls, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12, pady=4)

        result_box = ttk.Frame(controls)
        result_box.pack(padx=12, pady=(4, 4), fill=tk.X)

        self.last_classification_var = tk.StringVar(value="—")
        self.last_parent_var = tk.StringVar(value="—")
        self.last_confidence_var = tk.StringVar(value="—")
        self.last_destination_var = tk.StringVar(value="—")

        def _result_row(caption: str, var: tk.StringVar) -> ttk.Frame:
            row = ttk.Frame(result_box)
            ttk.Label(row, text=caption, style="Muted.TLabel", width=11, anchor=tk.W)\
                .pack(side=tk.LEFT)
            ttk.Label(row, textvariable=var, style="Accent.TLabel", anchor=tk.W)\
                .pack(side=tk.LEFT, fill=tk.X, expand=True)
            return row

        # Label, [Parent], Confidence, Destination. The Parent row is inserted
        # right under Label and only while parent-classification mode is on.
        self._label_result_row = _result_row("Label", self.last_classification_var)
        self._label_result_row.pack(fill=tk.X, pady=2)
        self._parent_result_row = _result_row("Parent", self.last_parent_var)
        _result_row("Confidence", self.last_confidence_var).pack(fill=tk.X, pady=2)
        _result_row("Destination", self.last_destination_var).pack(fill=tk.X, pady=2)

        ttk.Label(controls, text="Last cropped", style="Muted.TLabel")\
            .pack(padx=12, anchor=tk.W, pady=(4, 2))
        # Maintenance preview only: show the cropped frame at half of the
        # previous display size. Capture and processing frame sizes are
        # unchanged.
        self.cropped_panel = ImagePanel(controls, width=135, height=135)
        self.cropped_panel.pack(pady=(2, 10))
        controls.bind("<Configure>", self._on_cropped_configure)

        # ----- Bottom of left pane: slot details -----------------------------
        self.details = SlotDetailsPanel(
            bottom_pane, config=config,
            on_assignment_change=self._notify_assignment_changed,
        )
        self.details.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._build_slot_cards()
        self._refresh_card_headstamps()
        self._update_parent_option_visibility()
        # Initial sash placement once the FlowGrid has a real width.
        self._schedule_v_sash_adjust()

        # Bus subscriptions ---------------------------------------------------
        bus.subscribe("run/started", lambda _p: self._set_running(True))
        bus.subscribe("run/stopped", lambda _p: self._on_run_stopped())
        bus.subscribe("run/error", self._on_run_error)
        bus.subscribe("feedback/queued", self._on_feedback_queued)
        bus.subscribe("run/status", self.app.set_status)
        bus.subscribe("run/cropped", self.cropped_panel.show_bgr)
        bus.subscribe("run/classified", self._on_classified)
        bus.subscribe("run/result", self._on_result)
        bus.subscribe("run/package_full", self._on_package_full)
        bus.subscribe("run/package_halt", self._on_package_halt)
        bus.subscribe("run/assignment_changed", self._on_assignment_changed)
        bus.subscribe("run/headstamps_synced", self._on_headstamps_synced)
        bus.subscribe("run/headstamps_sync_warning", self._on_headstamps_sync_warning)
        # Headstamps are scoped to the active model, so refresh the slot
        # cards + the current slot details panel whenever the active model
        # changes — otherwise the previous model's headstamps stay rendered.
        bus.subscribe("mode/changed", lambda _payload: self._on_active_model_changed())

        # Default selection: catch-all so the user sees something on first open.
        self._select_slot(0)
        self._update_feedback_button()

    # ----- layout -------------------------------------------------------------

    def _apply_initial_h_sash(self, event: tk.Event) -> None:
        """Snap the horizontal sash to 66/34 the first time we know our width."""
        if self._h_split_initialised:
            return
        width = event.width
        if width <= 1:
            return
        try:
            self._h_split.sashpos(0, int(width * 0.66))
        except tk.TclError:
            return
        self._h_split_initialised = True

    def _schedule_v_sash_adjust(self, _event=None) -> None:
        """Coalesce repeated events within the current Tk layout pass."""
        if not self.app._maintenance_mode:
            return
        if self._v_sash_pending:
            return
        self._v_sash_pending = True
        self.after_idle(self._adjust_v_sash)

    def _on_cropped_configure(self, event: tk.Event) -> None:
        """Resize the cropped-image panel to track the right pane's width."""
        if not self.app._maintenance_mode:
            return
        width = event.width
        if width <= 1:
            return
        # Use half of the previously available display width. This only
        # changes the Maintenance preview widget; the BGR frame delivered to
        # classification is untouched.
        size = max(80, (width - 48) // 2)
        self.cropped_panel.set_size(size, size)

    def _adjust_v_sash(self) -> None:
        """Pin the vertical sash to the slot grid's natural height.

        The slot grid only needs as many rows as required to fit its cards
        at the current column count; anything above that is wasted space.
        Measure it after Tk has finished propagating the latest resize
        (the FlowGrid reflows in its own <Configure> handler) and place the
        sash so the details panel claims the remainder.
        """
        self._v_sash_pending = False
        if not self.app._maintenance_mode:
            return
        available = self._v_split.winfo_height()
        if available <= 1:
            return
        # top_pane wraps the "Slots" LabelFrame which wraps the FlowGrid; its
        # requested height already accounts for the label chrome and padding.
        needed = self._top_pane.winfo_reqheight()
        # Always leave the details panel a usable strip even if the grid grows
        # tall enough to swallow the pane.
        min_details = 120
        max_top = max(150, available - min_details)
        sash = max(1, min(needed, max_top))
        try:
            self._v_split.sashpos(0, sash)
        except tk.TclError:
            pass

    # ----- card construction --------------------------------------------------

    def _build_slot_cards(self) -> None:
        slot_count = int(self.config.serial.get("slot_quantity", 8))
        # Slot count is the TOTAL number of physical slots including the
        # catch-all. e.g. slot_count=8 -> slots 0..7, with 0 = Catch-All.
        package = self.config.run_package_mode
        for slot_num in range(0, max(1, slot_count)):
            card = SlotCard(
                self.slot_grid, slot_number=slot_num, on_click=self._select_slot,
                on_reset=self._on_card_reset,
            )
            card.set_package_mode(package)
            self.slot_grid.add(card)
            self._slot_cards.append(card)
        self.slot_grid.finish()

    def _refresh_card_headstamps(self, slots: set[int] | None = None) -> None:
        # Build slot -> [label] mapping for slots 1..N. In parent mode the
        # labels are parent groups + ungrouped headstamps; otherwise they are
        # individual headstamps.
        slot_map: dict[int, list[str]] = defaultdict(list)
        if self.config.run_package_mode:
            for slot, names in self.config.package_slot_map().items():
                if slot > 0 and names:
                    slot_map[slot].extend(names)
            for card in self._slot_cards:
                if slots is not None and card.slot_number not in slots:
                    continue
                card.set_headstamps(
                    sorted(slot_map.get(card.slot_number, []), key=str.casefold)
                )
            return
        parents = self.config.parents_with_slots()
        if parents and self.config.use_parent_classifications:
            for p in parents:
                if int(p["slot"]) > 0:
                    slot_map[int(p["slot"])].append(p["name"])
            for h in self.config.headstamps_with_parents():
                if h["parent_id"] is None and int(h["slot"]) > 0:
                    slot_map[int(h["slot"])].append(h["name"])
        else:
            for entry in self.config.headstamps:
                name = entry.get("name")
                slot = int(entry.get("slot", 0))
                if name and slot > 0:
                    slot_map[slot].append(name)
        for card in self._slot_cards:
            if slots is not None and card.slot_number not in slots:
                continue
            card.set_headstamps(sorted(slot_map.get(card.slot_number, []), key=str.casefold))

    def _update_parent_option_visibility(self) -> None:
        """Show the parent-classification toggle only when the active model has
        parent groups; sync its checked state from the persisted preference."""
        if self.config.model_has_parents():
            self._use_parent_var.set(self.config.use_parent_classifications)
            self._parent_opt.pack(anchor=tk.W)
        else:
            self._parent_opt.pack_forget()
        self._update_parent_result_row()

    def _update_parent_result_row(self) -> None:
        """Show the 'Parent' readout row only while parent mode is active."""
        active = self.config.model_has_parents() and self.config.use_parent_classifications
        if active:
            self._parent_result_row.pack(fill=tk.X, pady=2, after=self._label_result_row)
        else:
            self._parent_result_row.pack_forget()
            self.last_parent_var.set("—")

    def _on_toggle_parent_mode(self) -> None:
        self.config.set_use_parent_classifications(self._use_parent_var.get())
        # Slot cards + details both render differently per mode.
        self._refresh_card_headstamps()
        self._update_parent_result_row()
        if self.details.current_slot is not None:
            self.details.show_slot(self.details.current_slot)
        self._notify_assignment_changed(source="maintenance_parent_mode")

    def _on_store_images_changed(self) -> None:
        mode = _STORE_IMAGES_BY_LABEL.get(self._store_images_var.get(), "none")
        self.config.set_run_store_images(mode)
        if mode != "none" and not self._store_warning_shown:
            self._store_warning_shown = True
            messagebox.showinfo(
                "Store images enabled",
                "Classified run images will be saved under the active model's "
                "run_images folder. This can use significant disk space over time.",
            )

    def _on_floor_changed(self) -> None:
        try:
            value = int(self._floor_var.get())
        except (tk.TclError, ValueError):
            value = self.config.run_confidence_floor
        value = max(0, min(100, value))
        self._floor_var.set(value)
        self.config.set_run_confidence_floor(value)

    # ----- package mode / auto-select / monitor ------------------------------

    def _on_toggle_package_mode(self) -> None:
        enabled = self._package_var.get()
        self.config.set_run_package_mode(enabled)
        if enabled:
            # Keep Batch size directly above the auto-select row.
            self._batch_row.pack(fill=tk.X, pady=2, before=self._auto_row)
        else:
            self._batch_row.pack_forget()
        for card in self._slot_cards:
            card.set_package_mode(enabled)
        # Counts and assignments are mode-specific; reset and re-render both.
        self._reset_counters()
        self._refresh_card_headstamps()
        if self.details.current_slot is not None:
            self.details.show_slot(self.details.current_slot)
        self._notify_assignment_changed(source="maintenance_package_mode")

    def _on_batch_size_changed(self) -> None:
        try:
            value = int(self._batch_var.get())
        except (tk.TclError, ValueError):
            value = self.config.run_package_size
        value = max(1, value)
        self._batch_var.set(value)
        self.config.set_run_package_size(value)

    def sync_auto_select_from_config(self) -> None:
        """Refresh the maintenance toggle from the persisted shared setting."""
        self._auto_select_var.set(bool(self.config.run_auto_select_trays))

    def sync_from_operator(self, operator) -> None:
        """Synchronize shared Run state when Maintenance becomes visible."""
        self._master_count = int(operator._count)
        self.master_count_var.set(str(self._master_count))
        self._slot_counts.clear()
        self._slot_counts.update(operator._slot_counts)
        for card in self._slot_cards:
            card.set_count(self._slot_counts[card.slot_number])
        controller = self.app.run_controller
        self._set_running(bool(controller is not None and controller.is_running))
        self.sync_auto_select_from_config()
        self._on_assignment_changed()

    def _on_toggle_auto_select(self) -> None:
        self.config.set_run_auto_select_trays(self._auto_select_var.get())
        # Keep the operator dashboard synchronized immediately.
        kiosk = getattr(self.app, "kiosk", None)
        if kiosk is not None and hasattr(kiosk, "sync_auto_select_from_config"):
            kiosk.sync_auto_select_from_config()

    def _on_card_reset(self, slot: int) -> None:
        """Live reset of one slot's batch counter (package mode)."""
        controller = self.app.run_controller
        if controller is not None and hasattr(controller, "reset_package_slot"):
            controller.reset_package_slot(slot)
        self._slot_counts[slot] = 0
        for card in self._slot_cards:
            if card.slot_number == slot:
                card.set_count(0)
        self.app.set_status(f"Reset counter for slot {slot}.")

    def _open_monitor(self) -> None:
        from .monitor import MonitorWindow
        win = self._monitor_window
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus_force()
                    return
            except tk.TclError:
                pass
        self._monitor_window = MonitorWindow(self, bus=self.bus, config=self.config)

    def _open_saved_bins(self) -> None:
        from .dialog_saved_bins import SavedBinsDialog

        dialog = self._saved_bins_dialog
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.deiconify()
                    dialog.lift()
                    dialog.focus_force()
                    return
            except tk.TclError:
                pass
        self._saved_bins_dialog = SavedBinsDialog(
            self, app=self.app, config=self.config
        )

    def _is_visible(self) -> bool:
        if not self.app._maintenance_mode:
            return False
        try:
            return self.app.notebook.select() == str(self.app._run_tab_container)
        except tk.TclError:
            return False

    def _on_assignment_changed(
        self, payload: dict | None = None, *, force: bool = False
    ) -> None:
        """Refresh only the visible routing widgets that actually changed."""
        action, slots = plan_assignment_refresh(
            payload,
            visible=self._is_visible(),
            local_source="maintenance_",
            force=force,
        )
        if action == "defer":
            self._assignments_dirty = True
            return
        if action == "skip":
            return  # the click handler already updated this visible surface

        self._assignments_dirty = False
        self._refresh_card_headstamps(slots)
        if action == "full" and self.details.current_slot is not None:
            self.details.show_slot(self.details.current_slot)

    def refresh_if_dirty(self) -> None:
        if self._assignments_dirty and self._is_visible():
            self._on_assignment_changed(force=True)

    def _notify_assignment_changed(
        self,
        change: dict | None = None,
        source: str = "maintenance_manual",
    ) -> None:
        """Publish a Maintenance routing change to every Run surface."""
        if source == "maintenance_manual":
            changed_slot = (change or {}).get("slot")
            slots = {changed_slot} if isinstance(changed_slot, int) else None
            self._refresh_card_headstamps(slots)
        post_assignment_changed(self.bus, source, change)

    def _on_package_full(self, payload: dict) -> None:
        slot = payload.get("slot")
        # Non-blocking bell tone — the batch-complete beep.
        try:
            self.app.root.bell()
        except Exception:
            pass
        self.app.set_status(f"Slot {slot} batch full ({payload.get('count')}). Reset it to refill.")

    def _on_package_halt(self, payload: dict) -> None:
        label = payload.get("label") or "headstamp"
        try:
            self.app.root.bell()
        except Exception:
            pass
        self.app.set_status(f"All slots for {label} are full — run stopped.")
        messagebox.showinfo(
            "Package run complete",
            f"Every slot configured for “{label}” has reached the batch size.\n\n"
            "The run has stopped. Empty the bins and reset their counters to continue.",
            parent=self,
        )

    def _on_active_model_changed(self) -> None:
        """Drop per-model state and re-render against the new active model.

        Slot counters belong to the previous model's run, so clear them.
        The slot cards and the slot-details panel both read config, which is
        auto-scoped to the active model, so we force them to re-render and
        re-evaluate whether the parent-classification toggle applies.
        """
        self._reset_counters()
        self.refresh_saved_bins_runtime()

    def refresh_saved_bins_runtime(self) -> None:
        """Synchronously redraw routing after a verified Saved Bins load."""
        self._package_var.set(self.config.run_package_mode)
        if self.config.run_package_mode:
            self._batch_row.pack(fill=tk.X, pady=2, before=self._auto_row)
        else:
            self._batch_row.pack_forget()
        for card in self._slot_cards:
            card.set_package_mode(self.config.run_package_mode)
        self._update_parent_option_visibility()
        self._refresh_card_headstamps()
        if self.details.current_slot is not None:
            self.details.show_slot(self.details.current_slot)
        self._update_feedback_button()

    # ----- community feedback loop -------------------------------------------

    def _active_feedback_model(self):
        """Return the active model iff it's a feedback-enabled community model."""
        if self._feedback is None:
            return None
        mid = self.config.settings.get_active_model_id()
        if mid is None:
            return None
        model = ModelRepo(self.app.db).get(mid)
        return model if is_feedback_model(model) else None

    def _update_feedback_button(self) -> None:
        """Show the manual upload button only for Manual-mode models with a
        non-empty queue. Instant/OnRunComplete upload automatically."""
        model = self._active_feedback_model()
        show = (
            model is not None
            and model.feedback_loop_upload_mode == "Manual"
            and self._feedback.has_pending(model.id)
        )
        if show:
            self._feedback_btn.pack(padx=12, pady=2, fill=tk.X)
        else:
            self._feedback_btn.pack_forget()

    def _on_feedback_queued(self, payload: dict) -> None:
        """A below-threshold image was staged during a run. Upload now in
        Instant mode; otherwise just refresh the manual button."""
        # This constructed Run tab is the automatic-upload owner even when LAN
        # Access is also running. WindowsWebOperations explicitly defers while
        # app.run_tab exists, so Desktop + Web presentation still has exactly
        # one queue drainer. In browser-only operation this tab is never
        # constructed and the web adapter owns the same event instead.
        model_id = payload.get("model_id")
        mode = payload.get("upload_mode")
        declined = model_id in self._feedback_declined_models
        debug_log(f"tab_run: feedback/queued received model_id={model_id} mode={mode} declined={declined}")
        if mode == "Instant" and model_id is not None and not declined:
            self._trigger_feedback_drain(model_id)
        else:
            debug_log("tab_run: not auto-draining (mode!=Instant or declined) — manual/OnRunComplete path")
            self._update_feedback_button()

    def _on_run_stopped(self) -> None:
        self._set_running(False)
        model = self._active_feedback_model()
        if model is not None:
            debug_log(
                f"tab_run: run stopped; active feedback model={model.id} "
                f"mode={model.feedback_loop_upload_mode} "
                f"pending={self._feedback.count_pending(model.id)}"
            )
        if model is not None and model.feedback_loop_upload_mode == "OnRunComplete" \
                and model.id not in self._feedback_declined_models:
            debug_log("tab_run: OnRunComplete — draining queue at run stop")
            self._trigger_feedback_drain(model.id)
        self._update_feedback_button()

    def _upload_feedback_clicked(self) -> None:
        model = self._active_feedback_model()
        if model is None:
            return
        self.app.set_status("Uploading feedback images…")
        self._trigger_feedback_drain(model.id)

    def _trigger_feedback_drain(self, model_id: int) -> None:
        """Drain a model's feedback queue on a worker thread (it holds the
        network call + auth). Coalesced so drains never overlap."""
        if self._feedback is None or self._feedback_upload_inflight:
            debug_log(
                f"tab_run: drain skipped (service={self._feedback is not None}, "
                f"inflight={self._feedback_upload_inflight})"
            )
            return
        debug_log(f"tab_run: starting drain worker for model {model_id} (auth={self.app.auth is not None})")
        self._feedback_upload_inflight = True
        self.app.run_worker(
            lambda: self._feedback.upload_pending(model_id, auth=self.app.auth),
            on_done=lambda res, mid=model_id: self._on_feedback_done(mid, res),
            on_error=lambda _exc: self._on_feedback_error(),
        )

    def _on_feedback_done(self, model_id: int, result: dict) -> None:
        self._feedback_upload_inflight = False
        debug_log(f"tab_run: drain done for model {model_id}: {result}")
        if result.get("declined"):
            self._feedback_declined_models.add(model_id)
            self.app.set_status("Feedback uploads paused by the server.")
        elif result.get("uploaded"):
            self.app.set_status(f"Uploaded {result['uploaded']} feedback image(s).")
        # New captures may have arrived mid-drain; keep draining in Instant mode.
        model = self._active_feedback_model()
        if (
            model is not None
            and model.id == model_id
            and model.feedback_loop_upload_mode == "Instant"
            and model_id not in self._feedback_declined_models
            and self._feedback.has_pending(model_id)
        ):
            self._trigger_feedback_drain(model_id)
        self._update_feedback_button()

    def _on_feedback_error(self) -> None:
        debug_log("tab_run: drain worker raised (see traceback above)")
        self._feedback_upload_inflight = False
        self._update_feedback_button()

    # ----- selection ---------------------------------------------------------

    def _select_slot(self, slot_num: int) -> None:
        for card in self._slot_cards:
            card.set_selected(card.slot_number == slot_num)
        self.details.show_slot(slot_num)

    # ----- run actions --------------------------------------------------------

    def _toggle_run(self) -> None:
        self.app.toggle_run()

    def _manual_feed(self) -> None:
        """Run one full classify+sort cycle (same flow as a continuous run)."""
        self.app.manual_feed()

    def _force_feed(self) -> None:
        """Send xf:0 directly — feed a case without capturing or classifying."""
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            messagebox.showerror("Not connected", "Connect to the board first.")
            return
        self.app.run_worker(broker.feed_one)

    def _reset_counters(self) -> None:
        self.app.reset_run_counters()

    def reset_counter_display(self) -> None:
        """Clear Maintenance counter widgets without emitting another reset."""
        self._master_count = 0
        self.master_count_var.set("0")
        self._slot_counts.clear()
        for card in self._slot_cards:
            card.set_count(0)
        self.details.reset_counters()

    # ----- state -------------------------------------------------------------

    def _set_running(self, running: bool) -> None:
        self._is_running = running
        if running:
            self.start_btn.config(text="Stop", style="Warning.TButton")
        else:
            self.start_btn.config(text="Start", style="Accent.TButton")

    # ----- bus event handlers -------------------------------------------------

    def _on_headstamps_sync_warning(self, error: str) -> None:
        self.app.set_status(f"Could not refresh remote labels; using saved list: {error}")

    def _on_headstamps_synced(self, payload: dict) -> None:
        """Refresh selectors after remote classes are imported automatically."""
        self._refresh_card_headstamps()
        if self.details.current_slot is not None:
            self.details.show_slot(self.details.current_slot)
        received = int((payload or {}).get("received", 0))
        added = int((payload or {}).get("added", 0))
        self.app.set_status(
            f"Remote labels synchronized: {received} received, {added} new."
        )

    def _on_classified(self, payload: dict) -> None:
        label = payload.get("label") or "(empty)"
        confidence = float(payload.get("confidence", 0) or 0)
        slot = payload.get("slot")
        slot_text = "Catch-All" if slot == 0 else f"Slot #{slot}"
        self.last_classification_var.set(label)
        self.last_parent_var.set(payload.get("parent") or "—")
        self.last_confidence_var.set(f"{confidence:.2f}%")
        self.last_destination_var.set(slot_text)

    def _on_result(self, result: dict) -> None:
        if not result.get("ok"):
            return
        slot = int(result.get("slot") or 0)
        label = result.get("label", "")
        # Update master + per-slot counters.
        self._master_count += 1
        self.master_count_var.set(str(self._master_count))
        self._slot_counts[slot] += 1
        for card in self._slot_cards:
            if card.slot_number == slot:
                card.set_count(self._slot_counts[slot])
        if label:
            self.details.increment_headstamp(slot, label)

    def _on_run_error(self, msg: str) -> None:
        self.app.set_status(f"Run error: {msg}")
        self._set_running(False)
