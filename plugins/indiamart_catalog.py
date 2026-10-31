# indiamart_catalog.py
"""IndiaMART company-catalog scraper.

Takes a product search, follows every result to the *company* behind it, and
extracts that company's entire product catalog rather than the handful of
products the search card previews.

How this differs from ``indiamart_products.py``
-----------------------------------------------
That plugin reads the search listing itself: four carousel products per card,
with gallery images harvested from the story popup. Verified against the live
site, that popup only ever serves the primary product's images, so sibling
products end up holding just their thumbnail.

This plugin goes one level deeper instead. Every company - including the ones
whose ``catalog_url`` is a vanity domain like ``https://www.aira-euro.com/`` -
also publishes the same catalog under ``indiamart.com/<companyalias>/``, on one
shared template. Normalising to that alias means a single extractor covers both
kinds of company URL, and the category pages carry every field (name, price,
MOQ, specs, brochure, and all product images) directly in the DOM.

The practical result: this scraper never clicks anything. No carousel, no
gallery popup, no "show more". That is also the popup defence - there is almost
no interaction surface for a modal to interrupt.

Rate limiting
-------------
A catalog costs one request per category, and a large seller has fifty-plus of
them. Measured against the live site, roughly fifty rapid requests is the point
where IndiaMART starts answering 429, and that throttle then persists for
several minutes - long enough to outlast any sane retry ladder. So the run is
paced rather than merely retried: ``delay_ms`` between category pages and
``company_cooldown_ms`` between companies keep the rate under the threshold,
with ``goto_with_retry`` as the backstop.

One uncapped company is already close to a full window's budget, which is why
this scrapes **one company per run** by default. Progress lives in a single
SQLite database shared by every query and every plugin
(``static/indiamart_catalog.db``): a company is marked ``done`` only when it
finished with no skipped categories, and a ``done`` company is never crawled
again - not by a later run, and not by a different search that happens to
surface the same seller. Anything left ``partial`` is picked up next time. So
the way to scrape everything is simply to run the same command repeatedly; each
run costs about one throttle window and permanently advances the total.
"""

import json
import os
import re
import sqlite3
from datetime import datetime
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from utils.logger import get_logger
from utils.store import export_path, slug

# Reuse the session/text helpers rather than duplicating them. They are already
# exercised against the live site by the sibling plugin.
from plugins.indiamart_products import (
    DESKTOP_UA,
    build_search_url,
    clean_text,
    clean_url,
    load_session,
    normalize_image,
    normalize_key,
    promote_specs,
    save_to_json,
    seed_local_storage,
    _safe,
)

logger = get_logger("indiamart_catalog")
description = "Scrape a company's full IndiaMART product catalog (all categories) for a search query."

MINISITE_ROOT = "https://www.indiamart.com"

# One database for every plugin and every query, so a company scraped under one
# search is never scraped again under another. Anchored to the project rather
# than the working directory: a relative path would quietly create a second,
# empty database whenever the scraper is launched from elsewhere, and the
# "already done" guard would then find nothing.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BASE_DIR, "static", "indiamart_catalog.db")

# --- Category index (<alias>/products.html) --------------------------------
CATEGORY_CARD = ".catindex-category-card"
CATEGORY_NAME = "[class*=cat-name]"

# --- Category page product cards -------------------------------------------
# Semantic, template-owned class names. Chosen over the positional XPaths
# because the minisite injects promo blocks between product rows, which shifts
# every div index but leaves these untouched.
ITEM = ".udg-category-item"
ITEM_TITLE = "[class*=udg-category-item__title]"
ITEM_LINK = "a[href*='proddetail']"
ITEM_PRICE_AMOUNT = "[class*=udg-category-item__priceAmount]"
ITEM_PRICE_UNIT = "[class*=udg-category-item__priceUnit]"
ITEM_MOQ = "[class*=udg-category-item__moq]"
ITEM_BROCHURE = "[class*=udg-category-item__brochure] a[href], a[href$='.pdf']"
ITEM_SPEC_ROW = "[class*=udg-category-item__specRow]"
ITEM_SPEC_KEY = "[class*=udg-category-item__specKey]"
ITEM_SPEC_VAL = "[class*=udg-category-item__specVal]"
ITEM_DESC = "[class*=udg-category-item__sdescRich]"
ITEM_STAGE_IMG = "[class*=udg-category-item__stage] img"

# A category page has never returned more than this in probing. Hitting it
# exactly is a truncation signal worth surfacing rather than silently accepting.
CATEGORY_ITEM_CAP = 40

# Many category links are labelled "Pressure Reducing Valve (11 Products)>>". The
# count is the site's own tally for that category, which makes it a free
# completeness check on the extractor - and it does not belong in the name.
CATEGORY_COUNT_RE = re.compile(r"\(\s*(\d+)\s*products?\s*\)", re.I)

