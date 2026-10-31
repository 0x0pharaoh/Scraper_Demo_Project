# test_store.py
"""Checks for utils/store.py - the URL dedup and the timestamped export names.

These are the two things the plugins now depend on: an entry URL can never
become a second row, and two runs of one query never write the same filename.

Run: python test_store.py
"""

import os
import tempfile
from datetime import datetime

from utils.store import db_path, export_path, known_urls, open_table, slug, upsert

URL = "https://www.indiamart.com/proddetail/ball-valve-123.html"


def _conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return open_table(path, "products", ["name", "price"]), path


def test_same_url_never_becomes_a_second_row():
    conn, path = _conn()
    try:
        upsert(conn, "products", URL, {"name": "Ball Valve", "price": "100"}, "ball valves")
        upsert(conn, "products", URL, {"name": "Ball Valve", "price": "120"}, "ball valves")
        rows = list(conn.execute('SELECT * FROM products'))
        assert len(rows) == 1, f"duplicate rows for one url: {len(rows)}"
        # The re-insert refreshes the row rather than being dropped.
        assert rows[0]["price"] == "120", rows[0]["price"]
        assert known_urls(conn, "products") == {URL}
    finally:
        conn.close()
        os.remove(path)


def test_known_urls_survives_a_reopen():
    """The point of the database: the *next* run must skip what this one stored."""
    conn, path = _conn()
    try:
        upsert(conn, "products", URL, {"name": "a", "price": "1"}, "q")
        conn.close()
        again = open_table(path, "products", ["name", "price"])
        assert known_urls(again, "products") == {URL}
        again.close()
    finally:
        os.remove(path)


def test_upsert_refuses_a_missing_url():
    conn, path = _conn()
    try:
        for bad in (None, ""):
            try:
                upsert(conn, "products", bad, {"name": "x", "price": "1"}, "q")
            except ValueError:
                pass
            else:
                raise AssertionError(f"accepted {bad!r} as a url")
    finally:
        conn.close()
        os.remove(path)


def test_column_names_are_checked_before_reaching_ddl():
    for bad in ('x" , "y', "a;DROP TABLE t", "", "-"):
        try:
            open_table(db_path("unused_check"), "t", [bad])
        except ValueError:
            continue
        raise AssertionError(f"accepted unsafe column {bad!r}")


def test_export_name_is_unique_per_second_and_carries_the_query():
    a = export_path("ball valves", "indiamart", "csv", when=datetime(2026, 10, 29, 14, 3, 9))
    b = export_path("ball valves", "indiamart", "csv", when=datetime(2026, 10, 29, 14, 3, 10))
    assert os.path.basename(a) == "ball_valves_indiamart_291026_140309.csv", a
    assert a != b, "two runs one second apart collided"
    # Absolute and project-anchored, so the working directory cannot move it.
    assert os.path.isabs(a) and os.path.dirname(a).endswith("static"), a


def test_slug_survives_punctuation_and_emptiness():
    assert slug("Ball Valves & Fittings!") == "ball_valves_fittings"
    assert slug("") == "query" and slug(None) == "query"


def test_products_plugin_stores_one_row_per_product_url():
    """indiamart_products: re-scraping a company must not duplicate its products."""
    from plugins.indiamart_products import DB_COLUMNS, save_products

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = open_table(path, "products", DB_COLUMNS)
    record = {
        "company": "Acme Valves", "company_url": "https://x/acme", "city": "Pune",
        "state": "MH",
        "products": [
            {"name": "Ball Valve", "product_url": URL, "price": "100",
             "specs": {"size": "2 inch"}, "images": ["https://i/1.jpg"]},
            # No detail page: falls back to the name, and must still store.
            {"name": "Gate Valve", "product_url": None, "price": "200",
             "specs": {}, "images": []},
        ],
    }
    try:
        save_products(conn, record, "ball valves")
        save_products(conn, record, "ball valves")      # the re-scrape
        rows = {r["url"]: r for r in conn.execute("SELECT * FROM products")}
        assert len(rows) == 2, f"expected 2 products, got {len(rows)}"
        assert URL in rows, list(rows)
        assert rows[URL]["company"] == "Acme Valves"
        assert rows[URL]["specs"] == '{"size": "2 inch"}', rows[URL]["specs"]
        assert "gate valve" in rows, list(rows)          # name fallback
    finally:
        conn.close()
        os.remove(path)


def test_indiamart_plugin_keys_listings_on_product_url():
    from plugins.indiamart import entry_key

    PROD = "https://www.indiamart.com/proddetail/ball-valve-9.html"
    # The product URL wins, and IndiaMART's per-search tracking string is
    # stripped - otherwise the same listing keys as new on every run.
    assert entry_key({"Product URL": PROD + "?pos=1&kwd=ball+valves", "URL": "https://x/acme"}) == PROD
    # No detail page: falls back to the supplier URL, then to name + location.
    assert entry_key({"Product URL": "N/A", "URL": "https://www.indiamart.com/acme/"}) == "https://www.indiamart.com/acme/"
    assert entry_key({"Company Name": "Acme Co", "Location": "Pune"}) == "acme co::pune"


def test_indiamart_db_row_maps_supplier_url_clear_of_the_pk():
    """A plain "URL" column would collide with the url primary key in SQLite."""
    from plugins.indiamart import _DB_COLUMNS, db_row

    row = db_row({"Company Name": "Acme", "Location": "Pune", "Phone": "N/A",
                  "URL": "https://x/acme", "Product": "Ball Valve"})
    assert set(row) == set(_DB_COLUMNS), (sorted(row), sorted(_DB_COLUMNS))
    assert row["Supplier URL"] == "https://x/acme"
    assert not any(c.lower() == "url" for c in _DB_COLUMNS), _DB_COLUMNS


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("All checks passed.")
