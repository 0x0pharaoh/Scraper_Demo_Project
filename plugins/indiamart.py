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

logger = get_logger("indiamart")
description = "Scrape supplier contact data from IndiaMART (B2B marketplace)."

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

def extract_card_data(card):
    """Extract data from a supplier card."""
    try:
        company_name = card.query_selector(".companyname a")
        location = card.query_selector(".newLocationUi span.highlight")
        phone_elem = card.query_selector(".pns_h, .contactnumber .duet")
        link = card.query_selector(".companyname a")

        company = company_name.inner_text().strip() if company_name else "N/A"
        city = location.inner_text().strip() if location else "N/A"
        phone = phone_elem.inner_text().strip() if phone_elem else "N/A"
        url = link.get_attribute("href") if link else "N/A"

        return {
            "Company Name": company,
            "Location": city,
            "Phone": phone,
            "URL": url
        }
    except Exception as e:
        logger.warning(f"Error extracting a card: {e}")
        return None

def save_to_csv(data, filepath):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Company Name", "Location", "Phone", "URL"])
        writer.writeheader()
        writer.writerows(data)
    logger.info(f"Scraping completed. Output saved to {filepath}")
    return filepath

def run_scraper(query, output_file=None, limit=None):
    target_count = limit if limit is not None else 40
    timeout_ms = 180000 if limit is None else 60000

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            page = browser.new_page()

            search_url = build_search_url(query)
            logger.info(f"Navigating to {search_url}")
            page.goto(search_url, timeout=timeout_ms)

            try:
                page.wait_for_selector(".supplierInfoDiv", timeout=15000)
            except PlaywrightTimeoutError:
                logger.warning("⚠ No supplier cards found.")
                browser.close()
                return {"file": None, "data": []}

            collected = []
            seen_entries = set()
            scrolls_done = 0
            max_scrolls = 40 if limit is None else 20
            last_cards_count = 0

            while len(collected) < target_count and scrolls_done < max_scrolls:
                cards = page.query_selector_all(".supplierInfoDiv")
                logger.info(f"Found {len(cards)} cards on scroll #{scrolls_done + 1}")

                new_cards = []
                for c in cards:
                    data = extract_card_data(c)
                    if not data:
                        continue
                    entry_key = normalize_key(data["Company Name"], data["Location"], data["Phone"])
                    if entry_key not in seen_entries:
                        new_cards.append(data)
                        seen_entries.add(entry_key)

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

            if not output_file:
                safe_query = query.replace(" ", "_")
                output_file = os.path.abspath(os.path.join("static", f"{safe_query}_indiamart.csv"))
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