# Popup/enquiry widgets. Blocked at the network layer so they never mount.
POPUP_SCRIPT_PATTERNS = ("enquiry", "popup", "/lms/", "webpush", "onesignal", "notification")


# ---------------------------------------------------------------------------
# Company identity
# ---------------------------------------------------------------------------

def goto_with_retry(page, url, attempts=5, backoff=(10000, 30000, 60000, 120000)):
    """Navigate, backing off when IndiaMART rate-limits us.

    Crawling a large catalog fires one request per category, and after roughly
    fifty in quick succession the site starts answering 429 - which previously
    wiped out every company after the first. Measured, that throttle outlasts a
    minute, so the ladder climbs to two minutes. Retrying is the safety net;
    the actual fix is the pacing in ``scrape_company`` and the cooldown between
    companies, which keep the request rate under the threshold to begin with.
    """
    for attempt in range(attempts):
        try:
            response = page.goto(url, timeout=60000, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            response = None
        except Exception as e:
            logger.debug(f"Navigation to {url} failed: {e}")
            response = None

        status = response.status if response else None
        if status is not None and status < 400:
            return response
        if status not in (429, 503, 500, None):
            return response  # a real 404 - retrying will not help

        if attempt < attempts - 1:
            wait = backoff[min(attempt, len(backoff) - 1)]
            logger.warning(
                f"HTTP {status} on {url.rsplit('/', 1)[-1]}; waiting {wait // 1000}s before retry "
                f"{attempt + 2}/{attempts}."
            )
            page.wait_for_timeout(wait)
    return None


def alias_from_url(url):
    """Pull the minisite alias out of an indiamart.com company URL."""
    if not url:
        return None
    parts = urlsplit(url)
    if "indiamart.com" not in (parts.netloc or ""):
        return None
    segments = [s for s in (parts.path or "").split("/") if s]
    if not segments:
        return None
    head = segments[0]
    # Skip non-company paths such as /proddetail/... or /impcat/...
    if head in {"proddetail", "impcat", "search.mp", "company"} or head.endswith(".html"):
        return None
    return head


def resolve_alias(fields):
    """Work out the ``indiamart.com/<alias>/`` catalog for a search result.

    The company's own ``catalog_url`` is often a vanity domain, which runs an
    older template with no products index. The alias minisite carries the same
    catalog on the template this scraper understands, so that is what gets
    crawled - while ``catalog_url`` is still reported as the company's URL.
    """
    for key in ("companyalias", "tscode"):
        alias = clean_text(fields.get(key))
        if alias:
            return alias
    return alias_from_url(fields.get("catalog_url") or fields.get("paidurl"))


def company_from_fields(fields, alias):
    """Company-level record built from the search payload."""
    return {
        "company": clean_text(fields.get("companyname")),
        "company_url": clean_url(fields.get("catalog_url") or fields.get("paidurl")),
        "indiamart_url": f"{MINISITE_ROOT}/{alias}/" if alias else None,
        "alias": alias,
        "city": clean_text(fields.get("city")),
        "state": clean_text(fields.get("state")),
        "locality": clean_text(fields.get("locality")),
        "phone": clean_text(fields.get("pns")),
        "rating": fields.get("supplier_rating"),
        "review_count": fields.get("rating_count"),
        "member_since": clean_text(fields.get("memberSince")),
        "gst_number": clean_text(fields.get("gstNumber")),
        "verified_exporter": bool(fields.get("isverifiedexporter")),
        "search_match_count": fields.get("numFound"),
        "categories": [],
        "products": [],
    }


# ---------------------------------------------------------------------------
# Search page
# ---------------------------------------------------------------------------

def read_search_results(page):
    """Read the server-rendered search payload.

    ``__NEXT_DATA__`` is present on first load and already carries the company
    identity for every result, so the company list needs no DOM scraping at all.
    """
    data = _safe(
        lambda: page.evaluate(
            "() => { const e = document.getElementById('__NEXT_DATA__');"
            " return e ? JSON.parse(e.textContent) : null; }"
        )
    )
    if not data:
        logger.warning("Search page carried no __NEXT_DATA__ payload.")
        return []
    try:
        results = data["props"]["pageProps"]["searchResponse"]["results"]
    except (KeyError, TypeError):
        logger.warning("Search payload had an unexpected shape.")
        return []

    out = []
    for entry in results or []:
        fields = entry.get("fields") or {}
        if fields:
            fields.setdefault("numFound", entry.get("numFound"))
            out.append(fields)
    return out


# ---------------------------------------------------------------------------
# Catalog crawling
# ---------------------------------------------------------------------------

def collect_categories(page, alias):
    """Return [(name, url)] for every category in the company's products index."""
    index_url = f"{MINISITE_ROOT}/{alias}/products.html"
    response = goto_with_retry(page, index_url)

    if response is None or response.status >= 400:
        status = response.status if response else "no response"
        logger.warning(f"{index_url} returned {status}; trying the company home page.")
        if goto_with_retry(page, f"{MINISITE_ROOT}/{alias}/") is None:
            logger.warning(f"Company home page unavailable for {alias}.")
            return []

    page.wait_for_timeout(2500)
    dismiss_overlays(page)

    raw = _safe(
        lambda: page.evaluate(
            """(sel) => {
                const cards = [...document.querySelectorAll(sel.card)];
                const out = cards.map(c => {
                    const a = c.querySelector('a[href]');
                    const n = c.querySelector(sel.name);
                    return { url: a ? a.href : null, name: n ? n.innerText.trim() : '' };
                });
                if (out.length) return out;
                // Home-page fallback: category links share the "<alias>/<slug>.html"
                // shape, so pick those and drop the in-page product anchors.
                return [...document.querySelectorAll('a[href]')]
                    .map(a => ({ url: a.href, name: (a.innerText || '').trim() }))
                    .filter(x => /\\/[^/]+\\.html$/.test(x.url));
            }""",
            {"card": CATEGORY_CARD, "name": CATEGORY_NAME},
        ),
        [],
    ) or []

    categories, seen = [], set()
    for item in raw:
        url = clean_url(item.get("url"))
        if not url or f"/{alias}/" not in url or url.rstrip("/").endswith("/products.html"):
            continue
        if url in seen:
            continue
        seen.add(url)
        label = clean_text(item.get("name")) or url.rsplit("/", 1)[-1]
        match = CATEGORY_COUNT_RE.search(label)
        expected = int(match.group(1)) if match else None
        name = clean_text(CATEGORY_COUNT_RE.sub("", label).replace("»", "").strip(" -–")) or label
        categories.append((name, url, expected))
    return categories


def parse_price(amount, unit):
    """Join the split price parts back into '₹ 2,530/Piece'."""
    amount, unit = clean_text(amount), clean_text(unit)
    if not amount:
        return None
    return f"{amount}{unit}" if unit and unit.startswith("/") else " ".join(filter(None, [amount, unit]))


def extract_products(page, category_name):
    """Read every product card on the current category page."""
    cards = _safe(
        lambda: page.evaluate(
            """(sel) => [...document.querySelectorAll(sel.item)].map(c => {
                const one = s => { const n = c.querySelector(s); return n ? n.innerText.trim() : null; };
                const link = c.querySelector(sel.link);
                const specs = [...c.querySelectorAll(sel.specRow)].map(r => {
                    const k = r.querySelector(sel.specKey);
                    const v = r.querySelector(sel.specVal);
                    if (k && v) return [k.innerText.trim(), v.innerText.trim()];
                    // Some rows render as a plain two-cell table row.
                    const cells = [...r.children].map(x => x.innerText.trim());
                    return cells.length >= 2 ? [cells[0], cells[1]] : null;
                }).filter(Boolean);
                const brochure = c.querySelector("a[href$='.pdf']");
                return {
                    name: one(sel.title) || (link ? link.innerText.trim() : null),
                    product_url: link ? link.href : null,
                    amount: one(sel.amount),
                    unit: one(sel.unit),
                    moq: one(sel.moq),
                    description: one(sel.desc),
                    brochure: brochure ? brochure.href : null,
                    specs: specs,
                    images: [...c.querySelectorAll(sel.img)].map(i => i.currentSrc || i.src).filter(Boolean)
                };
            })""",
            {
                "item": ITEM,
                "title": ITEM_TITLE,
                "link": ITEM_LINK,
                "amount": ITEM_PRICE_AMOUNT,
                "unit": ITEM_PRICE_UNIT,
                "moq": ITEM_MOQ,
                "desc": ITEM_DESC,
                "specRow": ITEM_SPEC_ROW,
                "specKey": ITEM_SPEC_KEY,
                "specVal": ITEM_SPEC_VAL,
                "img": ITEM_STAGE_IMG,
            },
        ),
        [],
    ) or []

    products = []
    for card in cards:
        name = clean_text(card.get("name"))
        if not name:
            continue

        specs = {}
        for pair in card.get("specs") or []:
            key, value = clean_text(pair[0]), clean_text(pair[1])
            if key and value and key not in specs:
                specs[key] = value

        images, video_thumbs = [], []
        for raw in card.get("images") or []:
            url = normalize_image(raw)
            if not url:
                continue
            # YouTube stills ride in the same image strip as the product photos.
            bucket = video_thumbs if "youtube.com" in url or "ytimg.com" in url else images
            if url not in bucket:
                bucket.append(url)

        moq = clean_text(card.get("moq")) or ""
        product = {
            "name": name,
            "product_url": clean_url(card.get("product_url")),
            "category": category_name,
            "price": parse_price(card.get("amount"), card.get("unit")),
            "moq": moq.split(":", 1)[-1].strip() if ":" in moq else (moq or None),
            "description": clean_text(card.get("description")),
            "brochure_url": clean_url(card.get("brochure")),
            "specs": specs,
            "images": images,
            "video_thumbnails": video_thumbs,
        }
        product.update(promote_specs(specs))
        products.append(product)

    if len(cards) >= CATEGORY_ITEM_CAP:
        logger.warning(
            f"Category '{category_name}' returned {len(cards)} items, the observed page cap - "
            "it may hold more products than this page lists."
        )
    return products


def scrape_company(page, company, max_categories=None, max_products=None, seen_products=None,
                   delay_ms=1500, throttle_pause_ms=600000, throttle_retries=3, conn=None):
    """Fill ``company['products']`` from its minisite catalog.

    A sustained 429 is treated as "come back later", not as failure: the whole
    point of a full crawl is that it finishes, and abandoning a company because
    the host is briefly throttling would silently leave a hole in the output.
    """
    alias = company.get("alias")
    if not alias:
        logger.warning(f"No minisite alias for {company.get('company')}; skipping catalog.")
        return company

    seen_products = seen_products if seen_products is not None else set()

    categories = []
    for attempt in range(throttle_retries + 1):
        categories = collect_categories(page, alias)
        if categories or attempt == throttle_retries:
            break
        logger.warning(
            f"{alias}: no categories (host is throttling). Sleeping "
            f"{throttle_pause_ms // 60000} min, then retry {attempt + 2}/{throttle_retries + 1}."
        )
        page.wait_for_timeout(throttle_pause_ms)

    if not categories:
        logger.error(f"No categories found for {alias} after {throttle_retries + 1} attempts.")
        return company

    if max_categories:
        if len(categories) > max_categories:
            logger.info(f"{alias}: limiting to {max_categories} of {len(categories)} categories.")
        categories = categories[:max_categories]

    logger.info(f"{company.get('company')} [{alias}]: {len(categories)} categories")
    company["categories"] = [name for name, _, _ in categories]

    # Categories finished by an earlier run are never fetched again.
    already = done_categories(conn, alias) if conn is not None else set()
    if already:
        seen_products.update(company_product_keys(conn, alias))
        logger.info(
            f"{alias}: {len(already)} categories already stored, "
            f"{len(categories) - len(already)} to go."
        )

    for index, (name, url, expected) in enumerate(categories, 1):
        if max_products and len(company["products"]) >= max_products:
            logger.info(f"{alias}: reached the {max_products}-product cap.")
            break
        if url in already:
            continue
        try:
            if goto_with_retry(page, url) is None:
                # The host is throttling. Sleep it off and try this category
                # once more - skipping would leave a hole that the resume logic
                # cannot see, because the company still looks populated.
                logger.warning(
                    f"'{name}' blocked; sleeping {throttle_pause_ms // 60000} min then retrying."
                )
                page.wait_for_timeout(throttle_pause_ms)
                if goto_with_retry(page, url) is None:
                    logger.error(f"Category '{name}' unreachable; marking company incomplete.")
                    company.setdefault("skipped_categories", []).append(name)
                    continue
            page.wait_for_timeout(1000)
            # Settle lazy images so their src attributes are populated.
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1000)
            dismiss_overlays(page)
            found = extract_products(page, name)
            # Pace the crawl. Fifty-odd back-to-back category loads is what
            # triggers the 429 in the first place.
            page.wait_for_timeout(delay_ms)
        except Exception as e:
            logger.warning(f"Category {name} failed: {e}")
            continue

        fresh = []
        added = 0
        for product in found:
            key = normalize_key(product.get("product_url") or "", product.get("name") or "")
            if key in seen_products:
                continue
            seen_products.add(key)
            company["products"].append(product)
            fresh.append(product)
            added += 1
            if max_products and len(company["products"]) >= max_products:
                break

        # The index page states how many products each category holds, so a
        # short read is visible instead of passing as a complete category.
        if expected is not None and len(found) < expected:
            logger.warning(
                f"Category '{name}' lists {expected} products but only {len(found)} were read."
            )

        # Flush this category before moving on, so a run that dies here keeps
        # everything up to this point.
        if conn is not None:
            try:
                save_products(conn, alias, name, url, fresh)
            except Exception as e:
                logger.warning(f"Could not persist category '{name}': {e}")

        logger.info(
            f"  [{index}/{len(categories)}] {name[:40]:42} +{added} products "
            f"(total {len(company['products'])})"
        )
    return company


