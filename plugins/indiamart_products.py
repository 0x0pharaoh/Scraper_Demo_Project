# indiamart_products.py
"""IndiaMART product-listing scraper.

Walks the product search results, expands every card's carousel, harvests the
gallery images from the story popup, and groups the products under the company
that sells them.

Kept separate from ``indiamart.py`` on purpose: that plugin scrapes supplier
contact rows into a CSV and is still used as-is. This one produces a nested
company -> products document and writes JSON.
"""

import html
import json
import os
import re
import sys
import subprocess
from datetime import datetime
from urllib.parse import quote_plus, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from utils.logger import get_logger
# Imported by name: run_scraper has a local ``store`` (the ListingStore), so a
# module named store cannot be referenced from inside it.
from utils.store import db_path, export_path, known_urls, open_table, upsert

logger = get_logger("indiamart_products")
description = "Scrape IndiaMART product cards (specs, price, gallery images) grouped by company."

# Chromium is normally installed by runner.py before this module loads. Keep a
# best-effort fallback for direct imports, but never let it kill the import.
subprocess.run(
    [sys.executable, "-m", "playwright", "install", "chromium"],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_SESSION_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "auth", "indiamart", "indiamart_session.json"
)

# The two cookies IndiaMART actually reads the login from; verified against the
# live search, which still returns rows with LGNSTR and sessid expired. A
# re-export that merges fresh analytics cookies over a stale im_iss keeps the
# file looking current while the login is long dead, so it is checked by name.
AUTH_COOKIES = {"im_iss", "ImeshVisitor"}

# --- Result card selectors -------------------------------------------------
# The listing is rendered by a remote component that namespaces everything with
# "template7-". These class names are far more stable than positional XPaths,
# which shift the moment an ad slot or banner is injected into the feed.
#
# This replaced an earlier "im-lc-" component wholesale: every selector below
# changed at once, and the plugin read the empty result as "no products". If it
# happens again, the symptom is the same, so check the markup before the
# session.
CARD = ".template7-product-card"
CARD_IMAGE = "img.template7-product-image"
CARD_IMAGE_OPEN = ".template7-card-media"
CARD_IMAGE_COUNT = "span.template7-image-count"
CARD_NEXT_VARIANT = ".media-nav"
CARD_DOTS = ".template7-carousel-dots > *"
CARD_TITLE = "a.prdtitle"
CARD_PRICE = "span.prc"
CARD_UNIT = "span.prcut"
CARD_SELLER = "a.template7-seller-name"
CARD_SPECS = "span.template7-isq-text"
SHOW_MORE = "button.showMoreBtn"

# --- Gallery popup selectors -----------------------------------------------
# Everything is scoped under ".ber-frwrap" because the page ships TWO elements
# carrying id="t0901_bewrapper" - one hidden enquiry form and the real image
# popup. Selecting by bare id resolves to the hidden one and hangs.
POPUP = "div.ber-frwrap"
POPUP_IMAGE = f"{POPUP} #t0901_zoomimage"
POPUP_NEXT = f"{POPUP} #t0901_nextbtnR"
POPUP_NEXT_TIP = f"{POPUP} #t0901_nextbtnR span.nxTp"
POPUP_CLOSE = f"{POPUP} #t0901_cls"
POPUP_BARS = f"{POPUP} #t0901_prodimgR div.stories-progress-bar"
POPUP_PRODUCT = f"{POPUP} #t0901_Prodname0R"

# Spec keys the caller asked to see promoted to the top of each product.
# Flat columns for the products table. "product_url" is left out because the
# primary key *is* the product url.
DB_COLUMNS = [
    "name", "company", "company_url", "city", "state",
    "price", "price_value", "display_id", "specs", "images",
]

PROMOTED_SPECS = {
    "automation_grade": ("automation grade",),
    "usage": ("usage/application", "usage", "application"),
    "brand": ("brand",),
    "connector_type": ("connector type", "connector", "end connection"),
    "type": ("type", "valve type", "product type"),
    "material": ("material",),
    "size": ("size", "valve size"),
}


# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------

