"""Tkinter GUI for cards, matching issues, portfolio history and card history."""
from __future__ import annotations

# Tkinter ships with the normal Windows Python installer, so the GUI does not
# require a third-party GUI framework.
import json
import tkinter as tk
from tkinter import messagebox, ttk

from core import (
    card_price_history,
    connect,
    current_cards,
    init_db,
    latest_portfolio,
    portfolio_history,
)
from matcher import clear_manual_product_id, set_manual_product_id


# -----------------------------------------------------------------------------
# Display helpers
# -----------------------------------------------------------------------------
# The GUI never downloads prices.  It formats and displays data already stored
# in SQLite by update.py/run_daily.py.
def money(value) -> str:
    return "—" if value is None else f"€{float(value):.2f}"


def number(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}"


def display_status(row) -> str:
    if row["match_status"] != "MATCHED":
        return row["match_status"] or "UNRESOLVED"
    return "PRICED" if row["benchmark"] is not None else "NO PRICE"


def issue_reason(row) -> str:
    if row["match_status"] != "MATCHED":
        return row["match_reason"] or "Card could not be matched automatically."
    if row["benchmark"] is None:
        return (
            f"Matched to Cardmarket product {row['product_id']}, but the latest "
            "price guide contained no usable trend/average/low benchmark."
        )
    return ""


