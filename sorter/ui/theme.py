"""Modern dark theme for the OSS Client UI.

Centralises colors, fonts, and ttk styles so the rest of the app can stay
focused on layout. Inspired by the Tokyo Night / VS Code Dark+ palettes —
flat surfaces with a single blue accent, high-contrast text, and a small
vertical color hierarchy that gives the UI a sense of depth without
relying on bevels or relief.

Two helpers are exported:

* ``apply_theme(root)`` — call once after the root is created. Applies
  ttk styles, sets default fonts on the legacy Tk widgets (Listbox / Text
  / Canvas), and returns the resolved fonts dict for callers that need to
  match.
* ``paint_gradient(canvas, ...)`` — paint a vertical/horizontal linear
  gradient across the canvas. Used for the title bar.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk


# Color palette — single source of truth.
PALETTE = {
    "bg_window":     "#121417",
    "bg_gradient_a": "#1C1F24",
    "bg_gradient_b": "#121417",
    "bg_surface":    "#1C1F24",
    "bg_card":       "#252932",
    "bg_card_hover": "#2D323C",
    "bg_card_sel":   "#343A46",
    "bg_input":      "#121417",
    "border":        "#343A46",
    "border_focus":  "#22D3EE",
    "text":          "#F2F4F8",
    "text_muted":    "#A8AFBD",
    "text_subtle":   "#737B8C",
    "text_inverse":  "#121417",
    "accent":        "#3B82F6",
    "accent_hover":  "#60A5FA",
    "accent_press":  "#2563EB",
    "accent_dim":    "#26334A",
    "cyan":          "#22D3EE",
    "success":       "#22C55E",
    "success_dim":   "#173524",
    "warning":       "#F59E0B",
    "error":         "#EF4444",
}


def _pick_font(
    root: tk.Misc, candidates: list[str], fallback: str
) -> str:
    """Return the first font in candidates that's available on the host."""
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        return fallback
    for name in candidates:
        if name in available:
            return name
    return fallback


def get_fonts(root: tk.Misc) -> dict[str, tuple]:
    """Pick a modern UI font and matching monospace font for the host."""
    family = _pick_font(
        root,
        [
            "Inter", "SF Pro Text", "Segoe UI", "Helvetica Neue",
            "Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans",
        ],
        fallback="TkDefaultFont",
    )
    mono = _pick_font(
        root,
        [
            "JetBrains Mono", "Cascadia Code", "Fira Code",
            "Consolas", "Menlo", "DejaVu Sans Mono",
        ],
        fallback="TkFixedFont",
    )
    return {
        "default":      (family, 10),
        "body":         (family, 10),
        "bold":         (family, 10, "bold"),
        "header":       (family, 12, "bold"),
        "title":        (family, 18, "bold"),
        "selected_tab": (family, 11, "bold"),
        "small":        (family, 9),
        "mono":         (mono, 10),
    }