# ---------------------------------------------------------------------------
# Popup / blockage handling
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
# SQLite rather than a JSON blob: the crawl runs one company at a time across
# many sessions, so it needs a durable record of which companies are already
# finished. Rewriting a growing JSON file after every company also gets slower
# the further the crawl gets, and a kill mid-write truncates the lot.

COMPANY_COLUMNS = (
    "company", "company_url", "indiamart_url", "city", "state", "locality",
    "phone", "rating", "review_count", "member_since", "gst_number",
    "verified_exporter", "search_match_count", "query", "source",
)
PRODUCT_COLUMNS = (
    "name", "product_url", "category", "price", "moq", "description",
    "brochure_url", "automation_grade", "usage", "brand", "connector_type",
    "type", "material", "size",
)


def open_db(path):
    """Open (creating if needed) the catalog database."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS companies (
            alias              TEXT PRIMARY KEY,
            company            TEXT,
            company_url        TEXT,
            indiamart_url      TEXT,
            city               TEXT,
            state              TEXT,
            locality           TEXT,
            phone              TEXT,
            rating             REAL,
            review_count       INTEGER,
            member_since       TEXT,
            gst_number         TEXT,
            verified_exporter  INTEGER,
            search_match_count INTEGER,
            query              TEXT,
            source             TEXT,
            categories         TEXT,
            skipped_categories TEXT,
            status             TEXT,
            product_count      INTEGER,
            updated_at         TEXT
        );
        CREATE TABLE IF NOT EXISTS products (
            id              TEXT PRIMARY KEY,
            alias           TEXT NOT NULL,
            name            TEXT,
            product_url     TEXT,
            category        TEXT,
            price           TEXT,
            moq             TEXT,
            description     TEXT,
            brochure_url    TEXT,
            automation_grade TEXT,
            usage           TEXT,
            brand           TEXT,
            connector_type  TEXT,
            type            TEXT,
            material        TEXT,
            size            TEXT,
            specs           TEXT,
            images          TEXT,
            video_thumbnails TEXT,
            updated_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS products_alias ON products(alias);
        -- Category-level progress. A big catalog can outlive a single run (53
        -- categories plus throttle sleeps), and without this a killed run threw
        -- away everything it had collected and started the company over.
        CREATE TABLE IF NOT EXISTS categories (
            alias         TEXT NOT NULL,
            url           TEXT NOT NULL,
            name          TEXT,
            product_count INTEGER,
            updated_at    TEXT,
            PRIMARY KEY (alias, url)
        );
        """
    )
    # Older databases predate the query/source columns; add them in place so a
    # shared file created by an earlier version keeps working.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(companies)")}
    for column in ("query", "source"):
        if column not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {column} TEXT")
    conn.commit()
    return conn


