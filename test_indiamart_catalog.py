# test_indiamart_catalog.py
"""Offline checks for the pure logic in plugins/indiamart_catalog.py.

No network and no browser: these cover the parts that decide *what* gets
crawled and *when to give up*, which are the pieces that silently corrupt a run
when they drift. Live extraction is verified by running the plugin itself.

Run: python test_indiamart_catalog.py
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

from plugins.indiamart_catalog import (
    CATEGORY_COUNT_RE,
    alias_from_url,
    company_from_fields,
    completed_aliases,
    export_json,
    goto_with_retry,
    open_db,
    parse_price,
    product_id,
    resolve_alias,
    save_company,
)
from plugins.indiamart_products import load_session


def test_alias_from_url():
    assert alias_from_url("https://www.indiamart.com/annu-valve-products/") == "annu-valve-products"
    # A vanity domain has no alias in its URL - it has to come from the payload.
    assert alias_from_url("https://www.aira-euro.com/") is None
    # Non-company indiamart paths must not be mistaken for a minisite.
    assert alias_from_url("https://www.indiamart.com/proddetail/x-123.html") is None
    assert alias_from_url("https://www.indiamart.com/impcat/ball-valves.html") is None
    assert alias_from_url(None) is None


def test_resolve_alias_precedence():
    assert resolve_alias({"companyalias": "a-b", "tscode": "zz"}) == "a-b"
    assert resolve_alias({"tscode": "zz"}) == "zz"
    assert resolve_alias({"catalog_url": "https://www.indiamart.com/foo-bar/"}) == "foo-bar"
    assert resolve_alias({"catalog_url": "https://www.aira-euro.com/"}) is None


def test_category_label_parsing():
    cases = [
        ("Pressure Reducing Valve (11 Products)»", "Pressure Reducing Valve", 11),
        ("Lined Valves (4 Products)»", "Lined Valves", 4),
        ("Solenoid Valve", "Solenoid Valve", None),
    ]
    for label, want_name, want_count in cases:
        match = CATEGORY_COUNT_RE.search(label)
        assert (int(match.group(1)) if match else None) == want_count
        name = CATEGORY_COUNT_RE.sub("", label).replace("»", "").strip(" -–")
        assert name == want_name


def test_parse_price():
    assert parse_price("₹ 2,530", "/Piece") == "₹ 2,530/Piece"
    assert parse_price("₹ 900", None) == "₹ 900"
    assert parse_price(None, "/Piece") is None


def test_custom_domain_identity_is_preserved():
    """The vanity domain stays the company's URL even though we crawl the alias."""
    record = company_from_fields(
        {"companyname": "Aira", "catalog_url": "https://www.aira-euro.com/", "city": "Ahmedabad"},
        "aira-euro-automation",
    )
    assert record["company_url"] == "https://www.aira-euro.com/"
    assert record["indiamart_url"] == "https://www.indiamart.com/aira-euro-automation/"


class _Response:
    def __init__(self, status):
        self.status = status