def apply_theme(root: tk.Tk) -> dict[str, tuple]:
    """Apply the modern dark theme to the given root. Returns the fonts dict."""
    fonts = get_fonts(root)

    root.configure(bg=PALETTE["bg_window"])

    # Apply a native Tk palette as well as ttk styles. Raspberry Pi OS can
    # re-inject desktop theme colors after Tk starts; tk_setPalette updates
    # the actual widget database rather than relying only on ttk inheritance.
    root.tk_setPalette(
        background=PALETTE["bg_surface"],
        foreground=PALETTE["text"],
        activeBackground=PALETTE["bg_card_hover"],
        activeForeground=PALETTE["text"],
        selectColor=PALETTE["accent_press"],
        selectBackground=PALETTE["accent_press"],
        selectForeground=PALETTE["text"],
        highlightColor=PALETTE["border_focus"],
    )
    root.configure(bg=PALETTE["bg_window"])

    # Force a dark option database before any widgets are created. Raspberry Pi
    # desktop themes can otherwise supply light native defaults for legacy Tk
    # widgets even while ttk is using the dark ``clam`` theme.
    for pattern, value in (
        ("*Background", PALETTE["bg_surface"]),
        ("*background", PALETTE["bg_surface"]),
        ("*Foreground", PALETTE["text"]),
        ("*foreground", PALETTE["text"]),
        ("*activeBackground", PALETTE["bg_card_hover"]),
        ("*activeForeground", PALETTE["text"]),
        ("*selectBackground", PALETTE["accent_press"]),
        ("*selectForeground", PALETTE["text"]),
        ("*insertBackground", PALETTE["text"]),
        ("*highlightBackground", PALETTE["border"]),
        ("*highlightColor", PALETTE["border_focus"]),
        ("*troughColor", PALETTE["bg_input"]),
        ("*borderWidth", 0),
    ):
        root.option_add(pattern, value)

    root.option_add("*Canvas.background", PALETTE["bg_surface"])
    root.option_add("*Canvas.highlightThickness", 0)
    root.option_add("*Entry.background", PALETTE["bg_input"])
    root.option_add("*Entry.foreground", PALETTE["text"])
    root.option_add("*Entry.insertBackground", PALETTE["text"])
    root.option_add("*Spinbox.background", PALETTE["bg_input"])
    root.option_add("*Spinbox.foreground", PALETTE["text"])
    root.option_add("*Menu.background", PALETTE["bg_card"])
    root.option_add("*Menu.foreground", PALETTE["text"])
    root.option_add("*Menu.activeBackground", PALETTE["accent_press"])
    root.option_add("*Menu.activeForeground", PALETTE["text"])

    # Legacy Tk widgets (Listbox, Text, Canvas) don't pick up ttk styles —
    # set their defaults via the option database.
    root.option_add("*Listbox.background", PALETTE["bg_input"])
    root.option_add("*Listbox.foreground", PALETTE["text"])
    root.option_add("*Listbox.selectBackground", PALETTE["accent"])
    root.option_add("*Listbox.selectForeground", PALETTE["text_inverse"])
    root.option_add("*Listbox.borderWidth", 0)
    root.option_add("*Listbox.highlightThickness", 0)
    root.option_add("*Listbox.font", fonts["body"])
    root.option_add("*Listbox.activeStyle", "none")

    root.option_add("*Text.background", PALETTE["bg_input"])
    root.option_add("*Text.foreground", PALETTE["text"])
    root.option_add("*Text.insertBackground", PALETTE["accent"])
    root.option_add("*Text.selectBackground", PALETTE["accent_dim"])
    root.option_add("*Text.selectForeground", PALETTE["text"])
    root.option_add("*Text.borderWidth", 0)
    root.option_add("*Text.highlightThickness", 1)
    root.option_add("*Text.highlightBackground", PALETTE["border"])
    root.option_add("*Text.highlightColor", PALETTE["border_focus"])
    root.option_add("*Text.font", fonts["body"])

    style = ttk.Style(root)
    style.theme_use("clam")

    bg = PALETTE["bg_surface"]
    text = PALETTE["text"]
    accent = PALETTE["accent"]

    # ----- Frames ------------------------------------------------------------
    style.configure("TFrame", background=bg)
    style.configure("Window.TFrame", background=PALETTE["bg_window"])
    style.configure("Card.TFrame", background=PALETTE["bg_card"])
    style.configure("CardHover.TFrame", background=PALETTE["bg_card_hover"])
    style.configure("CardSel.TFrame", background=PALETTE["bg_card_sel"])
    style.configure("StatusBar.TFrame", background=PALETTE["bg_window"])

    # ----- Labels ------------------------------------------------------------
    style.configure(
        "TLabel",
        background=bg,
        foreground=text,
        font=fonts["body"],
    )
    style.configure(
        "Title.TLabel",
        background=PALETTE["bg_window"],
        foreground=PALETTE["text"],
        font=fonts["title"],
    )
    style.configure(
        "Subtitle.TLabel",
        background=PALETTE["bg_window"],
        foreground=PALETTE["text_muted"],
        font=fonts["small"],
    )
    style.configure(
        "Muted.TLabel",
        background=bg,
        foreground=PALETTE["text_muted"],
        font=fonts["body"],
    )
    style.configure(
        "Subtle.TLabel",
        background=bg,
        foreground=PALETTE["text_subtle"],
        font=fonts["small"],
    )
    style.configure(
        "Status.TLabel",
        background=PALETTE["bg_window"],
        foreground=PALETTE["text_muted"],
        font=fonts["small"],
    )
    style.configure(
        "Header.TLabel",
        background=bg,
        foreground=text,
        font=fonts["header"],
    )
    style.configure(
        "Accent.TLabel",
        background=bg,
        foreground=accent,
        font=fonts["bold"],
    )
    # Card-context labels.
    style.configure(
        "Card.TLabel",
        background=PALETTE["bg_card"],
        foreground=text,
        font=fonts["body"],
    )
    style.configure(
        "CardTitle.TLabel",
        background=PALETTE["bg_card"],
        foreground=text,
        font=fonts["bold"],
    )
    style.configure(
        "CardMuted.TLabel",
        background=PALETTE["bg_card"],
        foreground=PALETTE["text_muted"],
        font=fonts["body"],
    )
    style.configure(
        "CardSubtle.TLabel",
        background=PALETTE["bg_card"],
        foreground=PALETTE["text_subtle"],
        font=fonts["small"],
    )
    style.configure(
        "CardSel.TLabel",
        background=PALETTE["bg_card_sel"],
        foreground=text,
        font=fonts["body"],
    )
    style.configure(
        "CardSelTitle.TLabel",
        background=PALETTE["bg_card_sel"],
        foreground=accent,
        font=fonts["bold"],
    )
    style.configure(
        "CardSelMuted.TLabel",
        background=PALETTE["bg_card_sel"],
        foreground=PALETTE["text_muted"],
        font=fonts["body"],
    )
    style.configure(
        "CardSelSubtle.TLabel",
        background=PALETTE["bg_card_sel"],
        foreground=PALETTE["text_muted"],
        font=fonts["small"],
    )
    # Hover variant — used by slot cards on mouse-over.
    style.configure(
        "CardHoverTitle.TLabel",
        background=PALETTE["bg_card_hover"],
        foreground=text,
        font=fonts["bold"],
    )
    style.configure(
        "CardHoverMuted.TLabel",
        background=PALETTE["bg_card_hover"],
        foreground=PALETTE["text_muted"],
        font=fonts["body"],
    )
    style.configure(
        "CardHoverSubtle.TLabel",
        background=PALETTE["bg_card_hover"],
        foreground=PALETTE["text_subtle"],
        font=fonts["small"],
    )

    # ----- LabelFrame --------------------------------------------------------
    # Borderless — the colored label and bg-surface contrast against the
    # window already separate sections visually, no outline needed.
    style.configure(
        "TLabelframe",
        background=bg,
        bordercolor=bg,
        darkcolor=bg,
        lightcolor=bg,
        borderwidth=0,
        relief="flat",
    )
    style.configure(
        "TLabelframe.Label",
        background=bg,
        foreground=accent,
        font=fonts["bold"],
        padding=(4, 0),
    )

    # ----- Notebook ----------------------------------------------------------
    # Drop the focus ring (Notebook.focus) so we don't get a dotted box
    # around the active tab's text.
    style.layout("TNotebook.Tab", [
        ("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""}),
            ]}),
        ]}),
    ])
    style.configure(
        "TNotebook",
        background=PALETTE["bg_window"],
        bordercolor=PALETTE["bg_window"],
        darkcolor=PALETTE["bg_window"],
        lightcolor=PALETTE["bg_window"],
        borderwidth=0,
        tabmargins=(12, 8, 12, 0),
    )
    style.configure(
        "TNotebook.Tab",
        background=PALETTE["bg_card"],
        foreground=PALETTE["text_muted"],
        bordercolor=PALETTE["bg_card"],
        darkcolor=PALETTE["bg_card"],
        lightcolor=PALETTE["bg_card"],
        padding=(16, 8),
        borderwidth=0,
        font=fonts["bold"],
    )
    # Selected tab is visually elevated: extra padding, blue-tinted bg,
    # bumped font. Hovered tab gets a subtle lift.
    style.map(
        "TNotebook.Tab",
        background=[
            ("selected", PALETTE["bg_card_sel"]),
            ("active", PALETTE["bg_card_hover"]),
        ],
        foreground=[
            ("selected", PALETTE["text"]),
            ("active", PALETTE["text"]),
        ],
        bordercolor=[
            ("selected", PALETTE["bg_card_sel"]),
            ("active", PALETTE["bg_card_hover"]),
        ],
        lightcolor=[
            ("selected", PALETTE["bg_card_sel"]),
            ("active", PALETTE["bg_card_hover"]),
        ],
        darkcolor=[
            ("selected", PALETTE["bg_card_sel"]),
            ("active", PALETTE["bg_card_hover"]),
        ],
        padding=[
            ("selected", (22, 12)),
        ],
        font=[
            ("selected", fonts["selected_tab"]),
        ],
        expand=[
            ("selected", (1, 1, 1, 0)),
        ],
    )

    # ----- Buttons -----------------------------------------------------------
    style.configure(
        "TButton",
        background=PALETTE["accent_dim"],
        foreground=text,
        bordercolor=PALETTE["accent_dim"],
        darkcolor=PALETTE["accent_dim"],
        lightcolor=PALETTE["accent_dim"],
        focuscolor=PALETTE["accent"],
        borderwidth=0,
        relief="flat",
        padding=(14, 7),
        font=fonts["body"],
    )
    style.map(
        "TButton",
        background=[
            ("pressed", PALETTE["accent_press"]),
            ("active", PALETTE["accent"]),
            ("disabled", PALETTE["bg_surface"]),
        ],
        foreground=[
            ("disabled", PALETTE["text_subtle"]),
            ("active", PALETTE["text_inverse"]),
            ("pressed", PALETTE["text_inverse"]),
        ],
        bordercolor=[
            ("active", PALETTE["accent"]),
            ("pressed", PALETTE["accent_press"]),
        ],
        darkcolor=[
            ("active", PALETTE["accent"]),
            ("pressed", PALETTE["accent_press"]),
        ],
        lightcolor=[
            ("active", PALETTE["accent"]),
            ("pressed", PALETTE["accent_press"]),
        ],
    )

    # Primary / accent button.
    style.configure(
        "Accent.TButton",
        background=accent,
        foreground=PALETTE["text_inverse"],
        bordercolor=accent,
        darkcolor=accent,
        lightcolor=accent,
        font=fonts["bold"],
    )
    style.map(
        "Accent.TButton",
        background=[
            ("pressed", PALETTE["accent_press"]),
            ("active", PALETTE["accent_hover"]),
            ("disabled", PALETTE["bg_surface"]),
        ],
        foreground=[
            ("disabled", PALETTE["text_subtle"]),
        ],
        bordercolor=[
            ("active", PALETTE["accent_hover"]),
            ("pressed", PALETTE["accent_press"]),
        ],
        darkcolor=[
            ("active", PALETTE["accent_hover"]),
            ("pressed", PALETTE["accent_press"]),
        ],
        lightcolor=[
            ("active", PALETTE["accent_hover"]),
            ("pressed", PALETTE["accent_press"]),
        ],
    )

    # Stop / danger button — used while a run is active.
    style.configure(
        "Warning.TButton",
        background=PALETTE["warning"],
        foreground=PALETTE["text_inverse"],
        font=fonts["bold"],
        borderwidth=0,
        padding=(12, 7),
    )
    style.map(
        "Warning.TButton",
        background=[("active", "#fbbf24"), ("pressed", PALETTE["warning"])],
    )

    style.configure(
        "Danger.TButton",
        background=PALETTE["error"],
        foreground=PALETTE["text_inverse"],
        bordercolor=PALETTE["error"],
        darkcolor=PALETTE["error"],
        lightcolor=PALETTE["error"],
        font=fonts["bold"],
    )
    style.map(
        "Danger.TButton",
        background=[
            ("pressed", "#d04258"),
            ("active", "#ff8aa0"),
        ],
        bordercolor=[
            ("active", "#ff8aa0"),
            ("pressed", "#d04258"),
        ],
        darkcolor=[
            ("active", "#ff8aa0"),
            ("pressed", "#d04258"),
        ],
        lightcolor=[
            ("active", "#ff8aa0"),
            ("pressed", "#d04258"),
        ],
    )

    # ----- Entries / Spinboxes / Combobox -----------------------------------
    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(
            widget,
            fieldbackground=PALETTE["bg_input"],
            background=PALETTE["bg_input"],
            foreground=text,
            bordercolor=PALETTE["border"],
            darkcolor=PALETTE["border"],
            lightcolor=PALETTE["border"],
            insertcolor=accent,
            arrowcolor=PALETTE["text_muted"],
            padding=4,
        )
        style.map(
            widget,
            fieldbackground=[
                ("readonly", PALETTE["bg_input"]),
                ("disabled", PALETTE["bg_surface"]),
            ],
            foreground=[
                ("disabled", PALETTE["text_subtle"]),
            ],
            bordercolor=[("focus", PALETTE["border_focus"])],
            lightcolor=[("focus", PALETTE["border_focus"])],
            darkcolor=[("focus", PALETTE["border_focus"])],
            arrowcolor=[("active", accent)],
        )

    # Combobox dropdown list (uses the option database).
    root.option_add("*TCombobox*Listbox.background", PALETTE["bg_card"])
    root.option_add("*TCombobox*Listbox.foreground", text)
    root.option_add("*TCombobox*Listbox.selectBackground", PALETTE["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", PALETTE["text_inverse"])
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*TCombobox*Listbox.font", fonts["body"])

    # ----- Checkbutton / Radiobutton ----------------------------------------
    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(
            widget,
            background=bg,
            foreground=text,
            focuscolor=accent,
            indicatorbackground=PALETTE["bg_input"],
            indicatorforeground=accent,
            indicatordiameter=12,
            font=fonts["body"],
            padding=2,
        )
        style.map(
            widget,
            background=[("active", bg)],
            indicatorbackground=[
                ("selected", accent),
                ("disabled", PALETTE["bg_surface"]),
            ],
            indicatorforeground=[
                ("selected", PALETTE["text_inverse"]),
            ],
            foreground=[
                ("disabled", PALETTE["text_subtle"]),
            ],
        )
    # Bold checkbutton — parent-group headers in the slot-details panel.
    style.configure(
        "Group.TCheckbutton",
        background=bg,
        foreground=text,
        focuscolor=accent,
        indicatorbackground=PALETTE["bg_input"],
        indicatorforeground=accent,
        indicatordiameter=12,
        font=fonts["bold"],
        padding=2,
    )
    style.map(
        "Group.TCheckbutton",
        background=[("active", bg)],
        indicatorbackground=[
            ("selected", accent),
            ("disabled", PALETTE["bg_surface"]),
        ],
        indicatorforeground=[("selected", PALETTE["text_inverse"])],
        foreground=[("disabled", PALETTE["text_subtle"])],
    )

    # Card-context check/radio that need to sit on PALETTE["bg_card"].
    for widget in ("Card.TCheckbutton", "Card.TRadiobutton"):
        style.configure(
            widget,
            background=PALETTE["bg_card"],
            foreground=text,
            focuscolor=accent,
            indicatorbackground=PALETTE["bg_input"],
            indicatorforeground=accent,
            font=fonts["body"],
        )
        style.map(
            widget,
            background=[("active", PALETTE["bg_card"])],
            indicatorbackground=[("selected", accent)],
            foreground=[("disabled", PALETTE["text_subtle"])],
        )

    # ----- Scale (slider) ---------------------------------------------------
    style.configure(
        "Horizontal.TScale",
        background=bg,
        troughcolor=PALETTE["bg_input"],
        sliderlength=18,
        bordercolor=PALETTE["border"],
        darkcolor=accent,
        lightcolor=accent,
        gripcount=0,
    )
    style.map(
        "Horizontal.TScale",
        background=[("active", bg)],
        troughcolor=[("active", PALETTE["bg_input"])],
    )

    # ----- Scrollbar --------------------------------------------------------
    for orient in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(
            orient,
            background=PALETTE["bg_card"],
            troughcolor=PALETTE["bg_window"],
            bordercolor=PALETTE["bg_window"],
            darkcolor=PALETTE["bg_card"],
            lightcolor=PALETTE["bg_card"],
            arrowcolor=PALETTE["text_muted"],
            borderwidth=0,
            relief="flat",
        )
        style.map(
            orient,
            background=[
                ("active", PALETTE["accent_dim"]),
                ("pressed", PALETTE["accent"]),
            ],
            arrowcolor=[("active", PALETTE["text"])],
        )

    # ----- Treeview ---------------------------------------------------------
    # Without an explicit style the clam Treeview renders light-grey, which
    # reads as a foreign element on the dark surface. Match the input fields.
    style.configure(
        "Treeview",
        background=PALETTE["bg_input"],
        fieldbackground=PALETTE["bg_input"],
        foreground=text,
        bordercolor=PALETTE["border"],
        darkcolor=PALETTE["bg_input"],
        lightcolor=PALETTE["bg_input"],
        borderwidth=0,
        relief="flat",
        rowheight=26,
        font=fonts["body"],
    )
    style.map(
        "Treeview",
        background=[("selected", PALETTE["accent"])],
        foreground=[("selected", PALETTE["text_inverse"])],
    )
    style.configure(
        "Treeview.Heading",
        background=PALETTE["bg_card"],
        foreground=PALETTE["text"],
        bordercolor=PALETTE["border"],
        darkcolor=PALETTE["bg_card"],
        lightcolor=PALETTE["bg_card"],
        relief="flat",
        padding=(8, 6),
        font=fonts["bold"],
    )
    style.map(
        "Treeview.Heading",
        background=[
            ("active", PALETTE["bg_card_hover"]),
            ("pressed", PALETTE["bg_card_sel"]),
        ],
        foreground=[("active", PALETTE["text"])],
    )

    # ----- Separator --------------------------------------------------------
    style.configure("TSeparator", background=PALETTE["border"])

    # ----- PanedWindow ------------------------------------------------------
    style.configure(
        "TPanedwindow",
        background=PALETTE["bg_window"],
    )
    style.configure(
        "Sash",
        background=PALETTE["bg_window"],
        bordercolor=PALETTE["bg_window"],
        gripcount=0,
        sashthickness=6,
    )

    # ----- Kiosk operator surface ------------------------------------------
    style.configure("Kiosk.TFrame", background=PALETTE["bg_window"])
    style.configure("KioskCard.TFrame", background=PALETTE["bg_surface"])
    style.configure("KioskTitle.TLabel", background=PALETTE["bg_window"], foreground=PALETTE["text"], font=(fonts["title"][0], 26, "bold"))
    style.configure("KioskState.TLabel", background=PALETTE["bg_window"], foreground=PALETTE["accent"], font=(fonts["title"][0], 16, "bold"))
    style.configure("KioskCaption.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text_muted"], font=(fonts["body"][0], 11, "bold"))
    style.configure("KioskBody.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text"], font=(fonts["body"][0], 14))
    style.configure("KioskMuted.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text_muted"], font=(fonts["body"][0], 12))
    style.configure("KioskResult.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text"], font=(fonts["title"][0], 38, "bold"))
    style.configure("KioskDestination.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["accent"], font=(fonts["title"][0], 25, "bold"))
    style.configure("KioskMetric.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text"], font=(fonts["title"][0], 27, "bold"))
    style.configure("KioskPrimary.TButton", background=PALETTE["accent_press"], foreground=PALETTE["text"], font=(fonts["body"][0], 15, "bold"), borderwidth=0)
    style.map("KioskPrimary.TButton", background=[("active", PALETTE["accent"]), ("pressed", PALETTE["accent_press"])])
    style.configure("KioskStop.TButton", background=PALETTE["warning"], foreground=PALETTE["text_inverse"], font=(fonts["body"][0], 15, "bold"), borderwidth=0)
    style.map("KioskStop.TButton", background=[("active", "#fbbf24"), ("pressed", PALETTE["warning"])])
    style.configure("KioskSecondary.TButton", background=PALETTE["bg_card"], foreground=PALETTE["text"], font=(fonts["body"][0], 13, "bold"), borderwidth=0)
    style.map("KioskSecondary.TButton", background=[("active", PALETTE["bg_card_hover"])])
    style.configure("Kiosk.TCheckbutton", background=PALETTE["bg_surface"], foreground=PALETTE["text"], font=(fonts["body"][0], 13, "bold"), padding=(4, 6))
    style.map("Kiosk.TCheckbutton", background=[("active", PALETTE["bg_surface"])], foreground=[("disabled", PALETTE["text_subtle"]), ("active", PALETTE["text"])])
    style.configure("KioskGhost.TButton", background=PALETTE["bg_window"], foreground=PALETTE["text_muted"], font=(fonts["body"][0], 11), borderwidth=0)
    style.map("KioskGhost.TButton", foreground=[("active", PALETTE["text"])])

    return fonts