def product_id(alias, product):
    """Stable primary key for a product.

    Most products carry a proddetail URL, but a few genuinely do not (verified
    live: some listings have no detail page at all), so those fall back to the
    company plus the product name.
    """
    url = product.get("product_url")
    if url:
        return url
    return f"{alias}::{normalize_key(product.get('name'))[0]}"


def save_products(conn, alias, category_name, category_url, products):
    """Write one category's products immediately and record the category as done.

    Called after every category rather than once per company: a catalog can take
    longer than a single run survives, and flushing per category means a killed
    process loses one category at most instead of the whole company.
    """
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.executemany(
            f"""INSERT INTO products
                (id, alias, {', '.join(PRODUCT_COLUMNS)}, specs, images,
                 video_thumbnails, updated_at)
                VALUES (?, ?{', ?' * (len(PRODUCT_COLUMNS) + 4)})
                ON CONFLICT(id) DO UPDATE SET
                    {', '.join(f'{c}=excluded.{c}' for c in PRODUCT_COLUMNS)},
                    specs=excluded.specs,
                    images=excluded.images,
                    video_thumbnails=excluded.video_thumbnails,
                    updated_at=excluded.updated_at""",
            [
                (
                    product_id(alias, p),
                    alias,
                    *[p.get(c) for c in PRODUCT_COLUMNS],
                    json.dumps(p.get("specs") or {}, ensure_ascii=False),
                    json.dumps(p.get("images") or [], ensure_ascii=False),
                    json.dumps(p.get("video_thumbnails") or [], ensure_ascii=False),
                    now,
                )
                for p in products
            ],
        )
        conn.execute(
            """INSERT INTO categories (alias, url, name, product_count, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(alias, url) DO UPDATE SET
                   name=excluded.name,
                   product_count=excluded.product_count,
                   updated_at=excluded.updated_at""",
            (alias, category_url, category_name, len(products), now),
        )


