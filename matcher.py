"""Automatic printing -> Cardmarket product matching.

The matcher is deliberately isolated from the pricing code.  It combines two
public data sources:

* YGOPRODeck identifies a Yu-Gi-Oh printing from the human-readable inventory
  fields (English card name, set code and rarity).
* Cardmarket's downloadable product catalog provides product IDs and expansion
  IDs used by the daily Cardmarket price guide.

Successful and unsuccessful decisions are cached in SQLite.  A normal daily
price refresh therefore does not rematch cards that have not changed.
"""
from __future__ import annotations

# Everything in this file is from Python's standard library; there is no extra
# matching/API package to install.
import hashlib             # fingerprints and safe cache filenames
import json                # API/catalog/cache serialization
import os                  # file and cache-directory handling
import re                  # set-code, rarity and Cardmarket-name cleanup
import sqlite3             # persistent match cache and manual overrides
import ssl                 # HTTPS certificate context
import time                # polite pause between YGOPRODeck requests
import unicodedata         # accent-insensitive text normalization
import urllib.parse        # query-string building and URL validation
import urllib.request      # HTTPS downloads without requests
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Callable, Iterable

# -----------------------------------------------------------------------------
# Remote sources, local cache and matching status values
# -----------------------------------------------------------------------------
# Cardmarket catalogs map names/expansions to Product IDs.  YGOPRODeck is used
# to identify the human-readable printing first.  Catalog/API responses are
# cached so normal daily pricing does not repeatedly rematch unchanged cards.
ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "data", "cache")

CARDMARKET_SINGLES_URL = (
    "https://downloads.s3.cardmarket.com/"
    "productCatalog/productList/products_singles_3.json"
)
CARDMARKET_NONSINGLES_URL = (
    "https://downloads.s3.cardmarket.com/"
    "productCatalog/productList/products_nonsingles_3.json"
)
YGOPRO_CARDINFO_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
YGOPRO_CARDSETSINFO_URL = "https://db.ygoprodeck.com/api/v7/cardsetsinfo.php"

CARDMARKET_CATALOG_MAX_AGE_DAYS = 7
MAX_CARDMARKET_BYTES = 120 * 1024 * 1024
MAX_YGOPRO_BYTES = 25 * 1024 * 1024
YGOPRO_REQUEST_PAUSE_SECONDS = 0.08  # stay below the documented 20 req/s limit
USER_AGENT = "YuGiOhSinglesPriceTracker/2.0"

MATCHED = "MATCHED"
UNRESOLVED = "UNRESOLVED"
AMBIGUOUS = "AMBIGUOUS"
ERROR = "ERROR"

Progress = Callable[[str], None]


# -----------------------------------------------------------------------------
# Normalization helpers
# -----------------------------------------------------------------------------
# Cross-source text is not always formatted identically.  These helpers create
# conservative comparison keys without changing the user's original inventory.
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value) -> str:
    return "" if value is None else str(value).strip()


