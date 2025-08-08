# google_maps.py

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import time
import csv
from datetime import datetime
from urllib.parse import quote_plus
from utils.logger import get_logger

logger = get_logger("google_maps")

description = "Scrape business listings, ratings, and addresses from Google Maps."

def build_search_url(query):
    return f"https://www.google.com/maps/search/{quote_plus(query)}"

def auto_scroll(page, scroll_container_selector, delay=1.0, max_scrolls=20, limit=None):
    scroll_container = page.locator(scroll_container_selector).nth(1)
    logger.info("Scrolling to load listings...")
    last_seen = 0

    for i in range(max_scrolls):
        scroll_container.evaluate("el => el.scrollBy(0, el.scrollHeight)")
        time.sleep(delay)

        current_count = page.locator("a.hfpxzc").count()
        logger.info(f"Scroll {i + 1}: {current_count} listings visible.")

        if current_count == last_seen:
            logger.info("⏹No new results loaded.")
            break
        if limit and current_count >= limit:
            logger.info(f"Early stopping at {current_count} listings.")
            break
        last_seen = current_count

def extract_data(page, limit=None):
    cards = page.locator("a.hfpxzc").all()
    logger.info(f"Total cards found: {len(cards)}")
    if limit:
        cards = cards[:limit]

    data = []
    for i, card in enumerate(cards):
        try:
            name = card.get_attribute("aria-label") or "N/A"
            url = card.get_attribute("href") or "N/A"

            with page.expect_navigation(timeout=10000):
                card.click()

            page.wait_for_selector("div[role='main']", timeout=10000)

            address = page.locator("button[data-item-id='address']").first
            rating = page.locator("span[aria-label*='stars']").first

            address_text = address.inner_text() if address and address.count() else "N/A"
            rating_text = rating.inner_text() if rating and rating.count() else "N/A"

            data.append({
                "Name": name.strip(),
                "URL": url.strip(),
                "Address": address_text.strip(),
                "Rating": rating_text.strip()
            })
        except Exception as e:
            logger.warning(f"⚠️ Failed to extract listing {i + 1}: {e}")
            continue

    return data

def save_to_csv(data, file_path):
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "URL", "Address", "Rating"])
        writer.writeheader()
        writer.writerows(data)
    logger.info(f"Results saved to {file_path}")

def run_scraper(query, output_file=None, limit=None):
    logger.info(f"Starting Google Maps scraper for: {query}")
    logger.info(f"Received limit: {limit}")

    search_url = build_search_url(query)

    if not output_file:
        date_str = datetime.now().strftime("%d%m%y")
        output_file = f"{query.replace(' ', '_')}_google_maps_{date_str}.csv"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.goto(search_url, timeout=60000)

        try:
            logger.info("Waiting for listings container to load...")
            page.locator("div.m6QErb.DxyBCb.kA9KIf.dS8AEf.XiKgde").nth(1).wait_for(timeout=20000)
        except PlaywrightTimeoutError:
            logger.error("Timeout: Listings not found.")
            save_to_csv([], output_file)
            return 0

        auto_scroll(page, "div.m6QErb.DxyBCb.kA9KIf.dS8AEf.XiKgde", delay=1.0, limit=limit)

        results = extract_data(page, limit=limit)

        if not results:
            logger.warning("⚠️ No data scraped.")

        save_to_csv(results, output_file)
        browser.close()

        print(f"FOUND_COUNT: {len(results)}")
        return len(results)














"""
# google_maps.py

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import time
import csv
from datetime import datetime
from urllib.parse import quote_plus
from utils.logger import get_logger

logger = get_logger("google_maps")

def build_search_url(query):
    return f"https://www.google.com/maps/search/{quote_plus(query)}"

def auto_scroll(page, scroll_container_selector, scroll_count=12, delay=2):
    scroll_container = page.locator(scroll_container_selector).nth(1)
    logger.info("Scrolling to load all listings...")
    for i in range(scroll_count):
        logger.info(f"Scrolling... ({i + 1}/{scroll_count})")
        scroll_container.evaluate("el => el.scrollBy(0, el.scrollHeight)")
        time.sleep(delay)

def extract_data(page):
    cards = page.locator("a.hfpxzc")
    count = cards.count()
    logger.info(f"Found {count} listings")
    data = []

    for i in range(count):
        try:
            card = cards.nth(i)
            name = card.get_attribute("aria-label") or "N/A"
            url = card.get_attribute("href") or "N/A"
            data.append({"Name": name.strip(), "URL": url.strip()})
        except Exception as e:
            logger.warning(f"Error extracting data from card {i}: {e}")
            continue

    return data

def save_to_csv(data, file_path):
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "URL"])
        writer.writeheader()
        writer.writerows(data)
    logger.info(f"Results saved to {file_path}")

def run_scraper(query, output_file=None):
    logger.info(f"Running Google Maps scraper for: {query}")
    search_url = build_search_url(query)

    if not output_file:
        date_str = datetime.now().strftime("%d%m%y")
        output_file = f"{query.replace(' ', '_')}_google_maps_{date_str}.csv"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(search_url, timeout=60000)

        try:
            logger.info("Waiting for results pane to load...")
            page.locator("div.m6QErb.DxyBCb.kA9KIf.dS8AEf.XiKgde").nth(1).wait_for(timeout=20000)
        except PlaywrightTimeoutError:
            logger.error("Timeout: Could not find listing results.")
            save_to_csv([], output_file)
            return

        auto_scroll(page, "div.m6QErb.DxyBCb.kA9KIf.dS8AEf.XiKgde", scroll_count=12, delay=2)
        results = extract_data(page)

        if not results:
            logger.warning("No data scraped.")
        save_to_csv(results, output_file)

        browser.close()

"""