def done_categories(conn, alias):
    """Category URLs already scraped for this company - never fetched again."""
    rows = conn.execute("SELECT url FROM categories WHERE alias = ?", (alias,)).fetchall()
    return {row["url"] for row in rows}


def company_product_keys(conn, alias):
    """Dedupe keys for products already stored, so a resumed run stays consistent."""
    rows = conn.execute(
        "SELECT product_url, name FROM products WHERE alias = ?", (alias,)
    ).fetchall()
    return {normalize_key(r["product_url"] or "", r["name"] or "") for r in rows}


def completed_aliases(conn):
    """Aliases already fully scraped. These are never crawled again."""
    rows = conn.execute("SELECT alias FROM companies WHERE status = 'done'").fetchall()
    return {row["alias"] for row in rows}


def save_company(conn, record):
    """Persist one company and its products in a single transaction.

    All-or-nothing on purpose: a company row marked done whose products failed
    to write would be skipped forever by the next run.
    """
    alias = record["alias"]
    skipped = record.get("skipped_categories") or []
    products = record.get("products") or []
    now = datetime.now().isoformat(timespec="seconds")

    with conn:  # commits on success, rolls back on exception
        conn.execute(
            f"""INSERT INTO companies
                (alias, {', '.join(COMPANY_COLUMNS)}, categories, skipped_categories,
                 status, product_count, updated_at)
                VALUES (?{', ?' * (len(COMPANY_COLUMNS) + 5)})
                ON CONFLICT(alias) DO UPDATE SET
                    {', '.join(f'{c}=excluded.{c}' for c in COMPANY_COLUMNS)},
                    categories=excluded.categories,
                    skipped_categories=excluded.skipped_categories,
                    status=excluded.status,
                    product_count=excluded.product_count,
                    updated_at=excluded.updated_at""",
            (
                alias,
                *[record.get(c) for c in COMPANY_COLUMNS],
                json.dumps(record.get("categories") or [], ensure_ascii=False),
                json.dumps(skipped, ensure_ascii=False),
                "partial",  # promoted to done below, once the rows are counted
                0,
                now,
            ),
        )
        conn.executemany(
            f"""INSERT INTO products
                (id, alias, {', '.join(PRODUCT_COLUMNS)}, specs, images,
                 video_thumbnails, updated_at)
                VALUES (?, ?{', ?' * (len(PRODUCT_COLUMNS) + 4)})
                ON CONFLICT(id) DO UPDATE SET
                    {', '.join(f'{c}=excluded.{c}' for c in PRODUCT_COLUMNS)},
                    specs=excluded.specs,
                    images=excluded.images,
                    video_thumbnails=excluded.video_thumbnails,
                    updated_at=excluded.updated_at""",
            [
                (
                    product_id(alias, p),
                    alias,
                    *[p.get(c) for c in PRODUCT_COLUMNS],
                    json.dumps(p.get("specs") or {}, ensure_ascii=False),
                    json.dumps(p.get("images") or [], ensure_ascii=False),
                    json.dumps(p.get("video_thumbnails") or [], ensure_ascii=False),
                    now,
                )
                for p in products
            ],
        )

    # Count from the table, not from this run's slice: a resumed company only
    # carries the categories crawled this time, so len(products) would undercount
    # everything an earlier run already stored.
    with conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM products WHERE alias = ?", (alias,)
        ).fetchone()["c"]
        status = "done" if total and not skipped else "partial"
        conn.execute(
            "UPDATE companies SET status = ?, product_count = ? WHERE alias = ?",
            (status, total, alias),
        )

    logger.info(f"Saved {alias}: {total} products total, status={status}.")
    return status


