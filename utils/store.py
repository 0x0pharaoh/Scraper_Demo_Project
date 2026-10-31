"""URL-keyed SQLite store and timestamped export paths, shared by every plugin.

Every plugin had the same two gaps. Exports went to a fixed filename, so each
run overwrote the last one's results. And dedup lived in a per-run ``seen``
set, so a re-run happily re-collected and re-wrote entries it already had.

A table keyed on the entry URL closes both: the same URL can never become a
second row, in this run or any later one, and the export carries a timestamp so
runs accumulate instead of clobbering each other.

``google_maps`` and ``indiamart_catalog`` already had their own stores with
richer per-row schemas; they keep them and take only ``export_path`` from here.
"""

import os
import re
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Column names are interpolated into DDL because SQLite has no placeholder for
# an identifier. They come from module-level constants in the plugins, never
# from scraped data, and this guard keeps it that way.
_SAFE_NAME = re.compile(r"\w[\w ]*\Z")


def slug(text):
    return re.sub(r"\W+", "_", (text or "").lower()).strip("_") or "query"


def export_path(query, suffix, ext, when=None):
    """``static/<query>_<suffix>_<ddmmyy_HHMMSS>.<ext>`` - one file per run.

    Second precision, not day: two runs of the same query an hour apart are two
    results, and a day-stamped name silently throws the first one away.

    Anchored to the project rather than the working directory, so launching the
    scraper from elsewhere cannot scatter output into a stray ``static/``.
    """
    stamp = (when or datetime.now()).strftime("%d%m%y_%H%M%S")
    return os.path.join(STATIC_DIR, f"{slug(query)}_{suffix}_{stamp}.{ext.lstrip('.')}")


def db_path(name):
    """One database per plugin, beside the exports and anchored the same way."""
    return os.path.join(STATIC_DIR, name if name.endswith(".db") else f"{name}.db")


def _check(names):
    bad = [n for n in names if not _SAFE_NAME.match(n)]
    if bad:
        raise ValueError(f"unsafe column names: {bad}")
    return names


def open_table(path, table, columns):
    """Open ``path``, creating ``table`` with ``url`` as the primary key."""
    _check([table, *columns])
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cols = ", ".join(f'"{c}" TEXT' for c in columns)
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{table}" '
        f'("url" TEXT PRIMARY KEY, {cols}, "query" TEXT, "updated_at" TEXT)'
    )
    conn.execute(f'CREATE INDEX IF NOT EXISTS "{table}_query" ON "{table}"("query")')
    conn.commit()
    return conn


def upsert(conn, table, url, row, query=None):
    """Insert one entry, or refresh it in place if that URL is already stored.

    Called per entry rather than once at the end of a run: a run that dies on
    entry 30 of 40 keeps the first 29 instead of losing the lot.
    """
    if not url:
        raise ValueError("upsert needs a url - it is the identity")
    cols = _check(list(row))
    fields = ["url", *cols, "query", "updated_at"]
    updates = ", ".join(f'"{c}"=excluded."{c}"' for c in fields[1:])
    with conn:
        conn.execute(
            f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in fields)}) '
            f'VALUES ({", ".join("?" * len(fields))}) '
            f'ON CONFLICT("url") DO UPDATE SET {updates}',
            [url, *(row[c] for c in cols), query,
             datetime.now().isoformat(timespec="seconds")],
        )


def known_urls(conn, table):
    """Every URL already stored, so a later run can skip them before scraping."""
    return {r[0] for r in conn.execute(f'SELECT "url" FROM "{table}"') if r[0]}