def normalize_text(value) -> str:
    """Normalize names conservatively for exact cross-source comparison."""
    text = unicodedata.normalize("NFKD", _clean_text(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = text.replace("’", "'").replace("`", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_card_code(value) -> str:
    """Convert common European-language set codes to YGOPRODeck's EN code."""
    code = _clean_text(value).upper().replace(" ", "")
    # SDAZ-DE001 -> SDAZ-EN001.  Codes such as LEDD-ENC02 already stay intact.
    return re.sub(r"-(DE|FR|IT|PT|SP)(?=[A-Z0-9])", "-EN", code)


def _rarity_key(value) -> str:
    text = normalize_text(value)
    aliases = {
        "c": "common",
        "common": "common",
        "r": "rare",
        "rare": "rare",
        "sr": "super rare",
        "super": "super rare",
        "super rare": "super rare",
        "ur": "ultra rare",
        "ultra": "ultra rare",
        "ultra rare": "ultra rare",
        "scr": "secret rare",
        "secret": "secret rare",
        "secret rare": "secret rare",
        "utr": "ultimate rare",
        "ultimate rare": "ultimate rare",
        "gr": "ghost rare",
        "ghost rare": "ghost rare",
        "starfoil": "starfoil rare",
        "starfoil rare": "starfoil rare",
        "mosaic rare": "mosaic rare",
        "shatterfoil rare": "shatterfoil rare",
        "gold rare": "gold rare",
        "gold secret rare": "gold secret rare",
        "platinum secret rare": "platinum secret rare",
        "collectors rare": "collectors rare",
        "collector s rare": "collectors rare",
        "quarter century secret rare": "quarter century secret rare",
        "starlight rare": "starlight rare",
        "duel terminal normal parallel rare": "duel terminal normal parallel rare",
        "normal parallel rare": "normal parallel rare",
        "parallel rare": "parallel rare",
    }
    return aliases.get(text, text)


# A fingerprint represents only fields that identify the printing.  Quantity,
# location and notes are excluded, so changing those does not force a rematch.
def _fingerprint(card: dict) -> str:
    identity = {
        "english_name": _clean_text(card.get("english_name")),
        "language": _clean_text(card.get("language")).upper(),
        "card_code": _clean_text(card.get("card_code")).upper(),
        "rarity": _clean_text(card.get("rarity")),
        "edition": _clean_text(card.get("edition")),
    }
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# -----------------------------------------------------------------------------
# Matching cache and manual overrides in SQLite
# -----------------------------------------------------------------------------
# card_matches caches per-card decisions; set_mappings caches expansion mapping;
# manual_cardmarket_ids is the user's explicit escape hatch for ambiguous cards.
def init_match_tables(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS card_matches (
            card_id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            product_id INTEGER,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            source TEXT,
            confidence REAL,
            ygopro_name TEXT,
            ygopro_set_name TEXT,
            ygopro_rarity TEXT,
            cardmarket_expansion_id INTEGER,
            candidates_json TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS set_mappings (
            ygopro_set_name TEXT PRIMARY KEY,
            cardmarket_expansion_id INTEGER,
            status TEXT NOT NULL,
            confidence REAL,
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS manual_cardmarket_ids (
            card_id TEXT PRIMARY KEY,
            product_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


# Manual IDs always take precedence over automatic matching.  They are marked as
# MATCHED with confidence 1.0 because the user explicitly chose the product.
def _manual_result(product_id: int) -> dict:
    """Return the canonical match metadata for a user-supplied Product ID."""
    return {
        "product_id": int(product_id),
        "status": MATCHED,
        "reason": "Cardmarket Product ID supplied manually by the user.",
        "source": "manual override",
        "confidence": 1.0,
        "candidates": [int(product_id)],
    }


def get_manual_product_id(con: sqlite3.Connection, card_id: str) -> int | None:
    """Return a saved manual Product ID for one TRICARD row, if present."""
    init_match_tables(con)
    row = con.execute(
        "SELECT product_id FROM manual_cardmarket_ids WHERE card_id = ?",
        (str(card_id),),
    ).fetchone()
    return int(row[0]) if row else None


def set_manual_product_id(
    con: sqlite3.Connection,
    card_id: str,
    product_id: int,
) -> None:
    """Save a manual Product ID and make it visible in the current GUI immediately."""
    init_match_tables(con)
    card_id = str(card_id).strip()
    try:
        product_id = int(product_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Cardmarket Product ID must be a positive integer.") from exc
    if product_id <= 0:
        raise ValueError("Cardmarket Product ID must be a positive integer.")

    exists = con.execute(
        "SELECT 1 FROM inventory WHERE card_id = ?",
        (card_id,),
    ).fetchone()
    if not exists:
        raise ValueError(f"Unknown inventory Card ID: {card_id}")

    result = _manual_result(product_id)
    with con:
        con.execute(
            """
            INSERT INTO manual_cardmarket_ids (card_id, product_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(card_id) DO UPDATE SET
                product_id=excluded.product_id,
                updated_at=excluded.updated_at
            """,
            (card_id, product_id, _utc_now()),
        )
        con.execute(
            """
            UPDATE inventory
            SET product_id = ?,
                match_status = ?,
                match_reason = ?,
                match_source = ?,
                match_confidence = ?,
                ygopro_set_name = NULL,
                cardmarket_expansion_id = NULL,
                candidate_products = ?
            WHERE card_id = ?
            """,
            (
                product_id,
                MATCHED,
                result["reason"],
                result["source"],
                1.0,
                json.dumps([product_id]),
                card_id,
            ),
        )


def clear_manual_product_id(con: sqlite3.Connection, card_id: str) -> None:
    """Remove a manual override. Automatic matching is restored on the next update."""
    init_match_tables(con)
    card_id = str(card_id).strip()
    with con:
        con.execute(
            "DELETE FROM manual_cardmarket_ids WHERE card_id = ?",
            (card_id,),
        )
        con.execute(
            """
            UPDATE inventory
            SET product_id = NULL,
                match_status = ?,
                match_reason = ?,
                match_source = NULL,
                match_confidence = NULL,
                ygopro_set_name = NULL,
                cardmarket_expansion_id = NULL,
                candidate_products = NULL
            WHERE card_id = ?
            """,
            (
                UNRESOLVED,
                "Manual override removed. Run update.py or run_daily.py to restore automatic matching.",
                card_id,
            ),
        )


def _load_manual_overrides(con: sqlite3.Connection) -> dict[str, int]:
    init_match_tables(con)
    return {
        str(row[0]): int(row[1])
        for row in con.execute(
            "SELECT card_id, product_id FROM manual_cardmarket_ids"
        )
    }


# -----------------------------------------------------------------------------
# Safe HTTP download + file cache
# -----------------------------------------------------------------------------
# Only known HTTPS Cardmarket/YGOPRODeck endpoints are accepted.  The Cardmarket
# product catalogs are much less volatile than prices, so they may be reused for
# several days rather than downloaded on every daily price refresh.
def _validate_download_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Refusing non-HTTPS URL: {url}")
    if parsed.hostname == "downloads.s3.cardmarket.com":
        if not parsed.path.startswith("/productCatalog/productList/"):
            raise ValueError(f"Unexpected Cardmarket catalog path: {parsed.path}")
        return
    if parsed.hostname == "db.ygoprodeck.com":
        if not parsed.path.startswith("/api/v7/"):
            raise ValueError(f"Unexpected YGOPRODeck path: {parsed.path}")
        return
    raise ValueError(f"Refusing unexpected download host: {parsed.hostname}")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_json(url: str, max_bytes: int) -> object:
    # Reject unknown hosts/paths before any network request is made.
    _validate_download_url(url)

    # A normal browser-like User-Agent makes the client identifiable to the service.
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    # Use verified HTTPS and validate every redirect destination.
    opener = urllib.request.build_opener(
        _SafeRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )

    # Download in chunks with a hard size ceiling to avoid unbounded responses.
    total = 0
    chunks: list[bytes] = []
    with opener.open(request, timeout=90) as response:
        _validate_download_url(response.geturl())
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Download exceeded safety limit: {url}")
            chunks.append(chunk)
    # Decode the collected UTF-8 bytes only after the bounded download is complete.
    return json.loads(b"".join(chunks).decode("utf-8"))


def _cache_is_fresh(path: str, max_age_days: int) -> bool:
    # No cache file means there is nothing to reuse.
    if not os.path.exists(path):
        return False

    # Compare the file modification time with the allowed cache age.
    modified = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    return datetime.now(timezone.utc) - modified <= timedelta(days=max_age_days)


def _cached_download(name: str, url: str, max_age_days: int, max_bytes: int) -> object:
    # Create the cache folder on first use.
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name)

    # Fresh cached catalogs are reused so normal daily runs avoid unnecessary downloads.
    if _cache_is_fresh(path, max_age_days):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # Download to memory, then write through a temporary file before replacing the cache.
    # os.replace() is atomic on the same filesystem, so a crash is less likely to leave
    # a half-written cache file behind.
    data = _download_json(url, max_bytes)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(temporary, path)
    return data


# Load Cardmarket singles + non-singles catalogs.  The latter helps distinguish
# similarly named TCG/OCG releases when card membership alone is not enough.
def load_cardmarket_catalogs(progress: Progress = print) -> tuple[list[dict], list[dict]]:
    progress("Loading Cardmarket product catalogs...")
    singles_data = _cached_download(
        "products_singles_3.json",
        CARDMARKET_SINGLES_URL,
        CARDMARKET_CATALOG_MAX_AGE_DAYS,
        MAX_CARDMARKET_BYTES,
    )
    nonsingles_data = _cached_download(
        "products_nonsingles_3.json",
        CARDMARKET_NONSINGLES_URL,
        CARDMARKET_CATALOG_MAX_AGE_DAYS,
        MAX_CARDMARKET_BYTES,
    )
    singles = singles_data.get("products", []) if isinstance(singles_data, dict) else []
    nonsingles = nonsingles_data.get("products", []) if isinstance(nonsingles_data, dict) else []
    if not singles:
        raise ValueError("Cardmarket singles catalog contains no products.")
    if not nonsingles:
        raise ValueError("Cardmarket non-singles catalog contains no products.")
    return singles, nonsingles


# -----------------------------------------------------------------------------
# YGOPRODeck identification
# -----------------------------------------------------------------------------
def _records(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return [row for row in payload["data"] if isinstance(row, dict)]
        if "name" in payload:
            return [payload]
    return []


def _ygopro_get(url: str) -> object:
    # Small delay keeps repeated calls comfortably below YGOPRODeck's documented limit.
    time.sleep(YGOPRO_REQUEST_PAUSE_SECONDS)
    return _download_json(url, MAX_YGOPRO_BYTES)


def _identify_name_batch(cards: list[dict]) -> dict[str, list[dict]]:
    """Fetch exact English card records in small batches and index by name."""
    result: dict[str, list[dict]] = defaultdict(list)

    # Deduplicate card names first so repeated copies do not create repeated API queries.
    unique_names = []
    seen = set()
    for card in cards:
        name = _clean_text(card.get("english_name"))
        key = normalize_text(name)
        if name and key not in seen:
            seen.add(key)
            unique_names.append(name)

    # Ask for up to 10 names per request to reduce total network round-trips.
    for start in range(0, len(unique_names), 10):
        batch = unique_names[start : start + 10]
        query = urllib.parse.urlencode({"name": "|".join(batch)})
        try:
            payload = _ygopro_get(f"{YGOPRO_CARDINFO_URL}?{query}")
        except Exception:
            # Individual code lookup below is the fallback; a single awkward
            # card name should not make the entire matching run fail.
            continue
        for row in _records(payload):
            result[normalize_text(row.get("name"))].append(row)
    return result


def _choose_set_printing(card: dict, card_records: list[dict]) -> tuple[dict | None, str | None]:
    # Convert the human inventory fields to the same normalized form used for API data.
    wanted_code = normalize_card_code(card.get("card_code"))
    wanted_name = normalize_text(card.get("english_name"))
    wanted_rarity = _rarity_key(card.get("rarity"))

    # First collect every YGOPRODeck printing matching both card name and set code.
    candidates: list[dict] = []
    for record in card_records:
        if wanted_name and normalize_text(record.get("name")) != wanted_name:
            continue
        for printing in record.get("card_sets") or []:
            if normalize_card_code(printing.get("set_code")) == wanted_code:
                candidates.append(
                    {
                        "name": record.get("name"),
                        "set_name": printing.get("set_name"),
                        "set_code": printing.get("set_code"),
                        "set_rarity": printing.get("set_rarity"),
                    }
                )

    if not candidates:
        return None, None
    # If the same code has more than one variant, rarity is the next discriminator.
    rarity_matches = [
        row for row in candidates if _rarity_key(row.get("set_rarity")) == wanted_rarity
    ]
    if len(rarity_matches) == 1:
        return rarity_matches[0], None
    if len(candidates) == 1:
        warning = None
        if wanted_rarity and _rarity_key(candidates[0].get("set_rarity")) != wanted_rarity:
            warning = (
                f"Set code/name matched, but inventory rarity '{card.get('rarity')}' "
                f"differs from YGOPRODeck rarity '{candidates[0].get('set_rarity')}'."
            )
        return candidates[0], warning
    if rarity_matches:
        return rarity_matches[0], None
    return None, (
        f"Set code {wanted_code} has multiple printings for this card and rarity "
        f"'{card.get('rarity')}' did not disambiguate them."
    )


# Identify each pending inventory row with YGOPRODeck.  This is only called for
# rows not satisfied by a manual override or a valid cached match.
def identify_cards(cards: list[dict], progress: Progress = print) -> dict[str, dict]:
    """Identify inventory rows with YGOPRODeck using name batches + code fallback."""
    progress(f"Identifying {len(cards)} new/changed inventory rows with YGOPRODeck...")
    by_name = _identify_name_batch(cards)
    identified: dict[str, dict] = {}

    for index, card in enumerate(cards, start=1):
        card_id = card["card_id"]
        records = by_name.get(normalize_text(card.get("english_name")), [])
        printing, warning = _choose_set_printing(card, records)

        if printing is None:
            # Exact set-code lookup is slower, so use it only for rows the
            # batched name endpoint could not settle.
            code = normalize_card_code(card.get("card_code"))
            query = urllib.parse.urlencode({"setcode": code})
            try:
                payload = _ygopro_get(f"{YGOPRO_CARDSETSINFO_URL}?{query}")
                fallback_records = _records(payload)
            except Exception as exc:
                identified[card_id] = {
                    "status": UNRESOLVED,
                    "reason": f"YGOPRODeck could not identify set code {code}: {exc}",
                }
                continue

            fake_records = []
            for row in fallback_records:
                fake_records.append(
                    {
                        "name": row.get("name"),
                        "card_sets": [
                            {
                                "set_name": row.get("set_name"),
                                "set_code": row.get("set_code"),
                                "set_rarity": row.get("set_rarity"),
                            }
                        ],
                    }
                )
            printing, warning = _choose_set_printing(card, fake_records)

        if printing is None:
            identified[card_id] = {
                "status": AMBIGUOUS if warning else UNRESOLVED,
                "reason": warning or (
                    f"No YGOPRODeck printing matched name '{card.get('english_name')}' "
                    f"and set code {normalize_card_code(card.get('card_code'))}."
                ),
            }
            continue

        identified[card_id] = {
            "status": "IDENTIFIED",
            "reason": warning or "Printing identified from card name and set code.",
            "ygopro_name": printing.get("name"),
            "ygopro_set_name": printing.get("set_name"),
            "ygopro_rarity": printing.get("set_rarity"),
        }
        if index % 50 == 0:
            progress(f"  identified {index}/{len(cards)} rows")
    return identified


# -----------------------------------------------------------------------------
# Set/expansion resolution
# -----------------------------------------------------------------------------
# We first determine the YGOPRODeck set, then infer the corresponding Cardmarket
# expansion by comparing set membership and sealed-product naming evidence.
def _set_cache_path(set_name: str) -> str:
    # Use a short deterministic hash so arbitrary set names become safe filenames.
    digest = hashlib.sha1(set_name.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"ygopro_set_{digest}.json")


def _fetch_ygopro_set(set_name: str) -> list[dict]:
    """Fetch and locally cache all YGOPRODeck cards belonging to one set."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _set_cache_path(set_name)

    # Once a complete set response is cached, reuse it instead of calling YGOPRODeck again.
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return _records(payload)

    # Request every card in the set. Some API responses are paginated.
    url = f"{YGOPRO_CARDINFO_URL}?{urllib.parse.urlencode({'cardset': set_name})}"
    rows: list[dict] = []
    pages = 0
    # Follow YGOPRODeck's next_page links, with a defensive maximum page count.
    while url and pages < 25:
        payload = _ygopro_get(url)
        rows.extend(_records(payload))
        pages += 1
        next_page = None
        if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
            next_page = payload["meta"].get("next_page")
        if next_page:
            _validate_download_url(str(next_page))
            url = str(next_page)
        else:
            url = ""

    # Save the assembled paginated response as one local JSON file for future runs.
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"data": rows}, fh, ensure_ascii=False)
    return rows


def _base_product_name(value) -> str:
    """Remove Cardmarket's trailing variant label when present."""
    text = _clean_text(value)
    text = re.sub(r"\s*\(V\.\s*\d+\s*-\s*[^)]*\)\s*$", "", text, flags=re.I)
    return text.strip()


# Convert the downloaded Cardmarket catalog lists into lookup dictionaries.  The
# indexes make later set and product matching much faster and easier to read.
def _build_catalog_indexes(singles: list[dict], nonsingles: list[dict]) -> dict:
    expansion_names: dict[int, set[str]] = defaultdict(set)
    name_to_expansions: dict[str, set[int]] = defaultdict(set)
    singles_by_expansion: dict[int, list[dict]] = defaultdict(list)
    nonsingle_names: dict[int, list[str]] = defaultdict(list)

    # Singles build the main indexes used for card-name/expansion matching.
    for row in singles:
        try:
            expansion_id = int(row.get("idExpansion"))
        except (TypeError, ValueError):
            continue
        name = normalize_text(_base_product_name(row.get("name")))
        if not name:
            continue
        expansion_names[expansion_id].add(name)
        name_to_expansions[name].add(expansion_id)
        singles_by_expansion[expansion_id].append(row)

    # Non-singles (sealed products) provide extra set-name evidence for TCG/OCG ties.
    for row in nonsingles:
        try:
            expansion_id = int(row.get("idExpansion"))
        except (TypeError, ValueError):
            continue
        name = _clean_text(row.get("name"))
        if name:
            nonsingle_names[expansion_id].append(name)

    return {
        "expansion_names": expansion_names,
        "name_to_expansions": name_to_expansions,
        "singles_by_expansion": singles_by_expansion,
        "nonsingle_names": nonsingle_names,
    }


def _set_member_names(set_name: str, rows: list[dict]) -> set[str]:
    names: set[str] = set()
    wanted = normalize_text(set_name)
    for card in rows:
        belongs = any(
            normalize_text(printing.get("set_name")) == wanted
            for printing in (card.get("card_sets") or [])
        )
        if belongs:
            name = normalize_text(card.get("name"))
            if name:
                names.add(name)
    return names


def _nonsingle_similarity(set_name: str, expansion_id: int, indexes: dict) -> float:
    target = normalize_text(set_name)
    names = indexes["nonsingle_names"].get(expansion_id, [])
    return max(
        (SequenceMatcher(None, target, normalize_text(name)).ratio() for name in names),
        default=0.0,
    )


# Score candidate Cardmarket expansions for one YGOPRODeck set.  When evidence
# is not decisive, return AMBIGUOUS/UNRESOLVED instead of guessing.
def infer_expansion(set_name: str, set_card_names: set[str], indexes: dict) -> dict:
    """Infer Cardmarket idExpansion from set membership and sealed-product names."""
    # Count how many cards from the YGOPRODeck set also occur in each Cardmarket expansion.
    counts: Counter[int] = Counter()
    for name in set_card_names:
        for expansion_id in indexes["name_to_expansions"].get(name, ()):
            counts[expansion_id] += 1

    if not counts:
        return {
            "status": UNRESOLVED,
            "reason": "No Cardmarket expansion shared card names with this YGOPRODeck set.",
        }

    ranked = counts.most_common(8)
    best_overlap = ranked[0][1]
    overlap_tied = [exp for exp, count in ranked if count == best_overlap]
    set_size = max(len(set_card_names), 1)

    # Sealed/non-single names are especially useful for distinguishing a TCG
    # set from an OCG set with nearly identical card membership.
    similarities = {
        exp: _nonsingle_similarity(set_name, exp, indexes) for exp in overlap_tied
    }
    ordered_ties = sorted(overlap_tied, key=lambda exp: similarities[exp], reverse=True)
    chosen = ordered_ties[0]
    chosen_similarity = similarities[chosen]
    second_similarity = similarities[ordered_ties[1]] if len(ordered_ties) > 1 else 0.0

    if len(overlap_tied) > 1:
        if not (
            chosen_similarity >= 0.90
            and (chosen_similarity - second_similarity >= 0.05 or chosen_similarity >= 0.99)
        ):
            details = ", ".join(
                f"{exp} ({best_overlap} shared, name score {similarities[exp]:.2f})"
                for exp in ordered_ties[:4]
            )
            return {
                "status": AMBIGUOUS,
                "reason": f"Multiple Cardmarket expansions fit '{set_name}': {details}.",
                "candidates": ordered_ties[:8],
            }

    coverage = best_overlap / set_size
    if best_overlap < 2 and not (coverage >= 0.50 and chosen_similarity >= 0.85):
        return {
            "status": UNRESOLVED,
            "reason": (
                f"Expansion evidence was too weak: only {best_overlap}/{set_size} "
                f"YGOPRODeck card names overlapped Cardmarket expansion {chosen}."
            ),
            "candidates": [exp for exp, _ in ranked[:5]],
        }

    confidence = min(
        0.99,
        0.55 + 0.30 * min(coverage, 1.0) + 0.14 * chosen_similarity,
    )
    return {
        "status": MATCHED,
        "expansion_id": chosen,
        "confidence": confidence,
        "reason": (
            f"Set '{set_name}' mapped to Cardmarket expansion {chosen} using "
            f"{best_overlap}/{set_size} shared card names"
            + (f" and sealed-product name similarity {chosen_similarity:.2f}." if chosen_similarity else ".")
        ),
    }


def _variant_rarity(product_name: str) -> str:
    match = re.search(r"\(V\.\s*\d+\s*-\s*([^)]*)\)\s*$", _clean_text(product_name), re.I)
    return _rarity_key(match.group(1)) if match else ""


# Once the expansion is known, find the product with the same normalized card
# name.  Rarity is used only to disambiguate multiple variants where possible.
def match_product(english_name: str, rarity: str, expansion_id: int, indexes: dict) -> dict:
    # Restrict product matching to the already-resolved expansion. This dramatically
    # reduces false positives compared with searching the entire Cardmarket catalog.
    products = indexes["singles_by_expansion"].get(expansion_id, [])
    wanted = normalize_text(english_name)
    candidates = [
        row for row in products
        if normalize_text(_base_product_name(row.get("name"))) == wanted
    ]

    if len(candidates) == 1:
        row = candidates[0]
        return {
            "status": MATCHED,
            "product_id": int(row["idProduct"]),
            "confidence": 1.0,
            "reason": f"Unique exact card-name match inside Cardmarket expansion {expansion_id}.",
            "candidates": [int(row["idProduct"])],
        }

    if len(candidates) > 1:
        wanted_rarity = _rarity_key(rarity)
        rarity_matches = [row for row in candidates if _variant_rarity(row.get("name")) == wanted_rarity]
        if len(rarity_matches) == 1:
            row = rarity_matches[0]
            return {
                "status": MATCHED,
                "product_id": int(row["idProduct"]),
                "confidence": 0.98,
                "reason": (
                    f"Card name had multiple products in expansion {expansion_id}; "
                    f"rarity '{rarity}' selected the unique variant."
                ),
                "candidates": [int(x["idProduct"]) for x in candidates],
            }
        return {
            "status": AMBIGUOUS,
            "reason": (
                f"{len(candidates)} Cardmarket products in expansion {expansion_id} "
                f"share this card name and rarity '{rarity}' did not select one uniquely."
            ),
            "candidates": [int(x["idProduct"]) for x in candidates],
        }

    # Conservative spelling fallback inside the already-resolved expansion.
    scored = []
    for row in products:
        candidate_name = normalize_text(_base_product_name(row.get("name")))
        if not candidate_name:
            continue
        score = SequenceMatcher(None, wanted, candidate_name).ratio()
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored:
        best_score, best_row = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= 0.985 and best_score - second_score >= 0.03:
            return {
                "status": MATCHED,
                "product_id": int(best_row["idProduct"]),
                "confidence": 0.93,
                "reason": (
                    f"Near-exact card-name match inside expansion {expansion_id} "
                    f"(similarity {best_score:.3f})."
                ),
                "candidates": [int(best_row["idProduct"])],
            }

    return {
        "status": UNRESOLVED,
        "reason": (
            f"Cardmarket expansion {expansion_id} was resolved, but no product name "
            f"matched '{english_name}'."
        ),
    }


# -----------------------------------------------------------------------------
# Persist/reuse automatic matching decisions
# -----------------------------------------------------------------------------
def _load_cached_match(con: sqlite3.Connection, card: dict) -> dict | None:
    # Reuse a result only when both Card ID and printing fingerprint still match.
    row = con.execute(
        "SELECT * FROM card_matches WHERE card_id = ? AND fingerprint = ?",
        (card["card_id"], _fingerprint(card)),
    ).fetchone()
    return dict(row) if row else None


def _store_match(con: sqlite3.Connection, card: dict, result: dict) -> None:
    # Upsert means one current cached decision per TRICARD row.
    with con:
        con.execute(
            """
            INSERT INTO card_matches (
                card_id, fingerprint, product_id, status, reason, source,
                confidence, ygopro_name, ygopro_set_name, ygopro_rarity,
                cardmarket_expansion_id, candidates_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(card_id) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                product_id=excluded.product_id,
                status=excluded.status,
                reason=excluded.reason,
                source=excluded.source,
                confidence=excluded.confidence,
                ygopro_name=excluded.ygopro_name,
                ygopro_set_name=excluded.ygopro_set_name,
                ygopro_rarity=excluded.ygopro_rarity,
                cardmarket_expansion_id=excluded.cardmarket_expansion_id,
                candidates_json=excluded.candidates_json,
                updated_at=excluded.updated_at
            """,
            (
                card["card_id"],
                _fingerprint(card),
                result.get("product_id"),
                result["status"],
                result["reason"],
                result.get("source"),
                result.get("confidence"),
                result.get("ygopro_name"),
                result.get("ygopro_set_name"),
                result.get("ygopro_rarity"),
                result.get("cardmarket_expansion_id"),
                json.dumps(result.get("candidates", [])),
                _utc_now(),
            ),
        )


def _cached_set_mapping(con: sqlite3.Connection, set_name: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM set_mappings WHERE ygopro_set_name = ?", (set_name,)
    ).fetchone()
    return dict(row) if row else None


def _store_set_mapping(con: sqlite3.Connection, set_name: str, result: dict) -> None:
    with con:
        con.execute(
            """
            INSERT INTO set_mappings (
                ygopro_set_name, cardmarket_expansion_id, status,
                confidence, reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ygopro_set_name) DO UPDATE SET
                cardmarket_expansion_id=excluded.cardmarket_expansion_id,
                status=excluded.status,
                confidence=excluded.confidence,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (
                set_name,
                result.get("expansion_id"),
                result["status"],
                result.get("confidence"),
                result["reason"],
                _utc_now(),
            ),
        )


# Merge matching metadata back into the original inventory dictionary so the
# rest of the application can treat each card as one self-contained record.
def _attach(card: dict, result: dict) -> dict:
    merged = dict(card)
    merged.update(
        {
            "product_id": result.get("product_id"),
            "match_status": result.get("status", UNRESOLVED),
            "match_reason": result.get("reason", "No matching result."),
            "match_source": result.get("source"),
            "match_confidence": result.get("confidence"),
            "ygopro_set_name": result.get("ygopro_set_name"),
            "cardmarket_expansion_id": result.get("cardmarket_expansion_id"),
            "candidate_products": result.get("candidates_json")
            if "candidates_json" in result
            else json.dumps(result.get("candidates", [])),
        }
    )
    return merged


# -----------------------------------------------------------------------------
# MAIN MATCHING ORCHESTRATOR
# -----------------------------------------------------------------------------
# Priority for every inventory row:
#   1) manual Cardmarket ID override,
#   2) valid cached automatic match (same fingerprint),
#   3) fresh YGOPRODeck + Cardmarket matching.
#
# This priority is why daily runs after the first are much faster.
def match_inventory(
    inventory: Iterable[dict],
    con: sqlite3.Connection,
    rematch: bool = False,
    progress: Progress = print,
) -> tuple[list[dict], dict]:
    """Resolve missing product IDs and return inventory rows with match metadata."""
    init_match_tables(con)
    cards = list(inventory)
    if rematch:
        # --rematch discards automatic caches, but the separate manual table is kept.
        with con:
            con.execute("DELETE FROM card_matches")
            con.execute("DELETE FROM set_mappings")

    resolved: dict[str, dict] = {}
    pending: list[dict] = []
    # Manual overrides are authoritative and survive --rematch.
    manual_overrides = _load_manual_overrides(con)
    manual_count = 0
    for card in cards:
        manual_product_id = manual_overrides.get(card["card_id"])
        if manual_product_id is not None:
            resolved[card["card_id"]] = {
                **_manual_result(manual_product_id),
                "candidates_json": json.dumps([manual_product_id]),
            }
            manual_count += 1
            continue

        # If the printing-defining fields are unchanged, reuse the previous
        # automatic decision rather than calling remote sources again.
        cached = None if rematch else _load_cached_match(con, card)
        if cached:
            resolved[card["card_id"]] = cached
        else:
            pending.append(card)

    if pending:
        # Only this smaller pending list goes through the expensive matching path.
        progress(f"Matching {len(pending)} new or changed card rows...")
        # Stage A: identify card printing/set information via YGOPRODeck.
        identified = identify_cards(pending, progress)
        need_catalogs = any(x.get("status") == "IDENTIFIED" for x in identified.values())

        try:
            singles, nonsingles = load_cardmarket_catalogs(progress) if need_catalogs else ([], [])
            indexes = _build_catalog_indexes(singles, nonsingles) if need_catalogs else {}
        except Exception as exc:
            indexes = {}
            for card in pending:
                ident = identified.get(card["card_id"], {})
                if ident.get("status") == "IDENTIFIED":
                    ident = {
                        **ident,
                        "status": ERROR,
                        "reason": f"Cardmarket matching data could not be loaded: {exc}",
                    }
                    identified[card["card_id"]] = ident

        # Stage B: resolve each distinct YGOPRODeck set to a Cardmarket expansion.
        # Doing this once per set avoids repeating the same work for every card.
        set_results: dict[str, dict] = {}
        if indexes:
            set_names = sorted(
                {
                    str(result.get("ygopro_set_name"))
                    for result in identified.values()
                    if result.get("status") == "IDENTIFIED" and result.get("ygopro_set_name")
                }
            )
            progress(f"Resolving {len(set_names)} Yu-Gi-Oh sets to Cardmarket expansions...")
            for set_name in set_names:
                cached_set = None if rematch else _cached_set_mapping(con, set_name)
                if cached_set:
                    result = {
                        "status": cached_set["status"],
                        "expansion_id": cached_set["cardmarket_expansion_id"],
                        "confidence": cached_set["confidence"],
                        "reason": cached_set["reason"],
                    }
                else:
                    try:
                        set_rows = _fetch_ygopro_set(set_name)
                        names = _set_member_names(set_name, set_rows)
                        result = infer_expansion(set_name, names, indexes)
                    except Exception as exc:
                        result = {
                            "status": ERROR,
                            "reason": f"Could not load set membership for '{set_name}': {exc}",
                        }
                    _store_set_mapping(con, set_name, result)
                set_results[set_name] = result

        # Stage C: within the resolved expansion, resolve the exact Cardmarket
        # product.  Uncertain results remain visible as issues instead of guesses.
        for card in pending:
            ident = identified.get(card["card_id"]) or {
                "status": ERROR,
                "reason": "No identification result was produced.",
            }
            if ident.get("status") != "IDENTIFIED":
                final = {**ident, "source": "YGOPRODeck identification"}
            else:
                set_name = ident.get("ygopro_set_name")
                set_result = set_results.get(set_name, {})
                if set_result.get("status") != MATCHED:
                    final = {
                        **ident,
                        "status": set_result.get("status", UNRESOLVED),
                        "reason": set_result.get("reason", "Cardmarket expansion could not be resolved."),
                        "source": "set matching",
                        "confidence": set_result.get("confidence"),
                        "cardmarket_expansion_id": set_result.get("expansion_id"),
                        "candidates": set_result.get("candidates", []),
                    }
                else:
                    expansion_id = int(set_result["expansion_id"])
                    product = match_product(
                        ident.get("ygopro_name") or card.get("english_name"),
                        ident.get("ygopro_rarity") or card.get("rarity"),
                        expansion_id,
                        indexes,
                    )
                    final = {
                        **ident,
                        **product,
                        "source": "YGOPRODeck + Cardmarket catalogs",
                        "confidence": min(
                            float(set_result.get("confidence") or 1.0),
                            float(product.get("confidence") or 1.0),
                        ),
                        "cardmarket_expansion_id": expansion_id,
                    }
            _store_match(con, card, final)
            resolved[card["card_id"]] = {
                **final,
                "candidates_json": json.dumps(final.get("candidates", [])),
            }

    # Reassemble rows in original Excel order and produce updater statistics.
    matched_cards = [_attach(card, resolved[card["card_id"]]) for card in cards]
    counts = Counter(card["match_status"] for card in matched_cards)
    summary = {
        "matched_rows": counts[MATCHED],
        "ambiguous_rows": counts[AMBIGUOUS],
        "unresolved_rows": counts[UNRESOLVED],
        "match_error_rows": counts[ERROR],
        "cached_rows": len(cards) - len(pending) - manual_count,
        "manual_rows": manual_count,
        "rematched_rows": len(pending),
    }
    return matched_cards, summary