def export_json(conn, filepath, aliases=None):
    """Write the nested JSON the earlier runs produced.

    ``aliases`` limits the export to the companies a single run touched. Without
    it the file is the whole database, which means every timestamped export
    repeats everything the previous ones already held.
    """
    companies = []
    rows = conn.execute("SELECT * FROM companies ORDER BY company").fetchall()
    if aliases is not None:
        rows = [r for r in rows if r["alias"] in set(aliases)]
    for row in rows:
        record = {c: row[c] for c in ("alias", *COMPANY_COLUMNS)}
        record["verified_exporter"] = bool(record["verified_exporter"])
        record["categories"] = json.loads(row["categories"] or "[]")
        record["skipped_categories"] = json.loads(row["skipped_categories"] or "[]")
        record["status"] = row["status"]
        record["products"] = [
            {
                **{c: p[c] for c in PRODUCT_COLUMNS},
                "specs": json.loads(p["specs"] or "{}"),
                "images": json.loads(p["images"] or "[]"),
                "video_thumbnails": json.loads(p["video_thumbnails"] or "[]"),
            }
            for p in conn.execute(
                "SELECT * FROM products WHERE alias = ? ORDER BY category, name", (row["alias"],)
            ).fetchall()
        ]
        companies.append(record)
    save_to_json(companies, filepath)
    return companies


