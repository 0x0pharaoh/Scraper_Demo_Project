# google_maps.py

import subprocess
subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)

import time
import csv
import os
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright
from utils.logger import get_logger

logger = get_logger("google_maps")
description = "Scrape business data from Google Maps search results"

def scroll_feed(page):
    """Scroll the results feed to load more cards."""
    try:
        page.evaluate("document.querySelector('div[role=\"feed\"]').scrollBy(0, 1000)")
    except:
        logger.warning("Could not scroll feed. Possibly no more results.")
    time.sleep(2)

def extract_card_data(page):
    """Extract data from the currently opened side panel."""
    try:
        name = page.locator("h1.DUwDvf.lfPIob").inner_text().strip()
    except:
        name = "N/A"

    try:
        all_texts = page.locator("div.Io6YTe").all_inner_texts()
        address = next((t.strip() for t in all_texts if "," in t and any(c.isdigit() for c in t)), "N/A")
    except:
        address = "N/A"

    place_url = page.url
    return {"Name": name, "URL": place_url, "Address": address}

def save_to_csv(data, filepath):
    # Use the exact output_file path provided by run_scraper
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "URL", "Address"])
        writer.writeheader()
        writer.writerows(data)
    logger.info(f"Scraping completed. Output saved to {filepath}")
    return filepath

def run_scraper(query, output_file=None, limit=None):
    target_count = limit if limit is not None else 40  # Default target when no limit is given
    timeout_ms = 180000 if limit is None else 60000  # Longer timeout if no limit

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        page = browser.new_page()

        search_url = f"https://www.google.com/maps/search/{quote_plus(query)}"
        logger.info(f"Navigating to {search_url}")
        page.goto(search_url, timeout=timeout_ms)

        collected = []
        seen_entries = set()
        visited_hrefs = set()

        max_scrolls = 30 if limit is None else 20
        scrolls_done = 0
        last_cards_count = 0

        while len(collected) < target_count and scrolls_done < max_scrolls:
            try:
                page.wait_for_selector("a.hfpxzc", timeout=15000)
            except:
                logger.warning("⚠ No result cards found.")
                break

            cards = page.locator("a.hfpxzc").all()
            logger.info(f"Found {len(cards)} cards on scroll #{scrolls_done + 1}")

            new_cards = [c for c in cards if c.get_attribute("href") not in visited_hrefs]
            logger.info(f"New cards to process: {len(new_cards)}")

            if not new_cards:
                scroll_feed(page)
                scrolls_done += 1
                if len(cards) == last_cards_count:
                    logger.info("ℹ No new cards loaded after scrolling, ending.")
                    break
                last_cards_count = len(cards)
                continue

            for card in new_cards:
                href = card.get_attribute("href")
                if not href:
                    continue

                try:
                    card.click()
                    page.wait_for_timeout(2000)
                    data = extract_card_data(page)

                    entry_key = (data["Name"].lower(), data["URL"].lower())
                    if entry_key not in seen_entries:
                        collected.append(data)
                        seen_entries.add(entry_key)
                        logger.info(f"Collected: {data['Name']}")

                    visited_hrefs.add(href)

                    if len(collected) >= target_count:
                        break
                except Exception as e:
                    logger.warning(f"Failed to process a card: {e}")
                    continue

            scroll_feed(page)
            scrolls_done += 1

        browser.close()

    if not output_file:
        safe_query = query.replace(" ", "_")
        output_file = os.path.abspath(os.path.join("static", f"{safe_query}_googlemaps.csv"))

    filepath = save_to_csv(collected, output_file)
    return {"file": filepath, "data": collected}



















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