def load_session(session_file=None):
    """Read the exported browser session into (cookies, local_storage).

    The export stores ``expires`` as an ISO string and ``sameSite`` as values
    Playwright rejects ("Medium"), so both are normalised here.
    """
    path = session_file or DEFAULT_SESSION_FILE
    if not os.path.exists(path):
        logger.warning(f"No session file at {path}; IndiaMART returns zero results when signed out.")
        return [], {}

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    cookies = []
    expired = []
    now = datetime.now().timestamp()
    for raw in data.get("cookies", []):
        cookie = {
            "name": raw["name"],
            "value": raw["value"],
            "domain": raw.get("domain", ".indiamart.com"),
            "path": raw.get("path", "/"),
        }
        expires = raw.get("expires")
        if isinstance(expires, str):
            try:
                cookie["expires"] = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
            except ValueError:
                pass
        elif isinstance(expires, (int, float)):
            cookie["expires"] = float(expires)
        same_site = str(raw.get("sameSite", "")).lower()
        cookie["sameSite"] = {"strict": "Strict", "lax": "Lax", "none": "None"}.get(same_site, "Lax")
        if raw.get("httpOnly") is not None:
            cookie["httpOnly"] = bool(raw["httpOnly"])
        if raw.get("secure") is not None:
            cookie["secure"] = bool(raw["secure"])
        if cookie.get("expires", float("inf")) <= now:
            # Playwright accepts these and the browser drops them silently, which
            # surfaces 20 minutes later as "no search results". Name them now.
            expired.append(cookie["name"])
            continue
        cookies.append(cookie)

    if expired:
        logger.warning(f"Dropped {len(expired)} expired cookies: {', '.join(expired)}")
    missing = AUTH_COOKIES - {c["name"] for c in cookies}
    if missing:
        raise RuntimeError(
            f"{path}: no live {', '.join(sorted(missing))} cookie. IndiaMART serves an empty "
            "listing when signed out. Re-export indiamart_session.json while signed in."
        )

    # The export splits browser state: session_data is sessionStorage,
    # browser_storage the real localStorage. Seed both.
    return cookies, {**(data.get("browser_storage") or {}), **(data.get("session_data") or {})}


