# plugins/indiamart.py

import subprocess
subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import csv
import os
import time
from urllib.parse import quote_plus
from utils.logger import get_logger

logger = get_logger("indiamart")
description = "Scrape supplier contact data from IndiaMART (B2B marketplace)."

def build_search_url(query):
    return f"https://dir.indiamart.com/search.mp?ss={quote_plus(query)}"

def scroll_until_end(page, max_scrolls=20):
    logger.info("Starting auto-scroll to load all results...")
    last_height = 0
    for i in range(max_scrolls):
        page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        time.sleep(3)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            logger.info(f"No more content loaded after {i + 1} scrolls.")
            break
        last_height = new_height
    logger.info("Auto-scroll completed.")

def extract_data_from_page(page):
    data = []
    try:
        cards = page.query_selector_all(".supplierInfoDiv")
        logger.info(f"Found {len(cards)} cards on current scroll.")
        for card in cards:
            company_name = card.query_selector(".companyname a")
            location = card.query_selector(".newLocationUi span.highlight")
            phone_elem = card.query_selector(".pns_h, .contactnumber .duet")
            link = card.query_selector(".companyname a")

            company = company_name.inner_text().strip() if company_name else ""
            city = location.inner_text().strip() if location else ""
            phone = phone_elem.inner_text().strip() if phone_elem else ""
            url = link.get_attribute("href") if link else ""

            data.append({
                "Company Name": company,
                "Location": city,
                "Phone": phone,
                "URL": url
            })
    except Exception as e:
        logger.error(f"Error extracting data: {e}")
    return data

def save_to_csv(data, file_path):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        if not data:
            # Write only headers if no data found
            writer = csv.DictWriter(f, fieldnames=["Company Name", "Location", "Phone", "URL"])
            writer.writeheader()
        else:
            writer = csv.DictWriter(f, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
    logger.info(f"CSV saved: {file_path} ({len(data)} rows)")

def run_scraper(query, output_file=None, limit=None):
    logger.info(f"Running IndiaMART scraper for: {query}")
    logger.info(f"Limit: {limit}")
    url = build_search_url(query)
    logger.info(f"Opening URL: {url}")

    all_data = []
    final_file_path = None

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, timeout=60000)

            try:
                page.wait_for_selector(".supplierInfoDiv", timeout=15000)
            except PlaywrightTimeoutError:
                logger.warning(".supplierInfoDiv not found — page may not have loaded correctly.")

            os.makedirs("static", exist_ok=True)
            screenshot_path = os.path.abspath("static/indiamart_debug.png")
            page.screenshot(path=screenshot_path, full_page=True)
            logger.info(f"Screenshot saved to: {screenshot_path}")

            scroll_until_end(page)
            all_data = extract_data_from_page(page)
            logger.info(f"Total extracted: {len(all_data)} records")

            if limit and all_data:
                all_data = all_data[:int(limit)]
                logger.info(f"Limit applied: {limit} → Returning {len(all_data)} records.")

            if output_file:
                final_file_path = os.path.abspath(output_file)
                logger.info(f"Saving CSV to: {final_file_path}")
                save_to_csv(all_data, final_file_path)

            browser.close()
            print(f"FOUND_COUNT: {len(all_data)}")
            return len(all_data)

        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            # Still save empty CSV if output file was requested
            if output_file:
                final_file_path = os.path.abspath(output_file)
                save_to_csv([], final_file_path)
            return 0


































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
