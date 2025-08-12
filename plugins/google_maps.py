# plugins/google_maps.py

import subprocess
subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)

import time
import csv
import os
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from utils.logger import get_logger

logger = get_logger("google_maps")
description = "Scrape business data from Google Maps search results"

def scroll_until_end(page, max_scrolls=20):
    logger.info("Starting auto-scroll to load all results...")
    last_height = 0
    for i in range(max_scrolls):
        page.evaluate("document.querySelector('div[role=\"feed\"]').scrollBy(0, 1000)")
        time.sleep(2)
        new_height = page.evaluate("document.querySelector('div[role=\"feed\"]').scrollHeight")
        if new_height == last_height:
            logger.info(f"No more content loaded after {i + 1} scrolls.")
            break
        last_height = new_height
    logger.info("Auto-scroll completed.")

def extract_detailed_data(page, limit=None):
    """Clicks each card to get accurate URL & address."""
    results = []

    cards = page.locator("a.hfpxzc").all()
    logger.info(f"Found {len(cards)} results in list view.")

    if limit:
        cards = cards[:limit]

    for idx, card in enumerate(cards):
        try:
            logger.info(f"Opening result {idx+1}/{len(cards)}...")
            card.click()
            page.wait_for_timeout(2000)  # Wait for side panel to load

            # Name
            try:
                name = page.locator("h1.DUwDvf.lfPIob").inner_text().strip()
            except:
                name = "N/A"

            # Address
            try:
                # Get all Io6YTe divs and pick the one looking like an address
                all_texts = page.locator("div.Io6YTe").all_inner_texts()
                address = next((t.strip() for t in all_texts if "," in t and any(c.isdigit() for c in t)), "N/A")
            except:
                address = "N/A"

            # Exact URL
            place_url = page.url

            results.append({
                "Name": name,
                "URL": place_url,
                "Address": address
            })

        except Exception as e:
            logger.warning(f"⚠️ Failed to extract result {idx+1}: {e}")
            continue

    return results

def save_to_csv(data, filename):
    os.makedirs("static", exist_ok=True)
    filepath = os.path.join("static", filename)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "URL", "Address"])
        writer.writeheader()
        writer.writerows(data)
    logger.info(f"Scraping completed. Output saved to {filepath}")
    return filepath

def run_scraper(query, output_file=None, limit=None):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        search_url = f"https://www.google.com/maps/search/{quote_plus(query)}"
        logger.info(f"Navigating to {search_url}")
        page.goto(search_url, timeout=60000)

        # Scroll through results
        scroll_until_end(page, max_scrolls=20)

        # Extract accurate info
        data = extract_detailed_data(page, limit=limit)

        browser.close()

    if not output_file:
        safe_query = query.replace(" ", "_")
        output_file = f"{safe_query}_googlemaps.csv"

    filepath = save_to_csv(data, output_file)
    return data, filepath  # ✅ return both for UI display

if __name__ == "__main__":
    data, file_path = run_scraper("restaurants in Mumbai", limit=5)
    print(f"Scraped {len(data)} results. Saved to {file_path}")













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




