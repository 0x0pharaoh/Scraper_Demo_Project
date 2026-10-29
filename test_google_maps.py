# test_google_maps.py
"""Offline checks for the SQLite store in plugins/google_maps.py.

No browser: these cover the persistence and the skip guard, which is what
silently breaks (a wrong key either re-scrapes everything or skips the wrong
business). Extraction itself is verified by running the plugin.

Run: python test_google_maps.py
"""

import os
import tempfile

from plugins.google_maps import (
    GOOGLE_MAPS_FIELDNAMES,
    _place_key,
    open_db,
    save_place,
    scraped_places,
)

PLACE = {
    "Name": "King Valves",
    "Address": "12 Industrial Rd, Ahmedabad",
    "City": "Ahmedabad",
    "Rating": "4.9",
    "ReviewCount": "24",
    "Images": ["https://i/1.jpg", "https://i/2.jpg"],
    "Reviews": [{"Author": "A", "Rating": "5", "Date": "1 week ago", "Review": "Good"}],
}


def _fresh_db():
    path = os.path.join(tempfile.gettempdir(), "gmaps_test.db")
    if os.path.exists(path):
        os.remove(path)
    return open_db(path), path


def test_save_and_skip():
    conn, path = _fresh_db()
    try:
        save_place(conn, PLACE, "valves ahmedabad", "https://maps/x?a=1")
        keys, hrefs = scraped_places(conn)
        assert _place_key(PLACE) in keys, keys
        assert "https://maps/x?a=1" in hrefs
    finally:
        conn.close()
        os.remove(path)


def test_resave_is_idempotent():
    """A second visit must update the row, not add a duplicate business."""
    conn, path = _fresh_db()
    try:
        save_place(conn, PLACE, "q", "https://maps/x")
        richer = dict(PLACE, Reviews=PLACE["Reviews"] * 3)
        save_place(conn, richer, "q", "https://maps/x")
        assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 1
        row = conn.execute('SELECT "Reviews" FROM places').fetchone()
        assert row["Reviews"].count("Author") == 3
    finally:
        conn.close()
        os.remove(path)


def test_same_name_different_address_stays_separate():
    """Chain branches share a name, so the key has to include the address."""
    conn, path = _fresh_db()
    try:
        save_place(conn, PLACE, "q", "https://maps/a")
        branch = dict(PLACE, Address="99 Other Rd, Surat")
        save_place(conn, branch, "q", "https://maps/b")
        assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 2
        keys, _ = scraped_places(conn)
        assert _place_key(PLACE) in keys and _place_key(branch) in keys
    finally:
        conn.close()
        os.remove(path)


def test_nested_fields_survive_the_round_trip():
    conn, path = _fresh_db()
    try:
        save_place(conn, PLACE, "q", None)
        row = conn.execute('SELECT * FROM places').fetchone()
        for field in GOOGLE_MAPS_FIELDNAMES:
            assert field in row.keys(), f"{field} column missing"
        assert "https://i/2.jpg" in row["Images"]
        assert row["query"] == "q"
    finally:
        conn.close()
        os.remove(path)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("All checks passed.")