def import_legacy_json(conn, filepath):
    """One-off migration of an earlier JSON checkpoint into the database."""
    if not filepath or not os.path.exists(filepath):
        return 0
    try:
        with open(filepath, encoding="utf-8") as f:
            records = json.load(f)
    except Exception as e:
        logger.warning(f"Could not read {filepath}: {e}")
        return 0

    imported = 0
    existing = {row["alias"] for row in conn.execute("SELECT alias FROM companies").fetchall()}
    for record in records:
        alias = record.get("alias")
        if not alias or alias in existing or not (record.get("products") or []):
            continue
        save_company(conn, record)
        imported += 1
    if imported:
        logger.info(f"Imported {imported} companies from {filepath} into the database.")
    return imported


def dismiss_overlays(page):
    """Close a blocking modal if one appeared.

    Measured across a 45-second dwell with scrolling on a category page, no
    overlay ever mounted - this scraper reads the DOM and clicks nothing, so
    there is little to trigger one. Kept as a cheap safety net.
    """
    try:
        found = page.evaluate(
            """() => {
                const blockers = [...document.querySelectorAll('body *')].filter(d => {
                    const s = getComputedStyle(d);
                    const r = d.getBoundingClientRect();
                    return s.position === 'fixed' && r.width > 380 && r.height > 220
                        && s.display !== 'none' && s.visibility !== 'hidden' && +s.opacity > 0.1;
                });
                let closed = 0;
                for (const b of blockers) {
                    const x = b.querySelector("[class*='close'], [id*='cls'], [aria-label*='lose'], .closeBtn");
                    if (x) { x.click(); closed++; }
                }
                return { blockers: blockers.length, closed };
            }"""
        )
    except Exception:
        return False

    if not found or not found.get("blockers"):
        return False
    logger.info(f"Dismissed {found['closed']} of {found['blockers']} blocking overlay(s).")
    return True