# -----------------------------------------------------------------------------
# Main desktop application
# -----------------------------------------------------------------------------
# App owns one SQLite connection and four main tabs.  Network/update work is kept
# out of the GUI on purpose so browsing remains fast and deterministic.
class App(tk.Tk):
    SORT_OPTIONS = ("TRICARD code", "Price", "Alphabetical")
    ORDER_OPTIONS = ("Ascending", "Descending")

    # Build the window, initialize the database schema if necessary, then load
    # the newest stored portfolio/cards into the controls.
    def __init__(self):
        super().__init__()
        self.title("Yu-Gi-Oh Singles Price Tracker")
        self.geometry("1380x820")
        self.minsize(1040, 640)

        self.con = connect()
        init_db(self.con)

        self.cards_by_id: dict[str, dict] = {}
        self.history_window: tk.Toplevel | None = None
        self.history_card_id: str | None = None
        self.history_card: dict | None = None
        self.history_rows: list = []

        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.refresh()

    # ------------------------------------------------------------------
    # Main window
    # ------------------------------------------------------------------
    # Construct the header, shared filter/sort/copy controls and the tab notebook.
    def _build(self):
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        self.total_var = tk.StringVar(value="Portfolio: —")
        self.coverage_var = tk.StringVar(value="Coverage: —")
        self.issues_var = tk.StringVar(value="Issues: —")
        self.date_var = tk.StringVar(value="Snapshot: —")

        ttk.Label(
            top,
            textvariable=self.total_var,
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left")
        ttk.Label(top, textvariable=self.coverage_var).pack(side="left", padx=(24, 12))
        ttk.Label(top, textvariable=self.issues_var).pack(side="left", padx=12)
        ttk.Label(top, textvariable=self.date_var).pack(side="left", padx=12)
        ttk.Button(top, text="Refresh view", command=self.refresh).pack(side="right")

        controls = ttk.Frame(self, padding=(12, 0, 12, 8))
        controls.pack(fill="x")

        ttk.Label(controls, text="Filter cards:").pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(controls, textvariable=self.search_var, width=32)
        search_entry.pack(side="left", padx=(8, 18))
        search_entry.bind("<KeyRelease>", lambda _e: self.load_cards())

        ttk.Label(controls, text="Sort by:").pack(side="left")
        self.sort_var = tk.StringVar(value="TRICARD code")
        sort_box = ttk.Combobox(
            controls,
            textvariable=self.sort_var,
            values=self.SORT_OPTIONS,
            state="readonly",
            width=16,
        )
        sort_box.pack(side="left", padx=(8, 10))

        self.order_var = tk.StringVar(value="Ascending")
        order_box = ttk.Combobox(
            controls,
            textvariable=self.order_var,
            values=self.ORDER_OPTIONS,
            state="readonly",
            width=11,
        )
        order_box.pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Sort", command=self.load_cards).pack(side="left")

        self.copy_button = ttk.Button(
            controls,
            text="Copy content",
            command=self.copy_current_view,
        )
        self.copy_button.pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        cards_tab = ttk.Frame(self.notebook)
        issues_tab = ttk.Frame(self.notebook)
        hist_tab = ttk.Frame(self.notebook)
        manual_tab = ttk.Frame(self.notebook)
        self.notebook.add(cards_tab, text="All cards")
        self.notebook.add(issues_tab, text="Pricing & matching issues")
        self.notebook.add(hist_tab, text="Portfolio history")
        self.notebook.add(manual_tab, text="Manual Cardmarket ID Entries")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_cards_tab(cards_tab)
        self._build_issues_tab(issues_tab)
        self._build_portfolio_history_tab(hist_tab)
        self._build_manual_entries_tab(manual_tab)
        self._on_tab_changed()

        self.status_var = tk.StringVar(
            value="Double-click a card in All cards to open its stored price history."
        )
        ttk.Label(
            self,
            textvariable=self.status_var,
            padding=(12, 3, 12, 8),
            anchor="w",
        ).pack(fill="x")

    # All cards: current inventory joined to the latest stored unit price.
    def _build_cards_tab(self, parent):
        columns = (
            "id",
            "name",
            "code",
            "rarity",
            "qty",
            "unit",
            "value",
            "location",
            "status",
        )
        self.tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")

        headings = {
            "id": "TRICARD ID",
            "name": "Card",
            "code": "Code",
            "rarity": "Rarity",
            "qty": "Qty",
            "unit": "Unit price",
            "value": "Value",
            "location": "Location",
            "status": "Status",
        }
        widths = {
            "id": 115,
            "name": 260,
            "code": 110,
            "rarity": 125,
            "qty": 50,
            "unit": 85,
            "value": 85,
            "location": 145,
            "status": 100,
        }

        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(
                col,
                width=widths[col],
                anchor="e" if col in {"qty", "unit", "value"} else "w",
            )

        self.tree.tag_configure("issue", background="#fff4e5")
        self.tree.bind("<Double-1>", self._on_card_double_click)

        yscroll = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(parent, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

    # Issues: only cards that are unmatched/ambiguous/error/no-price.
    def _build_issues_tab(self, parent):
        toolbar = ttk.Frame(parent, padding=(8, 8, 8, 4))
        toolbar.pack(fill="x")
        ttk.Label(
            toolbar,
            text="Cards that are unmatched, ambiguous, errored, or missing a usable price.",
        ).pack(side="left")

        table_frame = ttk.Frame(parent)
        table_frame.pack(fill="both", expand=True)

        issue_columns = ("id", "name", "code", "rarity", "status", "product", "reason")
        self.issue_tree = ttk.Treeview(table_frame, columns=issue_columns, show="headings")
        issue_headings = {
            "id": "TRICARD ID",
            "name": "Card",
            "code": "Code",
            "rarity": "Rarity",
            "status": "Status",
            "product": "Product ID",
            "reason": "Why it is not priced",
        }
        issue_widths = {
            "id": 110,
            "name": 225,
            "code": 105,
            "rarity": 115,
            "status": 100,
            "product": 90,
            "reason": 520,
        }
        for col in issue_columns:
            self.issue_tree.heading(col, text=issue_headings[col])
            self.issue_tree.column(col, width=issue_widths[col], anchor="w")

        issue_scroll_y = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.issue_tree.yview,
        )
        issue_scroll_x = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=self.issue_tree.xview,
        )
        self.issue_tree.configure(
            yscrollcommand=issue_scroll_y.set,
            xscrollcommand=issue_scroll_x.set,
        )
        self.issue_tree.grid(row=0, column=0, sticky="nsew")
        issue_scroll_y.grid(row=0, column=1, sticky="ns")
        issue_scroll_x.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

    # Portfolio history: a lightweight Tk canvas plot (no plotting dependency).
    def _build_portfolio_history_tab(self, parent):
        self.canvas = tk.Canvas(parent, background="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=12)
        self.canvas.bind("<Configure>", lambda _e: self.draw_history())

    # Manual-ID tab: lets the user resolve the rare cases where automation cannot
    # safely choose one Cardmarket Product ID.
    def _build_manual_entries_tab(self, parent):
        intro = ttk.Frame(parent, padding=(8, 8, 8, 4))
        intro.pack(fill="x")
        ttk.Label(
            intro,
            text=(
                "Use this only when automatic matching is unresolved or ambiguous. "
                "A manual Product ID overrides automatic matching and survives --rematch."
            ),
            wraplength=1000,
            justify="left",
        ).pack(side="left", fill="x", expand=True)

        table_frame = ttk.Frame(parent)
        table_frame.pack(fill="both", expand=True, padx=8)

        columns = (
            "id", "name", "code", "rarity", "status",
            "product", "source", "candidates", "reason",
        )
        self.manual_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "id": "TRICARD ID",
            "name": "Card",
            "code": "Code",
            "rarity": "Rarity",
            "status": "Status",
            "product": "Current Product ID",
            "source": "Match source",
            "candidates": "Candidate IDs",
            "reason": "Reason / note",
        }
        widths = {
            "id": 110, "name": 220, "code": 105, "rarity": 115,
            "status": 95, "product": 115, "source": 120,
            "candidates": 180, "reason": 430,
        }
        for col in columns:
            self.manual_tree.heading(col, text=headings[col])
            self.manual_tree.column(col, width=widths[col], anchor="w")

        self.manual_tree.bind("<<TreeviewSelect>>", self._on_manual_row_selected)

        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.manual_tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.manual_tree.xview)
        self.manual_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.manual_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        form = ttk.Frame(parent, padding=10)
        form.pack(fill="x")

        ttk.Label(form, text="TRICARD ID:").grid(row=0, column=0, sticky="w")
        self.manual_card_var = tk.StringVar()
        card_entry = ttk.Entry(
            form, textvariable=self.manual_card_var, width=20, state="readonly"
        )
        card_entry.grid(row=0, column=1, sticky="w", padx=(8, 22))

        ttk.Label(form, text="Cardmarket Product ID:").grid(row=0, column=2, sticky="w")
        self.manual_product_var = tk.StringVar()
        product_entry = ttk.Entry(form, textvariable=self.manual_product_var, width=18)
        product_entry.grid(row=0, column=3, sticky="w", padx=(8, 16))

        ttk.Button(
            form,
            text="Save manual ID",
            command=self.save_manual_id,
        ).grid(row=0, column=4, padx=(0, 8))
        ttk.Button(
            form,
            text="Remove override",
            command=self.remove_manual_id,
        ).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(
            form,
            text="Refresh",
            command=self.load_manual_entries,
        ).grid(row=0, column=6)

        ttk.Label(
            form,
            text=(
                "After saving a new ID, run run_daily.py (or update.py) once to fetch "
                "its Cardmarket price for the current snapshot."
            ),
        ).grid(row=1, column=0, columnspan=7, sticky="w", pady=(8, 0))

    # -------------------------------------------------------------------------
    # Refresh, filtering and sorting
    # -------------------------------------------------------------------------
    # Refreshing the view re-queries SQLite only.  It does NOT download new prices.
    def refresh(self):
        try:
            # Read the newest portfolio snapshot and current inventory from SQLite.
            portfolio = latest_portfolio(self.con)
            cards = current_cards("", self.con)
            issues = [row for row in cards if display_status(row) != "PRICED"]
            matched = sum(1 for row in cards if row["match_status"] == "MATCHED")

            if portfolio:
                self.total_var.set(f"Portfolio: €{portfolio['total_value']:.2f}")
                self.coverage_var.set(
                    f"Coverage: {portfolio['priced_rows']} priced / {len(cards)} rows "
                    f"({matched} matched)"
                )
                self.issues_var.set(f"Issues: {len(issues)}")
                self.date_var.set(f"Snapshot: {portfolio['snapshot_date']}")
            else:
                self.total_var.set("Portfolio: run update.py first")
                self.coverage_var.set(f"Inventory: {len(cards)} rows")
                self.issues_var.set(f"Issues: {len(issues)}")
                self.date_var.set("Snapshot: —")

            # Rebuild every visible tab from the same database state.
            self.load_cards()
            self.load_issues()
            self.load_manual_entries()
            self.draw_history()

            if self.history_window and self.history_window.winfo_exists() and self.history_card_id:
                self.show_card_history(self.history_card_id, bring_to_front=False)
        except Exception as exc:
            messagebox.showerror("Yu-Gi-Oh Price Tracker", str(exc))

    # Apply GUI-only ordering.  Sorting never changes Excel or database order.
    def _sorted_cards(self, rows):
        rows = [dict(row) for row in rows]
        sort_by = self.sort_var.get()
        descending = self.order_var.get() == "Descending"

        if sort_by == "Price":
            # Keep unpriced rows at the bottom in both directions instead of treating
            # a missing price as zero and mixing it into the numerical sort.
            priced = [row for row in rows if row.get("benchmark") is not None]
            unpriced = [row for row in rows if row.get("benchmark") is None]
            priced.sort(key=lambda row: float(row["benchmark"]), reverse=descending)
            unpriced.sort(key=lambda row: (row.get("english_name") or "").casefold())
            return priced + unpriced

        if sort_by == "Alphabetical":
            key = lambda row: (
                (row.get("english_name") or "").casefold(),
                (row.get("card_id") or "").casefold(),
            )
        else:  # TRICARD code
            key = lambda row: (row.get("card_id") or "").casefold()

        return sorted(rows, key=key, reverse=descending)

    # Rebuild the visible All cards table from current search/sort settings.
    def load_cards(self):
        selected = self.tree.selection()
        selected_id = selected[0] if selected else None

        # Treeview has no direct "replace all" call, so clear current rows first.
        for item in self.tree.get_children():
            self.tree.delete(item)

        # The SQL helper applies the text filter; _sorted_cards applies only display order.
        rows = self._sorted_cards(current_cards(self.search_var.get(), self.con))
        self.cards_by_id = {row["card_id"]: row for row in rows}

        for row in rows:
            status = display_status(row)
            self.tree.insert(
                "",
                "end",
                iid=row["card_id"],
                values=(
                    row["card_id"],
                    row["english_name"],
                    row["card_code"] or "—",
                    row["rarity"] or "—",
                    row["quantity"],
                    money(row["benchmark"]),
                    money(row["total_value"]),
                    row["location"] or "—",
                    status,
                ),
                tags=("issue",) if status != "PRICED" else (),
            )

        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)

    # Rebuild the Issues table with human-readable reasons produced by matcher.py.
    def load_issues(self):
        for item in self.issue_tree.get_children():
            self.issue_tree.delete(item)

        for row in current_cards("", self.con):
            status = display_status(row)
            if status == "PRICED":
                continue
            self.issue_tree.insert(
                "",
                "end",
                values=(
                    row["card_id"],
                    row["english_name"],
                    row["card_code"] or "—",
                    row["rarity"] or "—",
                    status,
                    row["product_id"] or "—",
                    issue_reason(row),
                ),
            )

    def _candidate_text(self, row) -> str:
        raw = row.get("candidate_products")
        if not raw:
            return "—"
        try:
            # Candidate IDs are stored as JSON text in SQLite; decode for display.
            values = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(values, list):
                return ", ".join(str(value) for value in values) or "—"
        except Exception:
            pass
        return str(raw)

    # Show current issues plus already-saved overrides so an override can also be
    # reviewed, changed or removed later.
    def load_manual_entries(self):
        if not hasattr(self, "manual_tree"):
            return

        selected = self.manual_tree.selection()
        selected_id = selected[0] if selected else None
        for item in self.manual_tree.get_children():
            self.manual_tree.delete(item)

        rows = [dict(row) for row in current_cards("", self.con)]
        rows = [
            row for row in rows
            if display_status(row) != "PRICED"
            or row.get("match_source") == "manual override"
        ]
        rows.sort(key=lambda row: (row.get("card_id") or "").casefold())

        for row in rows:
            status = display_status(row)
            self.manual_tree.insert(
                "",
                "end",
                iid=row["card_id"],
                values=(
                    row["card_id"],
                    row.get("english_name") or "—",
                    row.get("card_code") or "—",
                    row.get("rarity") or "—",
                    status,
                    row.get("product_id") or "—",
                    row.get("match_source") or "—",
                    self._candidate_text(row),
                    issue_reason(row)
                    or row.get("match_reason")
                    or "—",
                ),
            )

        if selected_id and self.manual_tree.exists(selected_id):
            self.manual_tree.selection_set(selected_id)

    def _on_manual_row_selected(self, _event=None):
        selected = self.manual_tree.selection()
        if not selected:
            return
        card_id = selected[0]
        values = self.manual_tree.item(card_id, "values")
        self.manual_card_var.set(card_id)
        current_product = values[5] if len(values) > 5 else ""
        self.manual_product_var.set("" if current_product in {"", "—"} else current_product)

    # Save the override immediately in SQLite.  A following updater run is needed
    # to fetch/store the corresponding Cardmarket price for the current snapshot.
    def save_manual_id(self):
        card_id = self.manual_card_var.get().strip()
        product_text = self.manual_product_var.get().strip()
        if not card_id:
            messagebox.showinfo(
                "Manual Cardmarket ID",
                "Select a card in the Manual Cardmarket ID Entries table first.",
            )
            return
        try:
            product_id = int(product_text)
            if product_id <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Manual Cardmarket ID",
                "Cardmarket Product ID must be a positive integer.",
            )
            return

        try:
            set_manual_product_id(self.con, card_id, product_id)
            self.status_var.set(
                f"Saved manual Product ID {product_id} for {card_id}. "
                "Run run_daily.py once to fetch its price."
            )
            self.refresh()
            if self.manual_tree.exists(card_id):
                self.manual_tree.selection_set(card_id)
                self.manual_tree.see(card_id)
        except Exception as exc:
            messagebox.showerror("Manual Cardmarket ID", str(exc))

    def remove_manual_id(self):
        card_id = self.manual_card_var.get().strip()
        if not card_id:
            messagebox.showinfo(
                "Manual Cardmarket ID",
                "Select a card first.",
            )
            return
        try:
            clear_manual_product_id(self.con, card_id)
            self.manual_product_var.set("")
            self.status_var.set(
                f"Removed manual override for {card_id}. "
                "Run run_daily.py once to restore automatic matching."
            )
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Manual Cardmarket ID", str(exc))

    # -------------------------------------------------------------------------
    # Clipboard helpers
    # -------------------------------------------------------------------------
    # Table data is copied as tab-separated text, making it easy to paste into a
    # spreadsheet, GitHub issue, or ChatGPT conversation.
    def _copy_text(self, text: str, description: str):
        # Replace clipboard contents with plain text and flush the Tk event queue so
        # the copied text remains available immediately to other applications.
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.status_var.set(f"Copied {description} to clipboard.")

    def copy_tree_content(self, tree: ttk.Treeview, title: str):
        columns = list(tree["columns"])
        headings = [tree.heading(col, "text") for col in columns]
        lines = [title, "\t".join(headings)]

        for item in tree.get_children(""):
            values = tree.item(item, "values")
            lines.append("\t".join(str(value) for value in values))

        self._copy_text("\n".join(lines), f"{len(lines) - 2} visible rows")

    def copy_current_view(self):
        """Copy the table shown in the currently selected main tab."""
        tab_index = self.notebook.index(self.notebook.select())

        if tab_index == 0:
            self.copy_tree_content(self.tree, "All cards")
        elif tab_index == 1:
            self.copy_tree_content(
                self.issue_tree,
                "Pricing & matching issues",
            )
        elif tab_index == 3:
            self.copy_tree_content(
                self.manual_tree,
                "Manual Cardmarket ID Entries",
            )

    def _on_tab_changed(self, _event=None):
        """Enable Copy content only for tabs that display copyable text/table data."""
        if not hasattr(self, "copy_button"):
            return

        tab_index = self.notebook.index(self.notebook.select())
        if tab_index == 2:  # Portfolio history is a plot.
            self.copy_button.state(["disabled"])
        else:
            self.copy_button.state(["!disabled"])

    # -------------------------------------------------------------------------
    # Per-card price history
    # -------------------------------------------------------------------------
    # IMPORTANT UX rule: history opens only on a double-click of a real All cards
    # row.  Single-click is selection only.
    def _on_card_double_click(self, event):
        """Open history only when a real All-cards row is double-clicked."""
        # identify_row() ensures a double-click on blank table space does nothing.
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        self.tree.focus(item)
        self.show_card_history(item)

    def _ensure_history_window(self):
        if self.history_window and self.history_window.winfo_exists():
            return

        # Toplevel creates a child window while keeping the main application alive.
        win = tk.Toplevel(self)
        win.title("Card history")
        win.geometry("1120x760")
        win.minsize(860, 600)
        win.protocol("WM_DELETE_WINDOW", self._close_history_window)
        self.history_window = win

        top = ttk.Frame(win, padding=12)
        top.pack(fill="x")

        self.history_title_var = tk.StringVar(value="Card history")
        self.history_meta_var = tk.StringVar(value="")
        ttk.Label(
            top,
            textvariable=self.history_title_var,
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            top,
            textvariable=self.history_meta_var,
            wraplength=980,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        buttonbar = ttk.Frame(win, padding=(12, 0, 12, 8))
        buttonbar.pack(fill="x")
        ttk.Button(
            buttonbar,
            text="Copy content",
            command=self.copy_card_history,
        ).pack(side="left")
        ttk.Button(buttonbar, text="Close", command=self._close_history_window).pack(side="right")

        self.card_history_canvas = tk.Canvas(
            win,
            background="white",
            highlightthickness=1,
            highlightbackground="#dddddd",
            height=255,
        )
        self.card_history_canvas.pack(fill="x", padx=12, pady=(0, 10))
        self.card_history_canvas.bind(
            "<Configure>",
            lambda _e: self.draw_card_history(),
        )

        table_frame = ttk.Frame(win, padding=(12, 0, 12, 12))
        table_frame.pack(fill="both", expand=True)

        columns = (
            "date",
            "benchmark",
            "source",
            "trend",
            "avg1",
            "avg7",
            "avg30",
            "avg",
            "low",
        )
        self.history_tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {
            "date": "Date",
            "benchmark": "Benchmark",
            "source": "Source",
            "trend": "Trend",
            "avg1": "Avg 1d",
            "avg7": "Avg 7d",
            "avg30": "Avg 30d",
            "avg": "Avg",
            "low": "Low",
        }
        widths = {
            "date": 105,
            "benchmark": 95,
            "source": 90,
            "trend": 85,
            "avg1": 85,
            "avg7": 85,
            "avg30": 85,
            "avg": 85,
            "low": 85,
        }
        for col in columns:
            self.history_tree.heading(col, text=headings[col])
            self.history_tree.column(
                col,
                width=widths[col],
                anchor="e" if col not in {"date", "source"} else "w",
            )

        scroll_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.history_tree.yview)
        scroll_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.history_tree.xview)
        self.history_tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

    def _close_history_window(self):
        if self.history_window and self.history_window.winfo_exists():
            self.history_window.destroy()
        self.history_window = None
        self.history_card_id = None
        self.history_card = None
        self.history_rows = []

    def show_card_history(self, card_id: str, bring_to_front: bool = True):
        row = self.cards_by_id.get(card_id)
        if row is None:
            matches = [dict(item) for item in current_cards("", self.con) if item["card_id"] == card_id]
            if not matches:
                return
            row = matches[0]

        self._ensure_history_window()
        self.history_card_id = card_id
        self.history_card = dict(row)
        self.history_rows = list(card_price_history(card_id, self.con))

        status = display_status(row)
        reason = issue_reason(row)
        product = row.get("product_id") or "—"
        latest = money(row.get("benchmark"))

        self.history_title_var.set(
            f"{row.get('english_name') or 'Unknown card'}  ·  {card_id}"
        )
        meta = (
            f"Code: {row.get('card_code') or '—'}   |   "
            f"Rarity: {row.get('rarity') or '—'}   |   "
            f"Quantity: {row.get('quantity', 0)}   |   "
            f"Product ID: {product}   |   "
            f"Status: {status}   |   Latest unit value: {latest}"
        )
        if reason:
            meta += f"\nReason: {reason}"
        self.history_meta_var.set(meta)

        for item in self.history_tree.get_children():
            self.history_tree.delete(item)

        for hist in self.history_rows:
            self.history_tree.insert(
                "",
                "end",
                values=(
                    hist["snapshot_date"],
                    money(hist["benchmark"]),
                    hist["benchmark_source"] or "—",
                    money(hist["trend"]),
                    money(hist["avg1"]),
                    money(hist["avg7"]),
                    money(hist["avg30"]),
                    money(hist["avg"]),
                    money(hist["low"]),
                ),
            )

        self.draw_card_history()
        if bring_to_front:
            self.history_window.deiconify()
            self.history_window.lift()
            self.history_window.focus_force()

    def copy_card_history(self):
        if not self.history_card:
            return

        card = self.history_card
        lines = [
            "Card history",
            f"Card ID: {card.get('card_id') or '—'}",
            f"Card: {card.get('english_name') or '—'}",
            f"Printed name: {card.get('printed_name') or '—'}",
            f"Code: {card.get('card_code') or '—'}",
            f"Rarity: {card.get('rarity') or '—'}",
            f"Edition: {card.get('edition') or '—'}",
            f"Language: {card.get('language') or '—'}",
            f"Quantity: {card.get('quantity', 0)}",
            f"Condition: {card.get('condition') or '—'}",
            f"Location: {card.get('location') or '—'}",
            f"Match status: {card.get('match_status') or '—'}",
            f"Cardmarket Product ID: {card.get('product_id') or '—'}",
        ]

        reason = issue_reason(card)
        if reason:
            lines.append(f"Issue reason: {reason}")

        lines.extend(
            [
                "",
                "Date\tBenchmark\tSource\tTrend\tAvg 1d\tAvg 7d\tAvg 30d\tAvg\tLow",
            ]
        )
        for hist in self.history_rows:
            lines.append(
                "\t".join(
                    [
                        str(hist["snapshot_date"]),
                        money(hist["benchmark"]),
                        str(hist["benchmark_source"] or "—"),
                        money(hist["trend"]),
                        money(hist["avg1"]),
                        money(hist["avg7"]),
                        money(hist["avg30"]),
                        money(hist["avg"]),
                        money(hist["low"]),
                    ]
                )
            )

        if not self.history_rows:
            lines.append("No stored price history for this card.")

        self._copy_text("\n".join(lines), f"history for {card.get('card_id', 'card')}")

    # Draw the selected card's benchmark history with Tkinter Canvas primitives.
    # The plot reads stored snapshots; opening it does not trigger an update.
    def draw_card_history(self):
        if not hasattr(self, "card_history_canvas"):
            return

        c = self.card_history_canvas
        # Canvas drawings are recreated from scratch whenever the window is resized.
        c.delete("all")
        w, h = max(c.winfo_width(), 240), max(c.winfo_height(), 180)
        margin_left, margin_right = 62, 30
        margin_top, margin_bottom = 38, 45

        priced = [row for row in self.history_rows if row["benchmark"] is not None]
        if not priced:
            c.create_text(
                w / 2,
                h / 2,
                text="No stored benchmark history for this card.",
                fill="#555",
            )
            return

        values = [float(row["benchmark"]) for row in priced]
        dates = [row["snapshot_date"] for row in priced]
        lo, hi = min(values), max(values)
        if hi == lo:
            pad = max(hi * 0.05, 0.05)
            lo = max(0.0, lo - pad)
            hi += pad

        x_span = max(w - margin_left - margin_right, 1)
        y_span = max(h - margin_top - margin_bottom, 1)
        points = []
        for i, value in enumerate(values):
            x = margin_left + x_span * i / max(len(values) - 1, 1)
            y = margin_top + (hi - value) / (hi - lo) * y_span
            points.extend((x, y))

        c.create_line(
            margin_left,
            margin_top,
            margin_left,
            h - margin_bottom,
            fill="#888",
        )
        c.create_line(
            margin_left,
            h - margin_bottom,
            w - margin_right,
            h - margin_bottom,
            fill="#888",
        )

        if len(points) >= 4:
            c.create_line(*points, width=2, fill="#2457a6")
        else:
            c.create_oval(
                points[0] - 3,
                points[1] - 3,
                points[0] + 3,
                points[1] + 3,
                fill="#2457a6",
                outline="",
            )

        c.create_text(margin_left, 18, text="Unit-price history", anchor="w", font=("Segoe UI", 11, "bold"))
        c.create_text(margin_left - 8, margin_top, text=f"€{hi:.2f}", anchor="e")
        c.create_text(margin_left - 8, h - margin_bottom, text=f"€{lo:.2f}", anchor="e")
        c.create_text(margin_left, h - 18, text=dates[0], anchor="w")
        c.create_text(w - margin_right, h - 18, text=dates[-1], anchor="e")

    # ------------------------------------------------------------------
    # Portfolio history plot
    # ------------------------------------------------------------------
    # Draw the total portfolio-value history from portfolio_history in SQLite.
    def draw_history(self):
        c = self.canvas
        c.delete("all")

        # Query every stored daily portfolio total and draw it using Tkinter primitives.
        rows = portfolio_history(self.con)
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 200)
        margin = 55

        if not rows:
            c.create_text(
                w / 2,
                h / 2,
                text="No history yet. Run update.py first.",
                fill="#555",
            )
            return

        values = [float(r["total_value"]) for r in rows]
        dates = [r["snapshot_date"] for r in rows]
        lo, hi = min(values), max(values)
        if hi == lo:
            hi = lo + 1.0

        x_span = max(w - 2 * margin, 1)
        y_span = max(h - 2 * margin, 1)
        points = []
        for i, value in enumerate(values):
            x = margin + (x_span * i / max(len(values) - 1, 1))
            y = h - margin - (value - lo) / (hi - lo) * y_span
            points.extend((x, y))

        c.create_line(margin, margin, margin, h - margin, fill="#888")
        c.create_line(margin, h - margin, w - margin, h - margin, fill="#888")
        if len(points) >= 4:
            c.create_line(*points, width=2, fill="#2457a6")
        else:
            c.create_oval(
                points[0] - 3,
                points[1] - 3,
                points[0] + 3,
                points[1] + 3,
                fill="#2457a6",
                outline="",
            )
        c.create_text(margin, margin - 15, text=f"€{hi:.2f}", anchor="w")
        c.create_text(margin, h - margin + 18, text=f"€{lo:.2f}", anchor="w")
        c.create_text(margin, h - 18, text=dates[0], anchor="w")
        c.create_text(w - margin, h - 18, text=dates[-1], anchor="e")
        c.create_text(
            w / 2,
            24,
            text="Portfolio value history",
            font=("Segoe UI", 12, "bold"),
        )

    # Close the database connection cleanly before destroying the Tk root window.
    def close(self):
        if self.history_window and self.history_window.winfo_exists():
            self.history_window.destroy()
        self.con.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
