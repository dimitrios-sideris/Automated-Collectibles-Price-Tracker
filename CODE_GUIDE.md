# Code Guide

This document is a map for reading the source code. The detailed comments live directly beside the important command groups in the Python files.

## Recommended reading order

If you are learning the project rather than only running it, read the files in this order:

1. `update.py` — tiny command-line entry point.
2. `core.py` — Excel input, SQLite, daily price download, snapshots.
3. `matcher.py` — the more advanced automatic matching algorithm.
4. `app.py` — Tkinter presentation layer.
5. `run_daily.py` — logging wrapper for scheduled updates.
6. `launch_gui.pyw` — two-line Windows GUI launcher.

## Call flow

```text
run_daily.py
  -> subprocess: update.py
       -> core.update()
            -> core.load_inventory()
            -> matcher.match_inventory()
                 -> manual override? use it
                 -> unchanged cached match? use it
                 -> otherwise identify via YGOPRODeck
                 -> map set/product via Cardmarket catalogs
                 -> cache result in SQLite
            -> core.sync_inventory()
            -> core.download_price_guide()
            -> core.read_relevant_prices()
            -> core.store_snapshot()

app.py
  -> core.current_cards() / latest_portfolio() / history queries
  -> matcher.set_manual_product_id() when the user saves an override
```

## `core.py`: inventory, prices, history

Read this file first after `update.py`. Its `update()` function is the central daily pipeline. It owns the human-readable Excel input, SQLite core tables, Cardmarket daily price-guide download, benchmark choice and GUI query helpers.

Important ideas to look for in the comments:

- `os.path` builds portable paths relative to the project folder.
- `openpyxl.load_workbook()` reads the `Triple_Loose` sheet.
- `sqlite3` stores the current inventory mirror and dated history.
- `urllib.request` downloads the Cardmarket guide without `requests`.
- the large raw price guide is temporary; only relevant products stay in SQLite.
- SQLite upserts prevent duplicate history points when the updater runs twice on the same date.

The selected portfolio benchmark is the first positive value in:

```text
trend -> avg7 -> avg30 -> avg1 -> avg -> low
```

## `matcher.py`: identification and Cardmarket Product IDs

This is the most algorithmic file. Matching is independent from daily pricing. A card is resolved in this priority order:

```text
manual override -> cached automatic decision -> new automatic matching
```

A SHA-256 fingerprint of the printing-identifying metadata determines whether an old cached match is still valid. Quantity, location and notes do not affect the fingerprint.

The fresh automatic path has three conceptual stages:

```text
A. identify card/set with YGOPRODeck
B. map the YGOPRODeck set to a Cardmarket expansion
C. choose the Product ID inside that expansion
```

The code intentionally returns `AMBIGUOUS` or `UNRESOLVED` rather than guessing when evidence is weak.

## `app.py`: presentation layer

The GUI reads SQLite; it does not perform the daily web update. Important sections are grouped by comments: widget construction, refresh/filter/sort, manual IDs, clipboard helpers, per-card history, and portfolio plotting.

Double-click a row in **All cards** to open its price history. Single-click only selects it.

The plot uses Tkinter's built-in `Canvas`, so the project does not need `matplotlib`.

## `update.py` and `run_daily.py`: entry points

`update.py` parses optional command-line arguments and calls `core.update()`. `run_daily.py` launches `update.py` in a child process, captures both normal output and errors in a timestamped log, and keeps the newest 60 logs.

A scheduler must invoke `run_daily.py`; merely having the file in the folder does not create a daily background task.

## Why `os` is used for files and folders

This version intentionally does **not** import `pathlib.Path`. File locations are ordinary strings and use familiar standard-library commands such as:

```text
os.path.abspath(...)
os.path.dirname(...)
os.path.join(...)
os.path.exists(...)
os.path.getsize(...)
os.makedirs(...)
os.remove(...)
os.replace(...)
```

This does not reduce pip dependencies versus `pathlib`—both are standard library—but it keeps filesystem code in the style chosen for this project.

## Dependencies

The only package from PyPI is:

```text
openpyxl
```

All other imports are standard library. In particular, HTTP uses `urllib`, the database uses `sqlite3`, the GUI uses `tkinter`, paths use `os`, and plots use `tkinter.Canvas`.

## Generated/local state

- `data/yugioh.sqlite`: current inventory mirror, match cache/overrides, price history, portfolio history.
- `data/cache/`: downloaded Cardmarket/YGOPRODeck matching data.
- `logs/*.log`: updater logs.
- `data/inventory.example.xlsx`: public source inventory.
- `data/inventory.xlsx`: optional private source inventory; preferred automatically if present.

The generated database/cache/log files are ignored by Git in the supplied `.gitignore`.