def install_guards(context, page, fast=True):
    """Block popup machinery before it can mount, and mute native dialogs."""
    # A native dialog would freeze the run until something answers it.
    page.on("dialog", lambda d: _safe(lambda: d.dismiss()))
    # A minisite that opens a new tab would otherwise steal the crawl.
    context.on("page", lambda p: _safe(lambda: p.close()) if p is not page else None)

    def route(route_obj, request):
        url = request.url.lower()
        if request.resource_type == "script" and any(p in url for p in POPUP_SCRIPT_PATTERNS):
            return route_obj.abort()
        # Image bytes are never needed - URLs are read from the src attribute -
        # and skipping them is by far the largest speed win on these pages.
        if fast and request.resource_type in ("image", "media", "font"):
            return route_obj.abort()
        return route_obj.continue_()

    _safe(lambda: context.route("**/*", route))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_scraper(
    query,
    output_file=None,
    limit=None,
    headless=False,
    session_file=None,
    max_categories=None,
    max_products_per_company=None,
    fast=True,
    delay_ms=1500,
    company_cooldown_ms=12000,
    throttle_pause_ms=600000,
    db_file=None,
):
    """Scrape company catalogs for ``query``, one company at a time.

    Defaults to a single company per run. IndiaMART's throttle is a request
    quota per window, and one catalog is roughly one window's worth - so
    stopping after one company is what lets repeated runs make steady progress
    instead of stalling halfway through the second.

    Companies already marked ``done`` in the database are never crawled again.
    """
    target = limit if limit is not None else 1
    cookies, local_storage = load_session(session_file)
    auth_failures = []

    if not output_file:
        output_file = export_path(query, "indiamart_catalog", "json")
    output_file = os.path.abspath(output_file)
    if not output_file.lower().endswith(".json"):
        output_file = os.path.splitext(output_file)[0] + ".json"

    db_file = os.path.abspath(db_file or DEFAULT_DB)
    conn = open_db(db_file)
    import_legacy_json(
        conn, os.path.join(BASE_DIR, "static", f"{slug(query)}_indiamart_catalog.json")
    )

    done = completed_aliases(conn)
    logger.info(f"Database {db_file}: {len(done)} companies already complete.")

    companies = []
    seen_products = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1600, "height": 950},
            locale="en-US",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        install_guards(context, page, fast=fast)

        def on_response(response):
            if "/api/search.rp" in response.url and response.status in (401, 403):
                auth_failures.append(f"search.rp HTTP {response.status}")
            elif "/api/reauth.rp" in response.url:
                # reauth answers 200 even when refusing the token, so the body is
                # the only reliable signal.
                try:
                    body = json.loads(response.text())
                except Exception:
                    return
                if str(body.get("access")) == "0":
                    auth_failures.append(f"reauth.rp refused: {body.get('message')}")

        page.on("response", on_response)

        try:
            search_url = build_search_url(query)
            logger.info(f"Navigating to {search_url}")
            page.goto(search_url, timeout=120000, wait_until="domcontentloaded")
            seed_local_storage(page, local_storage)
            page.wait_for_timeout(2500)

            results = read_search_results(page)
            if not results:
                logger.error(
                    "No search results. IndiaMART returns an empty listing when the session is "
                    "not recognised - re-export indiamart_session.json and retry."
                )
                return {"file": None, "data": []}

            logger.info(f"Search returned {len(results)} companies; scraping up to {target}.")

            for fields in results:
                if len(companies) >= target:
                    break
                alias = resolve_alias(fields)
                record = company_from_fields(fields, alias)
                if not record["company"] or not alias:
                    continue
                # The database is shared across queries and plugins, so record
                # which search surfaced this company.
                record["query"] = query
                record["source"] = "indiamart_catalog"

                if alias in done:
                    logger.info(f"Already complete, skipping: {record['company']} [{alias}]")
                    continue
                done.add(alias)

                try:
                    scrape_company(
                        page,
                        record,
                        max_categories=max_categories,
                        max_products=max_products_per_company,
                        seen_products=seen_products,
                        delay_ms=delay_ms,
                        throttle_pause_ms=throttle_pause_ms,
                        conn=conn,
                    )
                except Exception as e:
                    logger.warning(f"Company {record['company']} failed: {e}")

                companies.append(record)
                status = save_company(conn, record)
                if status != "done":
                    # Left incomplete, so clear the guard - the next run must be
                    # free to pick this company up again.
                    done.discard(alias)

                # A whole catalog is ~50 requests. Pausing between companies is
                # what keeps a multi-company run under IndiaMART's 429 threshold.
                if len(companies) < target and company_cooldown_ms:
                    page.wait_for_timeout(company_cooldown_ms)

            if auth_failures:
                logger.warning(
                    f"IndiaMART refused the saved session token ({auth_failures[-1]}). The company "
                    "list is limited to the first search page; catalog depth is unaffected. "
                    "Re-export indiamart_session.json while signed in to page further."
                )
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            _safe(lambda: browser.close())

    product_total = sum(len(c["products"]) for c in companies)
    logger.info(f"This run: {len(companies)} companies / {product_total} products.")

    # Only this run's companies go in the file. The database is the complete
    # record; re-dumping all of it under every new timestamp just republished
    # the previous file's contents under a new name.
    if companies:
        exported = export_json(
            conn, output_file, aliases=[c.get("alias") for c in companies]
        )
    else:
        logger.info("Nothing new this run - no export written.")
        output_file, exported = None, []
    totals = conn.execute(
        "SELECT status, COUNT(*) n FROM companies GROUP BY status"
    ).fetchall()
    logger.info(
        "Database totals: "
        + ", ".join(f"{r['status']}={r['n']}" for r in totals)
        + f", products={conn.execute('SELECT COUNT(*) c FROM products').fetchone()['c']}"
    )
    remaining = [r["company"] for r in conn.execute(
        "SELECT company FROM companies WHERE status != 'done'").fetchall()]
    if remaining:
        logger.info(f"Incomplete, will retry next run: {', '.join(remaining[:5])}")
    conn.close()

    return {"file": output_file, "db": db_file, "data": exported}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IndiaMART company catalog scraper")
    parser.add_argument("--query", required=True)
    parser.add_argument("--output")
    parser.add_argument("--limit", type=int, default=100,
        help="companies to scrape this run (default 1; already-done ones never recount)")
    parser.add_argument("--db", help="SQLite file (default: alongside the JSON output)")
    parser.add_argument("--session", help="exported session JSON (default: plugins/auth/indiamart/)")
    parser.add_argument("--max-categories", type=int, default=None)
    parser.add_argument("--max-products-per-company", type=int, default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-fast", action="store_true", help="load images/fonts too (slower)")
    parser.add_argument(
        "--delay", type=int, default=1500,
        help="ms to pause between category pages; raise it if IndiaMART starts returning 429",
    )
    parser.add_argument(
        "--company-cooldown", type=int, default=12000,
        help="ms to pause between companies",
    )
    parser.add_argument(
        "--throttle-pause", type=int, default=600000,
        help="ms to sleep when the host is throttling before retrying a company",
    )
    args = parser.parse_args()

    result = run_scraper(
        args.query,
        args.output,
        limit=args.limit,
        headless=args.headless,
        max_categories=args.max_categories,
        max_products_per_company=args.max_products_per_company,
        fast=not args.no_fast,
        delay_ms=args.delay,
        company_cooldown_ms=args.company_cooldown,
        throttle_pause_ms=args.throttle_pause,
        db_file=args.db,
        session_file=args.session,
    )
    print(
        json.dumps(
            {
                "file": result["file"],
                "db": result["db"],
                "companies": len(result["data"]),
                "products": sum(len(c["products"]) for c in result["data"]),
            }
        )
    )