def seed_local_storage(page, store):
    """Replay the exported localStorage so the app boots in its logged-in state."""
    if not store:
        return
    try:
        page.evaluate(
            """(data) => {
                for (const key in data) {
                    try {
                        const v = data[key];
                        localStorage.setItem(key, typeof v === 'string' ? v : JSON.stringify(v));
                    } catch (e) { /* quota or private mode - not fatal */ }
                }
            }""",
            store,
        )
    except Exception as e:
        logger.debug(f"Could not seed localStorage: {e}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def build_search_url(query):
    return f"https://dir.indiamart.com/search.mp?ss={quote_plus(query)}&search_type=p"


def clean_url(url):
    """Drop IndiaMART's tracking query string, keep the addressable page."""
    if not url:
        return None
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def normalize_image(url):
    """Force https on image URLs.

    The card markup serves http:// while the gallery popup serves https:// for
    the very same file, so without this the cover photo lands in the list twice.
    """
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url or None


def clean_text(value):
    if value is None:
        return None
    text = html.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalize_key(*values):
    """Case/whitespace-insensitive identity tuple used for duplicate detection."""
    return tuple(re.sub(r"\s+", " ", str(v or "").strip().lower()) for v in values)


def parse_isq(isq):
    """Turn IndiaMART's ["Brand==Uflow", ...] attribute list into a dict."""
    specs = {}
    for item in isq or []:
        if "==" not in str(item):
            continue
        key, _, value = str(item).partition("==")
        key, value = clean_text(key), clean_text(value)
        if key and value and key not in specs:
            specs[key] = value
    return specs


def promote_specs(specs):
    """Surface the specifically requested attributes alongside the full spec map."""
    lowered = {k.lower(): v for k, v in specs.items()}
    promoted = {}
    for field, aliases in PROMOTED_SPECS.items():
        promoted[field] = next((lowered[a] for a in aliases if a in lowered), None)
    return promoted


def _safe(fn, default=None):
    """Run a locator call that is allowed to fail without sinking the record."""
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Listing payload
# ---------------------------------------------------------------------------
# The cards are rendered by components.indiamart.com from a props blob that
# carries far more than the DOM shows: the complete ISQ attribute list, the
# seller phone number, and every sibling product in the carousel. Reading that
# blob is both richer and steadier than scraping the rendered card, so it is
# the primary source and the DOM is used to confirm alignment.

def parse_listing_payload(body):
    """Pull the result rows out of a search payload.

    Two shapes are served and both are accepted: the ``__NEXT_DATA__`` blob
    embedded in the first page, and the bare JSON that ``/api/search.rp``
    answers for later pages. The earlier ``listing-cards`` endpoint with its
    ``data-props`` attributes no longer exists.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []

    for path in (("props", "pageProps", "searchResponse"), ("searchResponse",), ()):
        node = data
        for step in path:
            node = node.get(step) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and node.get("results") is not None:
            return [e.get("fields") or {} for e in (node["results"] or []) if e]
    return []


def absorb_next_data(page, store):
    """Feed the first page's payload, which ships inside __NEXT_DATA__.

    Only pages 2+ are fetched over the wire, so without this the ten results on
    page 1 carry no payload at all and fall back to the four attributes the card
    renders.
    """
    blob = _safe(
        lambda: page.evaluate(
            "() => { const e = document.getElementById('__NEXT_DATA__');"
            " return e ? e.textContent : null; }"
        )
    )
    if blob:
        store.absorb(blob)
    logger.info(f"Listing payloads available: {len(store.by_url)}")


class ListingStore:
    """Collects listing payloads keyed by product URL.

    Keyed by URL rather than by feed position: the payload and the DOM used to
    be matched on index, which silently filed one product's specs under another
    whenever the feed injected an ad slot and shifted everything down. The URL
    is on both sides, so there is nothing to drift.
    """

    def __init__(self):
        self.by_url = {}

    def absorb(self, body):
        for fields in parse_listing_payload(body):
            url = clean_url(fields.get("title_url") or fields.get("desktop_title_url"))
            if url:
                self.by_url.setdefault(url, fields)

    def get(self, url):
        return self.by_url.get(clean_url(url) or "")


# ---------------------------------------------------------------------------
# Gallery popup
# ---------------------------------------------------------------------------

def close_popup(page):
    """Close the gallery. Never leave it open - it blocks every later click."""
    try:
        if page.locator(POPUP).count() == 0:
            return
        close = page.locator(POPUP_CLOSE)
        if close.count():
            close.first.click(timeout=3000)
            page.wait_for_timeout(700)
        if page.locator(POPUP).count():
            # Only now, with a popup provably still open, is a blind Escape safe.
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
    except Exception as e:
        logger.debug(f"Could not close gallery popup: {e}")


def _more_images_pending(page):
    """True while the next arrow still advances within the current product.

    IndiaMART hides the "Next Product" tooltip (class ``dispNone``) for as long
    as the arrow is paging through the current product's own images. Once the
    tooltip becomes visible the next click jumps to an unrelated product, so
    that is the stop signal.
    """
    tip = page.locator(POPUP_NEXT_TIP)
    if tip.count() == 0:
        return False
    return "dispNone" in (tip.first.get_attribute("class") or "")


def collect_popup_images(page, max_steps=25):
    """Open-gallery image harvest. Returns unique image URLs in display order."""
    images = []
    try:
        page.wait_for_selector(POPUP, timeout=12000)
    except PlaywrightTimeoutError:
        logger.debug("Gallery popup did not appear.")
        return images

    # The gallery auto-advances like a stories reel. Clicking the image pauses
    # it so our own next-clicks drive the sequence instead of racing it.
    _safe(lambda: page.locator(POPUP_IMAGE).first.click(timeout=3000))
    page.wait_for_timeout(400)

    expected = _safe(lambda: page.locator(POPUP_BARS).count(), 0) or 0

    stagnant = 0
    for _ in range(max_steps):
        src = _safe(lambda: page.locator(POPUP_IMAGE).first.get_attribute("src"))
        if src and src not in images:
            images.append(src)
            stagnant = 0
        else:
            # The arrow can keep reporting "more to come" while the image never
            # actually changes. Without this the loop burns its full step budget
            # on one card, which measured out at over a minute.
            stagnant += 1
            if stagnant >= 3:
                break
        if expected and len(images) >= expected:
            break
        if not _more_images_pending(page):
            break
        try:
            page.locator(POPUP_NEXT).first.click(timeout=3000)
        except Exception:
            break
        page.wait_for_timeout(800)

    if expected and len(images) < expected:
        logger.debug(f"Gallery showed {len(images)} of {expected} images.")
    return images


def gallery_for_primary(page, card):
    """Harvest the gallery for a card's primary product.

    Only the primary product is harvested on purpose. Verified against the live
    site: opening the popup from variants 1..n still loads the PRIMARY product's
    gallery (the popup header keeps showing the primary product's name and
    link), so attaching those images to a sibling product would file the wrong
    photos under the wrong item. Siblings keep their own card thumbnail instead.
    """
    opener = card.locator(CARD_IMAGE_OPEN)
    if opener.count() == 0:
        return []
    try:
        opener.first.scroll_into_view_if_needed(timeout=4000)
        opener.first.click(timeout=6000)
    except Exception as e:
        logger.debug(f"Could not open gallery: {e}")
        return []

    try:
        return collect_popup_images(page)
    finally:
        close_popup(page)


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------

def read_card_specs(card):
    """Read the spec table the card itself renders.

    Only a handful of attributes are shown here versus the full ISQ list in the
    payload, but the feed re-renders cards for which no payload was captured,
    and without this those products would carry no specs at all.
    """
    specs = {}
    try:
        # One flat run of spans, alternating label then value, rather than the
        # dl/dt/dd the previous markup used.
        cells = card.locator(CARD_SPECS).all_inner_texts()
    except Exception:
        return specs
    for key, value in zip(cells[::2], cells[1::2]):
        key, value = clean_text(key), clean_text(value)
        if key and value and key not in specs:
            specs[key] = value
    return specs


def read_card_variant(card):
    """Read whatever the currently displayed carousel slide shows."""
    title_el = card.locator(CARD_TITLE)
    price = _safe(lambda: card.locator(CARD_PRICE).first.inner_text(timeout=2000))
    unit = _safe(lambda: card.locator(CARD_UNIT).first.inner_text(timeout=2000))
    return {
        "name": clean_text(_safe(lambda: title_el.first.inner_text(timeout=2000))),
        "product_url": clean_url(_safe(lambda: title_el.first.get_attribute("href"))),
        "image": _safe(lambda: card.locator(CARD_IMAGE).first.get_attribute("src")),
        "price": clean_text(f"{price or ''}{unit or ''}") if price else None,
        "specs": read_card_specs(card),
    }


def product_key(product_url, name):
    """Identity of a product.

    Keyed on the URL, never on ``display_id``: the feed re-renders cards without
    a matching payload, so display_id is present on the first pass and missing on
    the second. Mixing the two lets the same product through twice.

    A few listings genuinely have no detail page, so those fall back to the
    product name. One string rather than a tuple, so the same key is also the
    database primary key and the two dedup checks cannot drift apart.
    """
    url = (product_url or "").strip()
    return url or normalize_key(name or "")[0]


def build_product(dom, payload, images):
    """Merge the rendered slide with its payload record into one product.

    The payload is the richer source where one exists - it carries the full ISQ
    attribute list against the four the card renders - but only the first
    product of each card appears in the feed payload, so the other carousel
    slides are built from the DOM alone.
    """
    payload = payload or {}
    specs = parse_isq(payload.get("isq")) or dom.get("specs") or {}
    name = clean_text(payload.get("title") or payload.get("original_title")) or dom.get("name")
    price = clean_text(payload.get("indiaPriceFormat")) or dom.get("price")
    moq = clean_text(payload.get("moq"))
    unit = clean_text(payload.get("moq_type"))

    gallery = []
    # "photo" is a list now; it used to be a single URL. Flatten either form.
    photos = payload.get("photo")
    photos = photos if isinstance(photos, list) else [photos]
    for raw in [dom.get("image"), *photos, payload.get("photo_1000"), *images]:
        url = normalize_image(raw)
        if url and url not in gallery:
            gallery.append(url)

    product = {
        "name": name,
        "product_url": clean_url(payload.get("title_url")) or dom.get("product_url"),
        "display_id": payload.get("displayid"),
        "price": price,
        "price_value": payload.get("itemprice"),
        "moq": f"{moq} {unit}".strip() if moq else None,
        "description": clean_text(payload.get("smalldescorg")),
        "brochure_url": clean_url(payload.get("prod_doc")),
        "specs": specs,
        "images": gallery,
    }
    product.update(promote_specs(specs))
    return product


def build_company(payload, card):
    """Company-level facts. Same for every product in the card."""
    payload = payload or {}
    seller = card.locator(CARD_SELLER)
    name = clean_text(payload.get("companyname")) or clean_text(
        _safe(lambda: seller.first.inner_text(timeout=2000))
    )
    store = clean_url(payload.get("catalog_url") or payload.get("paidurl")) or clean_url(
        _safe(lambda: seller.first.get_attribute("href"))
    )
    alias = payload.get("companyalias")
    phones = payload.get("phone") or payload.get("mobile") or []
    return {
        "company": name,
        "company_url": store,
        "indiamart_url": f"https://www.indiamart.com/{alias}/" if alias else None,
        "trustseal_url": clean_url(payload.get("desktopCatalogRatingUrl")),
        "city": clean_text(payload.get("city")) or clean_text(
            _safe(lambda: card.locator('span[itemprop="addressLocality"]').first.inner_text(timeout=2000))
        ),
        "state": clean_text(payload.get("state")),
        "country": clean_text(payload.get("countryname")),
        "address": clean_text(payload.get("address")),
        "phone": clean_text(phones[0]) if isinstance(phones, list) and phones else None,
        "member_since": clean_text(payload.get("member_since") or payload.get("yearofestablishment")),
        "response_rate": payload.get("response_rate"),
        "rating": payload.get("supplier_rating"),
        "review_count": payload.get("rating_count"),
        "banner": clean_text(payload.get("biz_banner")),
        "verified_exporter": str(payload.get("is_verified_exporter")) in ("1", "True"),
        "products": [],
    }


def variant_count(card, listing):
    """How many products this card's carousel holds.

    The dots are authoritative - one per product, labelled "Product 1..n". The
    payload used to carry the siblings under ``more_prod``; it no longer does,
    so ``productdisplayed`` is the only payload-side hint left.
    """
    dots = _safe(lambda: card.locator(CARD_DOTS).count(), 0) or 0
    if dots:
        return dots
    if listing:
        try:
            return max(1, int(listing.get("productdisplayed") or 1))
        except (TypeError, ValueError):
            return 1
    return 1


def show_variant(page, card, variant):
    """Bring carousel slide ``variant`` into view. True when it actually moved.

    Clicks the numbered dot rather than the next arrow. The arrow only ever
    toggled between the first two slides, so a four-product card yielded two
    products and the rest were silently lost.
    """
    dots = card.locator(CARD_DOTS)
    if variant < _safe(lambda: dots.count(), 0):
        try:
            dots.nth(variant).click(timeout=5000)
            page.wait_for_timeout(900)
            return True
        except Exception as e:
            logger.debug(f"Could not click dot {variant}: {e}")
    try:
        card.locator(CARD_NEXT_VARIANT).first.click(timeout=5000)
        page.wait_for_timeout(900)
        return True
    except Exception as e:
        logger.debug(f"Could not advance carousel: {e}")
        return False


def scrape_card(page, card, store, index, seen_products):
    """Walk one card's carousel and return a company record with its products.

    ``seen_products`` is shared across the whole run: IndiaMART re-renders the
    same sellers further down the feed, so the duplicate check has to be global,
    and it has to run before the gallery is opened - that popup dance is by far
    the slowest step and is pure waste on a product already collected.

    Each slide's payload is looked up by its own product URL, so an ad slot
    shifting the feed cannot file one product's specs under another.
    """
    total = variant_count(card, None)
    company = None
    collected = 0

    for variant in range(total):
        if variant and not show_variant(page, card, variant):
            break
        try:
            dom = read_card_variant(card)
            payload = store.get(dom.get("product_url"))
            if company is None:
                company = build_company(payload, card)
                logger.info(
                    f"Card #{index + 1}: {company['company'] or 'unknown'} ({total} products)"
                )

            key = product_key(dom.get("product_url"), dom.get("name"))
            if not dom.get("name"):
                logger.debug(f"Card {index + 1} slide {variant} has no title; skipped.")
            elif key in seen_products:
                logger.debug(f"Skipped duplicate product: {dom['name'][:60]}")
            else:
                images = gallery_for_primary(page, card)
                product = build_product(dom, payload, images)
                seen_products.add(key)
                company["products"].append(product)
                collected += 1
                logger.info(
                    f"    + {product['name'][:60]} "
                    f"({len(product['images'])} images, {len(product['specs'])} specs"
                    f"{'' if payload else ', card only'})"
                )
        except Exception as e:
            logger.warning(f"Product {variant + 1} on card {index + 1} failed: {e}")

    if company is None:
        company = build_company(None, card)
    if collected < total:
        logger.info(f"    card #{index + 1}: {collected} of {total} slides yielded a product")
    return company


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def grow_feed(page, before, rounds=8):
    """Let the feed lazily render more cards by scrolling to the bottom.

    The listing appends a second batch of cards on scroll alone, without any
    "show more" click. Doing this explicitly means the run does not depend on
    cards happening to appear while cards were being scrolled past.
    """
    for _ in range(rounds):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception as e:
            logger.debug(f"Feed scroll failed: {e}")
            return False
        page.wait_for_timeout(2200)
        if page.locator(CARD).count() > before:
            return True
    return False


def show_more_results(page, before):
    """Click "Show more results" and report whether new cards arrived."""
    button = page.locator(SHOW_MORE)
    if button.count() == 0:
        logger.info("No 'Show more results' button on the page.")
        return False
    # Scrolling is best-effort only. The button is frequently reported as not
    # visible (a feedback panel renders above it), and letting that abort the
    # attempt skips the click entirely - the JS path below does not need
    # visibility, so a failed scroll must not stop us.
    _safe(lambda: button.first.scroll_into_view_if_needed(timeout=4000))
    page.wait_for_timeout(600)
    try:
        # The button carries a "disableShowMore" class while the app decides
        # whether more pages are available; clearing it lets the click through.
        clicked = page.evaluate(
            """(sel) => {
                const b = document.querySelector(sel);
                if (!b) return false;
                b.classList.remove('disableShowMore');
                b.click();
                return true;
            }""",
            SHOW_MORE,
        )
        if not clicked:
            return False
    except Exception as e:
        logger.warning(f"Could not click 'Show more results': {e}")
        return False

    for _ in range(12):
        page.wait_for_timeout(1000)
        if page.locator(CARD).count() > before:
            return True
    return False


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_to_json(data, filepath):
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Scraping completed. Output saved to {filepath}")
    return filepath


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def save_products(conn, record, query):
    """Flush one company's products, keyed on the product URL.

    Called as each card finishes rather than once at the end: a run that dies
    on card 30 of 40 keeps the first 29 instead of losing the lot.
    """
    for product in record["products"]:
        row = {c: record.get(c) if c in ("company", "company_url", "city", "state")
               else product.get(c) for c in DB_COLUMNS}
        for nested in ("specs", "images"):
            row[nested] = json.dumps(row[nested] or ([] if nested == "images" else {}),
                                     ensure_ascii=False)
        upsert(conn, "products",
               product_key(product.get("product_url"), product.get("name")), row, query)


def run_scraper(query, output_file=None, limit=None, headless=False, session_file=None, max_pages=20):
    """Scrape IndiaMART product cards for ``query``.

    ``limit`` caps the number of companies collected. Output is always JSON
    because the records are nested (company -> products -> images), which a flat
    CSV cannot represent without stringifying half of it.
    """
    target = limit if limit is not None else 40
    cookies, local_storage = load_session(session_file)
    store = ListingStore()
    auth_failures = []

    conn = open_table(db_path("indiamart_products"), "products", DB_COLUMNS)
    companies = []
    seen_companies = {}
    # Pre-loaded with every product URL from every earlier run, so scrape_card
    # skips them before the gallery popup - the slowest step in the whole run -
    # and the export never repeats a product the database already holds.
    seen_products = known_urls(conn, "products")
    logger.info(f"Database has {len(seen_products)} products already stored.")

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

        def on_response(response):
            url = response.url
            if "/api/search.rp" in url and response.status == 200:
                # Page 2 onwards arrives here as bare JSON. Page 1 is not a
                # request at all - it is baked into __NEXT_DATA__ and absorbed
                # straight after the first navigation.
                try:
                    store.absorb(response.text())
                except Exception as e:
                    logger.debug(f"Could not read listing payload: {e}")
            elif "/api/search.rp" in url and response.status in (401, 403):
                auth_failures.append(f"search.rp HTTP {response.status}")
            elif "/api/reauth.rp" in url:
                # reauth answers HTTP 200 even when it refuses the token, so the
                # body is the only reliable signal. When it refuses, the app
                # never even issues the page-2 request.
                try:
                    body = json.loads(response.text())
                except Exception:
                    return
                if str(body.get("access")) == "0" or "verification failed" in str(
                    body.get("message", "")
                ).lower():
                    auth_failures.append(f"reauth.rp refused: {body.get('message')}")

        page.on("response", on_response)

        try:
            search_url = build_search_url(query)
            logger.info(f"Navigating to {search_url}")
            page.goto(search_url, timeout=120000, wait_until="domcontentloaded")
            seed_local_storage(page, local_storage)

            absorb_next_data(page, store)

            try:
                page.wait_for_selector(CARD, timeout=45000)
            except PlaywrightTimeoutError:
                logger.error(
                    "No product cards rendered. IndiaMART shows an empty listing when the "
                    "session is not recognised - re-export the session file and retry."
                )
                return {"file": None, "data": []}

            page.wait_for_timeout(3000)

            processed = 0
            for _ in range(max_pages):
                cards = page.locator(CARD)
                total_cards = cards.count()
                logger.info(f"Cards on page: {total_cards} (processed {processed})")

                while processed < total_cards and len(companies) < target:
                    index = processed
                    processed += 1
                    try:
                        card = cards.nth(index)
                        card.scroll_into_view_if_needed(timeout=5000)
                        page.wait_for_timeout(400)
                        record = scrape_card(page, card, store, index, seen_products)
                    except Exception as e:
                        logger.warning(f"Card {index + 1} failed entirely: {e}")
                        continue

                    if not record.get("company") or not record.get("products"):
                        continue

                    save_products(conn, record, query)

                    key = normalize_key(record["company"], record.get("city"))
                    existing = seen_companies.get(key)
                    if existing is None:
                        seen_companies[key] = len(companies)
                        companies.append(record)
                    else:
                        # Same seller further down the feed: fold the new products
                        # into the existing record instead of emitting the company
                        # twice. scrape_card already filtered out repeats, so
                        # everything here is genuinely new.
                        companies[existing]["products"].extend(record["products"])
                        logger.info(
                            f"Merged {len(record['products'])} more products into {record['company']}"
                        )

                if len(companies) >= target:
                    logger.info(f"Reached limit of {target} companies.")
                    break

                current = page.locator(CARD).count()
                if grow_feed(page, current):
                    logger.info("Feed rendered more cards on scroll.")
                    continue

                if not show_more_results(page, current):
                    if auth_failures:
                        logger.error(
                            "Pagination blocked: IndiaMART rejected the saved session token "
                            f"({auth_failures[-1]}). The first page still renders because that "
                            "comes from cookies, but every further page is fetched through "
                            "/api/search.rp, which needs a token the server accepts. "
                            "Re-export indiamart_session.json while signed in, then re-run."
                        )
                    else:
                        logger.info("No further results available.")
                    break
                page.wait_for_timeout(1500)

        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            _safe(lambda: close_popup(page))
            _safe(lambda: browser.close())

    companies = companies[:target]
    product_total = sum(len(c["products"]) for c in companies)
    logger.info(f"Collected {len(companies)} companies / {product_total} products.")

    if not companies:
        # Writing here would republish whatever the last run found under a new
        # timestamp. An empty run leaves no file.
        logger.info("Nothing new this run - no export written.")
        return {"file": None, "data": []}

    if not output_file:
        output_file = export_path(query, "indiamart_products", "json")
    output_file = os.path.abspath(output_file)
    if not output_file.lower().endswith(".json"):
        output_file = os.path.splitext(output_file)[0] + ".json"

    filepath = save_to_json(companies, output_file)
    return {"file": filepath, "data": companies}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IndiaMART product scraper")
    parser.add_argument("--query", required=True)
    parser.add_argument("--output")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--headless", action="store_true", help="run without a visible browser")
    args = parser.parse_args()

    result = run_scraper(
        args.query,
        args.output,
        limit=args.limit,
        headless=args.headless,
        max_pages=args.max_pages,
    )
    print(json.dumps({"file": result["file"], "companies": len(result["data"])}))
