#!/usr/bin/env python
"""Re-export the IndiaMART session from a live, signed-in browser.

Hand-exported JSON goes stale silently: a fresh export merges new analytics
cookies over dead login ones, so the file looks current while im_iss and LGNSTR
are weeks expired and every scrape gets an empty listing. Reading the cookies
out of a real signed-in context instead cannot produce that half-dead file.

    python refresh_indiamart_session.py
"""

import json
import sys

from playwright.sync_api import sync_playwright

from plugins.indiamart_products import AUTH_COOKIES, DEFAULT_SESSION_FILE, DESKTOP_UA

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False, args=["--disable-blink-features=AutomationControlled"]
    )
    context = browser.new_context(
        user_agent=DESKTOP_UA, viewport={"width": 1400, "height": 900}, locale="en-US"
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    page = context.new_page()
    page.goto("https://www.indiamart.com/", timeout=120000)

    print("Sign in to IndiaMART in the browser window, then press Enter here.")
    input()

    cookies = context.cookies()
    missing = AUTH_COOKIES - {c["name"] for c in cookies}
    if missing:
        browser.close()
        sys.exit(f"Still signed out: no {', '.join(sorted(missing))}. Nothing written.")

    # Keys load_session() reads: session_data is sessionStorage, browser_storage
    # is localStorage.
    session = {
        "site": "indiamart.com",
        "cookies": cookies,
        "session_data": page.evaluate("Object.fromEntries(Object.entries(sessionStorage))"),
        "browser_storage": page.evaluate("Object.fromEntries(Object.entries(localStorage))"),
    }
    browser.close()

with open(DEFAULT_SESSION_FILE, "w", encoding="utf-8") as f:
    json.dump(session, f, indent=2)
print(f"Wrote {DEFAULT_SESSION_FILE}: {len(cookies)} cookies.")
