# indiamart.py

import subprocess
subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import csv
import os
import time
import re
from urllib.parse import quote_plus
from utils.logger import get_logger
from utils import store
# The session loader and URL cleaner already live in indiamart_products; the
# listing is the same page, so there is nothing here to duplicate.
from plugins.indiamart_products import DESKTOP_UA, clean_url, load_session, seed_local_storage

logger = get_logger("indiamart")
description = "Scrape supplier contact data from IndiaMART (B2B marketplace)."

FIELDNAMES = ["Company Name", "Location", "Phone", "URL", "Product", "Product URL"]
# Stored columns. The primary key *is* the product url, and SQLite column names
# are case-insensitive, so a plain "URL" column would collide with it - the
# supplier's own link is stored under "Supplier URL" instead.
_DB_COLUMNS = ["Company Name", "Location", "Phone", "Supplier URL", "Product"]

def build_search_url(query):
    return f"https://dir.indiamart.com/search.mp?ss={quote_plus(query)}"

def scroll_feed(page):
    """Scroll results to load more cards."""
    try:
        page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
    except:
        logger.warning("Could not scroll feed. Possibly no more results.")
    time.sleep(2)

def normalize_key(*values):
    """Normalize values for duplicate detection."""
    def clean(v):
        return re.sub(r"\s+", " ", v.strip().lower())
    return tuple(clean(str(v)) for v in values)

def entry_key(data):
    """Stable identity for a row: the product URL.

    Each card in the listing is one product with its seller attached, so the
    proddetail URL is the entry's identity. Cards without one fall back to the
    supplier URL, then to name plus location - the same fallback shape the
    catalog plugin uses for products with no detail page.

    Every candidate is cleaned first: IndiaMART appends a per-search
    ``?pos=&kwd=&tags=`` string that differs on every run, so the raw href
    would key the same listing as a new row each time.
    """
    for field in ("Product URL", "URL"):
        url = clean_url((data.get(field) or "").strip())
        if url and url != "N/A":
            return url
    return "::".join(normalize_key(data.get("Company Name"), data.get("Location")))


def db_row(data):
    """Map a scraped card onto the stored columns."""
    return {
        "Company Name": data.get("Company Name"),
        "Location": data.get("Location"),
        "Phone": data.get("Phone"),
        "Supplier URL": data.get("URL"),
        "Product": data.get("Product"),
    }


def extract_card_data(card):
    """Extract one row from a result card.

    Reads the "template7" listing that replaced the old ``.supplierInfoDiv``
    markup. The city comes from the ``addressLocality`` microdata attribute
    rather than a class name, because the classes are the part IndiaMART
    rewrites.
    """
    try:
        seller = card.query_selector("a.template7-seller-name")
        city = card.query_selector('span[itemprop="addressLocality"]')
        product = card.query_selector("a.prdtitle")
        # Kept for when a card does carry a number, but the listing no longer
        # renders one: the phone now sits behind the Contact Supplier enquiry.
        phone_elem = card.query_selector(".pns_h, .contactnumber .duet")

        return {
            "Company Name": seller.inner_text().strip() if seller else "N/A",
            "Location": city.inner_text().strip() if city else "N/A",
            "Phone": phone_elem.inner_text().strip() if phone_elem else "N/A",
            "URL": clean_url(seller.get_attribute("href")) if seller else "N/A",
            "Product": product.inner_text().strip() if product else "N/A",
            "Product URL": clean_url(product.get_attribute("href")) if product else "N/A",
        }
    except Exception as e:
        logger.warning(f"Error extracting a card: {e}")
        return None

def save_to_csv(data, filepath):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(data)
    logger.info(f"Scraping completed. Output saved to {filepath}")
    return filepath

CARD = ".template7-product-card"


