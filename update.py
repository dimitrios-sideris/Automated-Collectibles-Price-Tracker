"""Manual inventory matching + Cardmarket price refresh."""
from __future__ import annotations

import argparse  # standard-library command-line parser

from core import update  # central update pipeline


# CLI entry point.  This file intentionally stays thin: all real work lives in
# core.update(), which makes the pipeline easier to test and reuse.
def main() -> int:
    # Define optional command-line switches. Normal daily use needs no switches.
    parser = argparse.ArgumentParser(
        description="Automatically match Yu-Gi-Oh singles and refresh Cardmarket prices."
    )
    parser.add_argument(
        "--price-guide",
        type=str,
        help="Use a local price_guide_3.json instead of downloading today's guide.",
    )
    parser.add_argument("--inventory", type=str, help="Use a specific inventory workbook.")
    parser.add_argument(
        "--rematch",
        action="store_true",
        help="Discard cached card/set mappings and resolve every row again.",
    )
    # Convert the command-line text into an object such as args.rematch / args.inventory.
    args = parser.parse_args()

    # Normal use with no flags downloads today's Cardmarket price guide.
    # --rematch only affects identification/matching; manual overrides still win.
    result = update(args.price_guide, args.inventory, rematch=args.rematch)
    # Print a compact summary. run_daily.py redirects these lines into its log file.
    print("\n=== UPDATE COMPLETE ===")
    print(f"Snapshot:        {result['snapshot_date']}")
    print(f"Inventory:       {result['inventory_file']}")
    print(f"Portfolio:       €{result['total_value']:.2f}")
    print(f"Physical cards:  {result['total_units']}")
    print(f"Priced rows:     {result['priced_rows']}")
    print(f"No-price rows:   {result['no_price_rows']}")
    print(f"Matched rows:    {result['matched_rows']}")
    print(f"Ambiguous rows:  {result['ambiguous_rows']}")
    print(f"Unresolved rows: {result['unresolved_rows']}")
    print(f"Match errors:    {result['match_error_rows']}")
    print(f"Cached matches:  {result['cached_rows']}")
    print(f"Manual IDs:      {result.get('manual_rows', 0)}")
    print(f"Rows rematched:  {result['rematched_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
