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

# A real results-card href shape: the !1s feature id is the stable part.
HREF = ("https://www.google.com/maps/place/King+Valves/data=!4m7!3m6"
        "!1s0x395e9be1c048f553:0x63bc16554df3418b!8m2!3d23.02!4d72.50")

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
        save_place(conn, PLACE, "valves ahmedabad", HREF)
        keys, hrefs = scraped_places(conn)
        assert _place_key(PLACE, HREF) in keys, keys
        assert HREF in hrefs
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


def test_url_feature_id_is_the_key_and_beats_a_changed_url():
    """Google rewrites the data=/entry= params between visits; the !1s id holds."""
    conn, path = _fresh_db()
    try:
        save_place(conn, PLACE, "q", HREF)
        # Same place, URL re-ordered and re-parameterised the way Google does.
        again = HREF.replace("!4m7!3m6", "!4m9!3m8") + "&entry=ttu"
        save_place(conn, dict(PLACE, Name="King Valves Pvt Ltd"), "q", again)
        assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 1, "url key let a duplicate through"
        assert _place_key(PLACE, HREF) == "0x395e9be1c048f553:0x63bc16554df3418b"
    finally:
        conn.close()
        os.remove(path)


def test_legacy_name_address_rows_are_rekeyed_on_open():
    """Old rows must not reappear as a second row under the new key."""
    conn, path = _fresh_db()
    try:
        legacy = "::".join(("king valves", "12 industrial rd, ahmedabad"))
        conn.execute('INSERT INTO places ("id", "href", "Name") VALUES (?, ?, ?)',
                     (legacy, HREF, "King Valves"))
        conn.commit()
        conn.close()

        conn = open_db(path)                       # migration runs here
        keys, _ = scraped_places(conn)
        assert keys == {"0x395e9be1c048f553:0x63bc16554df3418b"}, keys
        # And the re-scrape now matches, instead of inserting a twin.
        save_place(conn, PLACE, "q", HREF)
        assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 1
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