def run_scraper(query, output_file=None, limit=None, session_file=None):
    target_count = limit if limit is not None else 40
    timeout_ms = 180000 if limit is None else 60000

    # IndiaMART serves an empty listing to a signed-out browser, which this
    # plugin used to read as "no results". load_session raises when the saved
    # login is dead, so a stale session fails here with a clear reason.
    cookies, local_storage = load_session(session_file)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            context = browser.new_context(user_agent=DESKTOP_UA, locale="en-US")
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()

            search_url = build_search_url(query)
            logger.info(f"Navigating to {search_url}")
            page.goto(search_url, timeout=timeout_ms)
            seed_local_storage(page, local_storage)

            try:
                page.wait_for_selector(CARD, timeout=15000)
            except PlaywrightTimeoutError:
                logger.warning(
                    "No result cards found. IndiaMART shows an empty listing when the "
                    "session is not recognised - re-export the session file and retry."
                )
                browser.close()
                return {"file": None, "data": []}

            conn = store.open_table(
                store.db_path("indiamart"), "listings", _DB_COLUMNS
            )
            # Pre-loaded with every listing from every earlier run, so a re-run
            # collects only what is genuinely new and the export never repeats a
            # row the database already holds.
            known = store.known_urls(conn, "listings")
            logger.info(f"Database has {len(known)} listings already stored.")

            collected = []
            scrolls_done = 0
            max_scrolls = 40 if limit is None else 20
            last_cards_count = 0

            while len(collected) < target_count and scrolls_done < max_scrolls:
                cards = page.query_selector_all(CARD)
                logger.info(f"Found {len(cards)} cards on scroll #{scrolls_done + 1}")

                new_cards = []
                for c in cards:
                    data = extract_card_data(c)
                    if not data:
                        continue
                    key = entry_key(data)
                    if key in known:
                        continue
                    known.add(key)
                    store.upsert(conn, "listings", key, db_row(data), query)
                    new_cards.append(data)

                logger.info(f"New unique cards this round: {len(new_cards)}")
                collected.extend(new_cards)

                if len(collected) >= target_count:
                    break

                scroll_feed(page)
                scrolls_done += 1

                if len(cards) == last_cards_count:
                    logger.info("ℹ No new cards loaded after scrolling, ending.")
                    break
                last_cards_count = len(cards)

            browser.close()

            if not collected:
                # Writing here would republish the previous run's rows under a
                # new timestamp. An empty run leaves no file.
                logger.info("Nothing new this run - no export written.")
                return {"file": None, "data": []}

            if not output_file:
                output_file = store.export_path(query, "indiamart", "csv")
            else:
                # Ensure absolute path always
                output_file = os.path.abspath(output_file)

            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            filepath = save_to_csv(collected[:target_count], output_file)

            return {"file": filepath, "data": collected[:target_count]}

        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return {"file": None, "data": []}
































































"""

# indiamart.py
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
import pandas as pd
import os
import time
from datetime import datetime
from urllib.parse import quote_plus
from utils.logger import get_logger

logger = get_logger("indiamart")

def build_search_url(query):
    return f"https://dir.indiamart.com/search.mp?ss={quote_plus(query)}"

def save_results(results, query, output_file):
    if not output_file:
        date = datetime.now().strftime("%d%m%y")
        file_name = f"static/{query.replace(' ', '_')}_indiamart_{date}.csv"
    else:
        file_name = output_file

    os.makedirs(os.path.dirname(file_name), exist_ok=True)
    df = pd.DataFrame(results)
    df.to_csv(file_name, index=False)
    logger.info(f"IndiaMART results saved to {file_name}")

def run_scraper(query: str, output_file: str = None):
    logger.info(f"Running IndiaMART scraper for: {query}")
    search_url = build_search_url(query)
    logger.info(f"Opening URL: {search_url}")

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # ✅ Headless for Flask UI
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        ))

        page = context.new_page()
        page.goto(search_url, timeout=60000)

        try:
            logger.info("Waiting 5 seconds before checking for results...")
            time.sleep(5)

            # ✅ Try waiting for either of the known selectors
            page.wait_for_selector("div.prd-card, div.supplierInfoDiv", timeout=25000)
        except PlaywrightTimeoutError:
            logger.error("⏱ Timeout: No IndiaMART results.")
            browser.close()
            save_results([], query, output_file)
            return

        # ✅ Scroll to load more
        for _ in range(10):
            page.mouse.wheel(0, 2000)
            time.sleep(2)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("div.supplierInfoDiv")  # ✅ more consistent selector

        for card in cards:
            name = card.select_one("a.cardlinks")
            address = card.select_one("p.wpw")
            phone = card.select_one("span.pns_h")
            supplier = name
            price = card.select_one(".price")

            results.append({
                "Name": name.text.strip() if name else "",
                "Supplier": supplier.text.strip() if supplier else "",
                "Phone": phone.text.strip() if phone else "",
                "Address": address.text.strip() if address else "",
                "Price": price.text.strip() if price else "",
            })

        logger.info(f"✅ Scraped {len(results)} results.")
        browser.close()
        save_results(results, query, output_file)

"""