def reassert_dark_runtime(root: tk.Tk) -> None:
    """Reapply dark colors after Openbox/native theme mapping."""
    root.tk_setPalette(
        background=PALETTE["bg_surface"],
        foreground=PALETTE["text"],
        activeBackground=PALETTE["bg_card_hover"],
        activeForeground=PALETTE["text"],
        selectColor=PALETTE["accent_press"],
        selectBackground=PALETTE["accent_press"],
        selectForeground=PALETTE["text"],
        highlightColor=PALETTE["border_focus"],
    )
    root.configure(bg=PALETTE["bg_window"])
    # apply_theme() selects the ttk theme once during startup. Runtime
    # reassertion only refreshes style values; changing the active theme here
    # can emit <<ThemeChanged>> and create a callback loop.
    style = ttk.Style(root)
    style.configure("TFrame", background=PALETTE["bg_surface"])
    style.configure("Window.TFrame", background=PALETTE["bg_window"])
    style.configure("Kiosk.TFrame", background=PALETTE["bg_window"])
    style.configure("KioskCard.TFrame", background=PALETTE["bg_surface"])
    style.configure("TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text"])
    style.configure("KioskTitle.TLabel", background=PALETTE["bg_window"], foreground=PALETTE["text"])
    style.configure("KioskState.TLabel", background=PALETTE["bg_window"], foreground=PALETTE["accent"])
    style.configure("KioskCaption.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text_muted"])
    style.configure("KioskBody.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text"])
    style.configure("KioskMuted.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text_muted"])
    style.configure("KioskResult.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["text"])
    style.configure("KioskDestination.TLabel", background=PALETTE["bg_surface"], foreground=PALETTE["accent"])
    style.configure("KioskPrimary.TButton", background=PALETTE["accent_press"], foreground=PALETTE["text"])
    style.configure("KioskSecondary.TButton", background=PALETTE["bg_card"], foreground=PALETTE["text"])
    style.configure("KioskStop.TButton", background=PALETTE["warning"], foreground=PALETTE["text_inverse"])
    style.configure("Kiosk.TCheckbutton", background=PALETTE["bg_surface"], foreground=PALETTE["text"])
    enforce_dark_widget_tree(root)


def enforce_dark_widget_tree(widget: tk.Misc) -> None:
    """Apply dark colors to existing legacy Tk widgets recursively.

    ttk widgets are controlled by named styles. This pass covers widgets such
    as Canvas, Listbox, Text, Entry, Spinbox, Menu and plain Tk containers that
    may have inherited a light Raspberry Pi desktop theme.
    """
    legacy_options = {
        tk.Frame: {"background": PALETTE["bg_surface"]},
        tk.Label: {"background": PALETTE["bg_surface"], "foreground": PALETTE["text"]},
        tk.Canvas: {"background": PALETTE["bg_surface"], "highlightthickness": 0},
        tk.Listbox: {
            "background": PALETTE["bg_input"], "foreground": PALETTE["text"],
            "selectbackground": PALETTE["accent_press"], "selectforeground": PALETTE["text"],
            "highlightbackground": PALETTE["border"], "highlightcolor": PALETTE["border_focus"],
        },
        tk.Text: {
            "background": PALETTE["bg_input"], "foreground": PALETTE["text"],
            "insertbackground": PALETTE["text"], "selectbackground": PALETTE["accent_press"],
            "selectforeground": PALETTE["text"], "highlightbackground": PALETTE["border"],
            "highlightcolor": PALETTE["border_focus"],
        },
        tk.Entry: {
            "background": PALETTE["bg_input"], "foreground": PALETTE["text"],
            "insertbackground": PALETTE["text"], "selectbackground": PALETTE["accent_press"],
            "selectforeground": PALETTE["text"],
        },
        tk.Button: {
            "background": PALETTE["accent_dim"], "foreground": PALETTE["text"],
            "activebackground": PALETTE["accent_press"], "activeforeground": PALETTE["text"],
        },
        tk.Checkbutton: {
            "background": PALETTE["bg_surface"], "foreground": PALETTE["text"],
            "activebackground": PALETTE["bg_surface"], "activeforeground": PALETTE["text"],
            "selectcolor": PALETTE["bg_input"],
        },
        tk.Radiobutton: {
            "background": PALETTE["bg_surface"], "foreground": PALETTE["text"],
            "activebackground": PALETTE["bg_surface"], "activeforeground": PALETTE["text"],
            "selectcolor": PALETTE["bg_input"],
        },
    }
    for cls, options in legacy_options.items():
        if isinstance(widget, cls):
            for option, value in options.items():
                try:
                    widget.configure(**{option: value})
                except (tk.TclError, TypeError):
                    pass
            break
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return
    for child in children:
        enforce_dark_widget_tree(child)


def paint_gradient(
    canvas: tk.Canvas,
    *,
    color_a: str,
    color_b: str,
    direction: str = "horizontal",
) -> None:
    """Paint a linear gradient across the canvas, replacing any prior gradient.

    Lines are tagged "gradient" so callers can repaint on resize by simply
    calling this function again.
    """
    canvas.delete("gradient")
    width = canvas.winfo_width()
    height = canvas.winfo_height()
    if width < 2 or height < 2:
        return

    r1, g1, b1 = _hex_to_rgb(color_a)
    r2, g2, b2 = _hex_to_rgb(color_b)

    # A per-pixel gradient created thousands of canvas items and measured over
    # 200 ms on the production Windows machine. Sixty-four rectangles preserve
    # the visual gradient while keeping item creation bounded.
    steps = min(64, width if direction == "horizontal" else height)
    if direction == "horizontal":
        for i in range(steps):
            t = i / max(1, steps - 1)
            r = int(r1 + (r2 - r1) * t)
            g = int(g1 + (g2 - g1) * t)
            b = int(b1 + (b2 - b1) * t)
            x0 = round(i * width / steps)
            x1 = round((i + 1) * width / steps)
            canvas.create_rectangle(
                x0, 0, x1, height,
                fill=f"#{r:02x}{g:02x}{b:02x}",
                outline="",
                tags="gradient",
            )
    else:
        for i in range(steps):
            t = i / max(1, steps - 1)
            r = int(r1 + (r2 - r1) * t)
            g = int(g1 + (g2 - g1) * t)
            b = int(b1 + (b2 - b1) * t)
            y0 = round(i * height / steps)
            y1 = round((i + 1) * height / steps)
            canvas.create_rectangle(
                0, y0, width, y1,
                fill=f"#{r:02x}{g:02x}{b:02x}",
                outline="",
                tags="gradient",
            )
    canvas.tag_lower("gradient")


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