class _FakePage:
    """Minimal stand-in for a Playwright page: records calls and backoff waits."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0
        self.waits = []

    def goto(self, url, **kwargs):
        self.calls += 1
        return _Response(self.statuses.pop(0) if self.statuses else 200)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_goto_with_retry():
    page = _FakePage([200])
    assert goto_with_retry(page, "u").status == 200
    assert page.calls == 1

    # A 404 is final - retrying it just wastes a minute per category.
    page = _FakePage([404])
    assert goto_with_retry(page, "u").status == 404
    assert page.calls == 1

    # A 429 is transient: back off and retry, and the ladder must escalate.
    page = _FakePage([429, 429, 200])
    assert goto_with_retry(page, "u").status == 200
    assert page.calls == 3
    assert page.waits == [10000, 30000]

    # A sustained block gives up rather than hammering the host forever.
    page = _FakePage([429] * 9)
    assert goto_with_retry(page, "u") is None
    assert page.calls == 5


def _fresh_db():
    path = os.path.join(tempfile.gettempdir(), "im_catalog_test.db")
    if os.path.exists(path):
        os.remove(path)
    return open_db(path), path


def test_only_complete_companies_are_marked_done():
    """A company is 'done' only with products AND no skipped categories.

    This is what stops the crawl re-scraping finished companies, and equally
    what stops a throttled-out company being written off as finished.
    """
    conn, path = _fresh_db()
    try:
        assert save_company(conn, {
            "alias": "acme", "company": "Acme", "categories": ["Valves"],
            "products": [{"name": "P1", "product_url": "https://x/p1.html"}],
        }) == "done"
        assert save_company(conn, {
            "alias": "beta", "company": "Beta", "skipped_categories": ["Y"],
            "products": [{"name": "Q1", "product_url": "https://x/q1.html"}],
        }) == "partial"
        assert save_company(conn, {"alias": "gamma", "company": "Gamma", "products": []}) == "partial"
        assert completed_aliases(conn) == {"acme"}
    finally:
        conn.close()
        os.remove(path)


def test_resave_is_idempotent_and_partials_can_complete():
    conn, path = _fresh_db()
    try:
        company = {
            "alias": "acme", "company": "Acme",
            "products": [{"name": "P1", "product_url": "https://x/p1.html"},
                         {"name": "P2", "product_url": None}],
        }
        save_company(conn, company)
        save_company(conn, company)  # a re-run must not duplicate rows
        assert conn.execute(
            "SELECT COUNT(*) c FROM products WHERE alias='acme'").fetchone()["c"] == 2

        # A product with no detail page still needs a stable key.
        assert product_id("acme", {"name": "P2"}) == "acme::p2"

        save_company(conn, {"alias": "beta", "company": "Beta",
                            "skipped_categories": ["Y"],
                            "products": [{"name": "Q1", "product_url": "https://x/q1.html"}]})
        assert save_company(conn, {"alias": "beta", "company": "Beta",
                                   "skipped_categories": [],
                                   "products": [{"name": "Q1", "product_url": "https://x/q1.html"},
                                                {"name": "Q2", "product_url": "https://x/q2.html"}]}) == "done"
        assert completed_aliases(conn) == {"acme", "beta"}
        assert conn.execute(
            "SELECT COUNT(*) c FROM products WHERE alias='beta'").fetchone()["c"] == 2
    finally:
        conn.close()
        os.remove(path)


def test_json_export_round_trips_nested_fields():
    conn, path = _fresh_db()
    out = os.path.join(tempfile.gettempdir(), "im_catalog_test.json")
    try:
        save_company(conn, {
            "alias": "acme", "company": "Acme", "categories": ["Valves"],
            "products": [{"name": "P1", "product_url": "https://x/p1.html",
                          "specs": {"Brand": "Acme"}, "images": ["https://i/1.jpg"]}],
        })
        data = export_json(conn, out)
        product = data[0]["products"][0]
        assert product["specs"] == {"Brand": "Acme"}
        assert product["images"] == ["https://i/1.jpg"]
    finally:
        conn.close()
        os.remove(path)
        if os.path.exists(out):
            os.remove(out)


def _session_file(cookies):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies, "session_data": {"a": "1"},
                   "browser_storage": {"b": "2"}}, f)
    return path


def test_expired_cookies_are_dropped_and_dead_sessions_refused():
    """A stale export must fail here, not twenty minutes into a crawl."""
    iso = "%Y-%m-%dT%H:%M:%S.000Z"
    live = (datetime.now() + timedelta(days=30)).strftime(iso)
    dead = (datetime.now() - timedelta(days=1)).strftime(iso)
    auth = [{"name": n, "value": "v", "expires": live} for n in ("im_iss", "ImeshVisitor")]

    path = _session_file(auth + [{"name": "iploc", "value": "v", "expires": dead}])
    try:
        cookies, storage = load_session(path)
        assert [c["name"] for c in cookies] == ["im_iss", "ImeshVisitor"], cookies
        # Both halves of the export get seeded, not just session_data.
        assert storage == {"a": "1", "b": "2"}, storage
    finally:
        os.remove(path)

    # im_iss expired: the login is gone even though ImeshVisitor still looks fine.
    path = _session_file([dict(c, expires=dead if c["name"] == "im_iss" else live) for c in auth])
    try:
        try:
            load_session(path)
        except RuntimeError as e:
            assert "im_iss" in str(e), e
        else:
            raise AssertionError("a dead session was accepted")
    finally:
        os.remove(path)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("All checks passed.")
