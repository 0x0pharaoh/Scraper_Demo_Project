# google_maps.py

import subprocess
import sys
import time
import csv
import os
import re
import json
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from utils.logger import get_logger

# Chromium is normally installed by runner.py before this module loads. Keep a
# best-effort fallback for direct imports, but never let it kill the import.
subprocess.run(
    [sys.executable, "-m", "playwright", "install", "chromium"],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

logger = get_logger("google_maps")
description = "Scrape business data from Google Maps search results"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

GOOGLE_MAPS_FIELDNAMES = [
    "Name",
    "URL",
    "Address",
    "City",
    "State",
    "Zipcode",
    "Mobile",
    "Website",
    "Image",
    "Images",
    "Rating",
    "ReviewCount",
    "Reviews",
]

def scroll_feed(page, search_url=None):
    """Scroll the results feed to load more cards. Returns True if it moved.

    Two things this has to handle: after each business the scraper sits on a
    place panel, where div[role='feed'] does not exist at all, so the results
    list has to be restored first. And the feed viewport is ~970px while the
    list keeps growing, so a fixed scrollBy(0, 1000) barely advances it -
    jump to the current bottom instead.
    """
    try:
        if search_url and page.locator("a.hfpxzc").count() == 0:
            _restore_results_feed(page, search_url)
    except Exception as e:
        logger.debug(f"Could not restore feed before scrolling: {e}")

    try:
        moved = page.evaluate(
            """() => {
                let el = document.querySelector('div[role="feed"]');
                if (!el) {
                    const a = document.querySelector('a.hfpxzc');
                    el = a ? a.parentElement : null;
                    while (el && el !== document.body && el.scrollHeight <= el.clientHeight + 20)
                        el = el.parentElement;
                    if (el === document.body) el = null;
                }
                if (!el) return null;
                const before = el.scrollTop;
                el.scrollTo(0, el.scrollHeight);
                return el.scrollTop > before;
            }"""
        )
    except Exception as e:
        logger.debug(f"Feed scroll failed: {e}")
        moved = None

    if moved is None:
        logger.warning("Could not find the results feed to scroll.")
        return False

    page.wait_for_timeout(2000)
    return bool(moved)

def _safe_inner_text(locator, default=None, timeout=1500):
    try:
        return locator.inner_text(timeout=timeout).strip()
    except Exception:
        return default

def _safe_attribute(locator, attr, default=None, timeout=1500):
    try:
        return locator.get_attribute(attr, timeout=timeout)
    except Exception:
        return default

def _normalize_review_count(value):
    if not value:
        return None
    match = re.search(r"([\d,]+)\s*(?:google\s+)?reviews?", value, re.I)
    if not match:
        match = re.search(r"(?:more\s+)?reviews?\s*\(?([\d,]+)\)?", value, re.I)
    if not match:
        match = re.search(r"\(([\d,]+)\)", value)
    if not match:
        match = re.search(r"([\d,]+)", value)
    return match.group(1).replace(",", "") if match else None

def _parse_rating_label(label):
    if not label:
        return {"Rating": None, "ReviewCount": None}

    rating_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:stars?|star rating)", label, re.I)
    review_match = re.search(r"([\d,]+)\s*(?:google\s+)?(?:reviews?|ratings?)", label, re.I)
    return {
        "Rating": rating_match.group(1) if rating_match else None,
        "ReviewCount": review_match.group(1).replace(",", "") if review_match else None
    }

def _extract_review_count_from_page(page):
    """Find business-level review count without using absolute, brittle XPaths."""
    try:
        count_texts = page.evaluate(
            """
            () => {
                const root = document.querySelector("div[role='main']") || document.body;
                const nodes = Array.from(root.querySelectorAll("button, span, div[aria-label], div[role='link']"));
                return nodes
                    .filter(el => !el.closest(".jftiEf"))
                    .map(el => `${el.getAttribute("aria-label") || ""} ${el.innerText || ""}`.trim())
                    .filter(text => /\\b[\\d,]+\\s+(?:Google\\s+)?reviews?\\b/i.test(text))
                    .slice(0, 30);
            }
            """
        )
        for text in count_texts:
            count = _normalize_review_count(text)
            if count:
                return count
    except Exception as e:
        logger.debug(f"Could not extract review count from page text: {e}")
    return None

def _click_with_retry(locator, attempts=3, timeout=3000):
    for attempt in range(attempts):
        try:
            locator.first.click(timeout=timeout)
            return True
        except Exception as e:
            logger.debug(f"Click attempt {attempt + 1} failed: {e}")
            time.sleep(0.5)
    return False

def _open_result_card(page, card, href):
    """Open a Maps result while avoiding long waits on virtualized card locators."""
    # Resolve by href first: the feed is reloaded between businesses, so a
    # positional locator captured earlier can point at a different result.
    if href:
        try:
            exact = page.locator(f'a.hfpxzc[href="{href}"]')
            if exact.count() > 0:
                exact.first.scroll_into_view_if_needed(timeout=1500)
                exact.first.click(timeout=3000)
                return True
        except Exception as e:
            logger.debug(f"Href-addressed result-card click failed for {href}: {e}")

    try:
        card.scroll_into_view_if_needed(timeout=1500)
        card.click(timeout=3000)
        return True
    except Exception as e:
        logger.debug(f"Normal result-card click failed for {href}: {e}")

    try:
        card.click(timeout=2000, force=True)
        return True
    except Exception as e:
        logger.debug(f"Forced result-card click failed for {href}: {e}")

    try:
        clicked = page.evaluate(
            """
            targetHref => {
                const anchors = Array.from(document.querySelectorAll('a.hfpxzc'));
                const anchor = anchors.find(a => a.href === targetHref || a.getAttribute('href') === targetHref);
                if (!anchor) return false;
                anchor.scrollIntoView({block: 'center', inline: 'center'});
                anchor.click();
                return true;
            }
            """,
            href
        )
        if clicked:
            return True
    except Exception as e:
        logger.debug(f"JavaScript result-card click failed for {href}: {e}")

    try:
        # Last resort. Google serves a reduced page on direct navigation, so
        # reviews will usually be missing for this business.
        logger.warning(f"Falling back to direct navigation (reviews likely unavailable): {href}")
        page.goto(href, timeout=30000, wait_until="domcontentloaded")
        return True
    except Exception as e:
        logger.warning(f"Could not open result card URL {href}: {e}")
        return False

def _active_dialog(page):
    """Return the currently visible dialog/lightbox when Google Maps opens one."""
    # NOTE: do not add pane.reviewChart here - that jsaction is on the ratings
    # histogram (div.Bd93Zb), which is inline page content, not a dialog. Treating
    # it as one scopes review collection to a table with zero review cards.
    dialog_selectors = [
        "div[role='dialog']",
        "div[aria-modal='true']",
        "#lightbox",
    ]
    for selector in dialog_selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0 and locator.last.is_visible(timeout=1000):
                return locator.last
        except Exception:
            continue
    return None

def _on_place_page(page):
    """True while the business panel is still the thing on screen."""
    try:
        return "/maps/place/" in page.url
    except Exception:
        return False

def _restore_place_page(page, place_url):
    """Contribute buttons ('Add photos', 'Write a review') bounce us to the Google
    sign-in screen, which silently destroys the panel every later step depends on.
    Detect that and navigate back."""
    if _on_place_page(page) or not place_url:
        return _on_place_page(page)
    logger.warning(f"Left the place page (now {page.url[:60]}...). Restoring {place_url[:60]}...")
    try:
        page.goto(place_url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_selector("h1.DUwDvf, .DUwDvf", timeout=15000)
        return True
    except Exception as e:
        logger.warning(f"Could not restore place page: {e}")
        return False

def _restore_results_feed(page, search_url, attempts=2):
    """Return to the search results list so the next business starts from a clean SPA state."""
    for attempt in range(attempts):
        try:
            if page.locator("a.hfpxzc").count() > 0:
                return True
        except Exception:
            pass
        try:
            page.goto(search_url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_selector("a.hfpxzc", timeout=20000)
            return True
        except Exception as e:
            logger.debug(f"Feed restore attempt {attempt + 1} failed: {e}")
    return False

def _close_active_dialog(page):
    """Best-effort close that keeps the scraper on the opened business page."""
    try:
        dialog = _active_dialog(page)
        if not dialog:
            # Nothing is open. A blind Escape here just navigates the panel away.
            return
        close_button = dialog.locator(
            "button[aria-label='Close'], button[aria-label*='Close'], "
            "button:has-text('Close')"
        )
        if close_button.count() > 0 and _click_with_retry(close_button, attempts=2, timeout=2000):
            page.wait_for_timeout(600)
            return
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    except Exception as e:
        logger.debug(f"Could not close dialog cleanly: {e}")

def _scroll_active_container(page, dialog=None):
    """Scroll only the visible dialog/panel, not the whole page."""
    try:
        target = dialog or _active_dialog(page)
        if target:
            target.evaluate(
                """
                root => {
                    const candidates = [root, ...root.querySelectorAll('div, section')];
                    const scrollable = candidates
                        .filter(el => el.scrollHeight > el.clientHeight + 20)
                        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
                    if (scrollable) scrollable.scrollBy(0, Math.max(700, scrollable.clientHeight));
                }
                """
            )
        else:
            # No dialog open (e.g. Reviews tab renders inline in the side panel) -
            # scroll the nearest scrollable ancestor of a known review/content node instead.
            page.evaluate(
                """
                () => {
                    const anchor = document.querySelector('div.jftiEf, div[data-review-id], div.m6QErb');
                    if (!anchor) return;
                    let el = anchor.parentElement;
                    while (el && el !== document.body) {
                        if (el.scrollHeight > el.clientHeight + 20) {
                            el.scrollBy(0, Math.max(700, el.clientHeight));
                            return;
                        }
                        el = el.parentElement;
                    }
                }
                """
            )
        page.wait_for_timeout(800)
    except Exception as e:
        logger.debug(f"Could not scroll active dialog: {e}")

def _normalize_image_url(url):
    if not url or url.startswith("data:"):
        return None

    cleaned = url.replace("\\u003d", "=").replace("\\u0026", "&")
    if "googleusercontent.com" in cleaned:
        base = re.sub(r"=w\d+(?:-h\d+)?(?:-[a-z0-9]+)*$", "", cleaned)
        base = re.sub(r"=s\d+(?:-[a-z0-9]+)*$", "", base)
        return f"{base}=s0"
    return cleaned

def _collect_visible_image_urls(page):
    urls = []
    selectors = [
        "div[role='dialog'] img[src]",
        "div[aria-modal='true'] img[src]",
        "#lightbox img[src]",
        "img[src*='googleusercontent.com']"
    ]
    for selector in selectors:
        try:
            for img in page.locator(selector).all():
                src = _safe_attribute(img, "src")
                normalized = _normalize_image_url(src)
                if normalized:
                    urls.append(normalized)
        except Exception:
            continue

    try:
        background_urls = page.evaluate(
            """
            () => Array.from(document.querySelectorAll("div[role='dialog'] *, #lightbox *"))
                .map(el => getComputedStyle(el).backgroundImage)
                .filter(bg => bg && bg.includes('url('))
                .map(bg => bg.match(/url\\(["']?(.*?)["']?\\)/)?.[1])
                .filter(Boolean)
            """
        )
        for url in background_urls:
            normalized = _normalize_image_url(url)
            if normalized:
                urls.append(normalized)
    except Exception:
        pass

    return urls

def _rating_from_panel(page):
    """Read rating/count from the open panel's own header (div.F7nice, e.g. '4.9\\n(24)').

    Must be tried first: the search feed stays in the DOM behind the panel, so a
    page-wide star-label lookup happily returns a different business's numbers.
    """
    text = _safe_inner_text(page.locator("div.F7nice").first)
    if not text:
        return {"Rating": None, "ReviewCount": None}
    rating = re.search(r"(\d+(?:\.\d+)?)", text)
    count = (re.search(r"\(([\d,]+)\)", text)
             or re.search(r"([\d,]+)\s*(?:google\s+)?reviews?", text, re.I))
    return {
        "Rating": rating.group(1) if rating else None,
        "ReviewCount": count.group(1).replace(",", "") if count else None,
    }

def get_rating(page) -> dict:
    """Extract overall business rating and total review count."""
    try:
        parsed = _rating_from_panel(page)
        if parsed["Rating"] and parsed["ReviewCount"]:
            return parsed

        rating_label = _safe_attribute(
            page.locator("span[role='img'][aria-label*='stars'], span[aria-label*='stars']").first,
            "aria-label"
        )
        fallback = _parse_rating_label(rating_label)
        parsed = {
            "Rating": parsed["Rating"] or fallback["Rating"],
            "ReviewCount": parsed["ReviewCount"] or fallback["ReviewCount"],
        }

        if not parsed["Rating"]:
            parsed["Rating"] = _safe_inner_text(page.locator("div.F7nice span[aria-hidden='true']").first)

        if not parsed["ReviewCount"]:
            parsed["ReviewCount"] = _extract_review_count_from_page(page)

        if not parsed["ReviewCount"]:
            review_text = (
                _safe_attribute(
                    page.locator(
                        "button[aria-label*='reviews'], button[aria-label*='Reviews'], "
                        "span[aria-label*='reviews'], span[aria-label*='Reviews']"
                    ).first,
                    "aria-label"
                )
                or _safe_inner_text(
                    page.locator(
                        "button[aria-label*='reviews'], button[aria-label*='Reviews'], "
                        "button:has-text('reviews'), button:has-text('Reviews'), div.F7nice span"
                    ).first
                )
            )
            parsed["ReviewCount"] = _normalize_review_count(review_text)

        if not parsed["ReviewCount"]:
            review_text = _safe_inner_text(
                page.locator(
                    "button[aria-label*='reviews'], button[aria-label*='Reviews'], "
                    "button:has-text('reviews'), button:has-text('Reviews'), div.F7nice span"
                ).last
            )
            parsed["ReviewCount"] = _normalize_review_count(review_text)

        return parsed
    except Exception as e:
        logger.warning(f"Failed to extract rating: {e}")
        return {"Rating": None, "ReviewCount": None}

# Clicking any of these opens a Google account wall, which navigates away from the
# business page and silently kills every extraction step that runs afterwards.
CONTRIBUTE_LABELS = ("add a photo", "add photo", "add photos", "contribute",
                     "upload", "write a review", "sign in", "add missing")

def _is_contribute_control(locator):
    label = (_safe_attribute(locator, "aria-label") or "").lower()
    text = (_safe_inner_text(locator) or "").lower()
    combined = f"{label} {text}"
    return any(bad in combined for bad in CONTRIBUTE_LABELS)

def get_images(page, limit: int = 10) -> list:
    """Open the photos gallery and collect unique high-resolution image URLs."""
    images = []
    seen = set()
    place_url = page.url if _on_place_page(page) else None
    # The real gallery button carries the text "See photos" and an EMPTY aria-label,
    # so match on text. Never match aria-label*='photos' - that hits "Add photos &
    # videos", which is a sign-in wall.
    open_selectors = [
        "button:has-text('See photos')",
        "button.Dx2nRe",
        "button[aria-label*='See all photos']",
        "div.ZKCDEc button",
        "div.ZKCDEc img",
    ]

    try:
        opened = False
        for selector in open_selectors:
            locator = page.locator(selector)
            try:
                if locator.count() == 0:
                    continue
                # Never click a contribute/upload control - it redirects to sign-in.
                candidate = None
                for item in locator.all()[:8]:
                    if not _is_contribute_control(item):
                        candidate = item
                        break
                if candidate is None:
                    continue
                if _click_with_retry(candidate, attempts=2, timeout=3000):
                    opened = True
                    break
            except Exception:
                continue

        if not opened:
            return images

        # A stray click may still have bounced us to an account wall.
        if not _on_place_page(page):
            _restore_place_page(page, place_url)
            return images

        try:
            page.wait_for_selector("div[role='dialog'] img, #lightbox img, img[src*='googleusercontent.com']", timeout=7000)
        except PlaywrightTimeoutError:
            logger.debug("Photos gallery opened but no images became visible.")

        stagnant_scrolls = 0
        previous_count = 0
        while len(images) < limit and stagnant_scrolls < 4:
            for url in _collect_visible_image_urls(page):
                key = re.sub(r"=s0$", "", url)
                if key not in seen:
                    seen.add(key)
                    images.append(url)
                    if len(images) >= limit:
                        break

            if len(images) == previous_count:
                stagnant_scrolls += 1
            else:
                stagnant_scrolls = 0
                previous_count = len(images)

            if len(images) < limit:
                _scroll_active_container(page)
    except Exception as e:
        logger.warning(f"Failed to collect gallery images: {e}")
    finally:
        _close_active_dialog(page)
        # The panel must survive for the review pass that runs next.
        _restore_place_page(page, place_url)

    return images[:limit]

def _open_reviews_panel(page, attempts=3):
    selectors = [
        "button[role='tab'][aria-label*='eview']",
        "div[role='tab'][aria-label*='eview']",
        "button[aria-label*='eview']",
        "button[role='tab']:has-text('Reviews')",
        "div[role='tab']:has-text('Reviews')",
        "button:has-text('Reviews')",
    ]

    for attempt in range(attempts):
        candidates_seen = []
        for selector in selectors:
            locator = page.locator(selector)
            try:
                for candidate in locator.all():
                    label = (_safe_attribute(candidate, "aria-label") or "").lower()
                    text = (_safe_inner_text(candidate) or "").lower()
                    combined = f"{label} {text}".strip()
                    if combined:
                        candidates_seen.append(combined)
                    if any(blocked in combined for blocked in ("write a review", "legal disclosure", "learn more")):
                        continue
                    if not combined:
                        continue
                    if _click_with_retry(candidate, attempts=2, timeout=3000):
                        try:
                            page.wait_for_selector(
                                "div[data-review-id], div.jftiEf",
                                timeout=5000
                            )
                        except PlaywrightTimeoutError:
                            pass
                        return True
            except Exception:
                continue
        logger.debug(f"Reviews tab not found on attempt {attempt + 1}. Candidates seen: {candidates_seen[:15]}")
        page.wait_for_timeout(500)
    return False

def _open_overview_tab(page):
    """Return to Overview. The gallery button only exists there, and the review
    pass leaves the panel parked on the Reviews tab."""
    try:
        tab = page.locator("button[role='tab'][aria-label*='verview'], [role='tab'][aria-label*='verview']")
        if tab.count() > 0:
            _click_with_retry(tab, attempts=2, timeout=3000)
            page.wait_for_timeout(1200)
            return True
    except Exception as e:
        logger.debug(f"Could not return to Overview tab: {e}")
    return False

def _expand_review_texts(scope):
    """Expand truncated review bodies.

    Scoped to review cards on purpose: a page-wide "More" search also matches
    Street View and photo controls (:has-text is a case-insensitive substring
    match), and clicking those navigates off the business page entirely.
    """
    try:
        more_buttons = scope.locator(
            "div.jftiEf button.w8nwRe, "
            "div.jftiEf button:has-text('More'), "
            "div[data-review-id] button:has-text('More')"
        )
        for button in more_buttons.all()[:20]:
            try:
                if _is_contribute_control(button):
                    continue
                if button.is_visible(timeout=500):
                    button.click(timeout=1000)
            except Exception:
                continue
    except Exception:
        pass

def _extract_review_from_card(card):
    author = (
        _safe_inner_text(card.locator(".d4r55").first)
        or _safe_inner_text(card.locator("[class*='fontTitle']").first)
        or _safe_inner_text(card.locator("button, a").first)
    )
    rating_label = (
        _safe_attribute(card.locator("span[role='img'][aria-label*='star']").first, "aria-label")
        or _safe_attribute(card.locator("[aria-label*='star']").first, "aria-label")
    )
    rating_match = re.search(r"(\d+(?:\.\d+)?)", rating_label or "")
    date = (
        _safe_inner_text(card.locator(".rsqaWe").first)
        or _safe_inner_text(card.locator("span:has-text('ago')").first)
    )
    review_text = (
        _safe_inner_text(card.locator(".wiI7pd").first)
        or _safe_inner_text(card.locator("[data-review-id] span").last)
    )

    if not any([author, rating_match, date, review_text]):
        return None

    return {
        "Author": author,
        "Rating": rating_match.group(1) if rating_match else None,
        "Date": date,
        "Review": review_text
    }

def _collect_reviews_from_scope(scope, reviews, seen, limit):
    _expand_review_texts(scope)
    cards = scope.locator("div.jftiEf").all()
    if not cards:
        cards = scope.locator("div[data-review-id]:has(.d4r55), div[data-review-id]:has(.wiI7pd)").all()

    for card in cards:
        review = _extract_review_from_card(card)
        if not review:
            continue
        key = normalize_key(
            review.get("Author"),
            review.get("Date"),
            review.get("Rating"),
            review.get("Review")
        )
        if key not in seen:
            seen.add(key)
            reviews.append(review)
            if len(reviews) >= limit:
                break

def get_reviews(page, limit: int = 10) -> list:
    """Open the reviews panel and collect unique review dictionaries."""
    reviews = []
    seen = set()
    opened_reviews = False
    place_url = page.url if _on_place_page(page) else None

    try:
        _collect_reviews_from_scope(page, reviews, seen, limit)
        if len(reviews) >= limit:
            return reviews

        opened_reviews = _open_reviews_panel(page)
        if not opened_reviews:
            logger.debug("Reviews tab could not be opened; keeping preview reviews only.")
            return reviews

        try:
            page.wait_for_selector("div[role='dialog'], div[data-review-id], div.jftiEf", timeout=7000)
        except PlaywrightTimeoutError:
            logger.debug("Reviews panel opened but review cards were not immediately visible.")

        stagnant_scrolls = 0
        previous_count = 0
        while len(reviews) < limit and stagnant_scrolls < 5:
            # Always scope to the page. Reviews render inline in the side panel,
            # and a place page carries unrelated div[role='dialog'] nodes that
            # _active_dialog would otherwise pick, scoping us to zero cards.
            _collect_reviews_from_scope(page, reviews, seen, limit)

            if len(reviews) == previous_count:
                stagnant_scrolls += 1
            else:
                stagnant_scrolls = 0
                previous_count = len(reviews)

            if len(reviews) < limit:
                _scroll_active_container(page, None)
    except Exception as e:
        logger.warning(f"Failed to collect reviews: {e}")
    finally:
        if opened_reviews:
            _close_active_dialog(page)
        _restore_place_page(page, place_url)

    return reviews[:limit]
    
def parse_business_details(data_list):
   
    result = {
        "Address": None,
        "City": None,
        "State": None,
        "Zipcode": None,
        "Mobile": None,
        "Website": None
    }

    for item in data_list:
        item = item.strip()

        # Full address (look for commas and zipcode)
        if "," in item and re.search(r"\d{6}", item):
            result["Address"] = item
            # Extract zipcode
            match = re.search(r"\b\d{6}\b", item)
            if match:
                result["Zipcode"] = match.group(0)
            # Extract state (last word before zipcode)
            parts = item.split(",")
            if len(parts) >= 2:
                state_part = parts[-1].strip()
                if result["Zipcode"] and state_part.endswith(result["Zipcode"]):
                    state_part = state_part.replace(result["Zipcode"], "").strip()
                result["State"] = state_part
            # Extract city (second last part)
            if len(parts) >= 3:
                result["City"] = parts[-2].strip()

        # Website
        elif re.search(r"(https?:\/\/)?[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", item):
            result["Website"] = item

        elif re.search(r"\b\d[\d\s]{4,20}\d\b", item):
            number = re.search(r"\b\d[\d\s]{4,20}\d\b", item).group(0)
            # Normalize: remove spaces
            result["Mobile"] = re.sub(r"\s+", "", number)

    return result

def extract_card_data(page):
    """Extract data from the currently opened side panel."""
    try:
        page.wait_for_selector(".DUwDvf, h1.DUwDvf, div.Io6YTe", timeout=10000)
        name = (
            _safe_inner_text(page.locator("h1[aria-level='1']").first)
            or _safe_inner_text(page.locator("h1.DUwDvf.lfPIob").first)
            or _safe_inner_text(page.locator(".DUwDvf").first)
            or "N/A"
        )
    except:
        name = "N/A"
    
    try:
        all_texts = page.locator("div.Io6YTe").all_inner_texts()
    except:
        all_texts = []
        
    try:
        image_src = page.locator("div.ZKCDEc img").first.get_attribute("src")
    except:
        image_src = None

    place_url = page.url
    rating = get_rating(page)
    details = parse_business_details(all_texts)
    data = {
        "Name": name,
        "URL": place_url,
        "Address": details.get("Address"),
        "City": details.get("City"),
        "State": details.get("State"),
        "Zipcode": details.get("Zipcode"),
        "Mobile": details.get("Mobile"),
        "Website": details.get("Website"),
        "Image": image_src,
        "Images": [],
        "Rating": rating.get("Rating"),
        "ReviewCount": rating.get("ReviewCount"),
        "Reviews": []
    }

    # Reviews first: they need the intact place panel, and any gallery interaction
    # risks bouncing the page to an account wall.
    try:
        data["Reviews"] = get_reviews(page, limit=10)
    except Exception as e:
        logger.warning(f"Review extraction failed for {name}: {e}")
        data["Reviews"] = []

    _restore_place_page(page, place_url)
    _open_overview_tab(page)

    try:
        data["Images"] = get_images(page, limit=15)
    except Exception as e:
        logger.warning(f"Image extraction failed for {name}: {e}")
        data["Images"] = []

    return data

def _place_key(data):
    """Stable identity for a business: name plus address.

    Not the URL - Google varies the data=/entry= parameters between visits to the
    same place, so URL keys let duplicates through. Not the URL's !1s feature id
    either: page.url can still hold the previous business when the panel is read,
    which merges two unrelated companies. Name+address survives both, and real
    branches of one chain differ by address so they stay separate.
    """
    return normalize_key(data.get("Name"), data.get("Address"))

def _ensure_parent_dir(filepath):
    """makedirs() on a bare filename raises FileNotFoundError - skip when no dir part."""
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)

def normalize_key(*values):
    """Normalize values for duplicate detection."""
    def clean(v):
        return re.sub(r"\s+", " ", v.strip().lower())
    return tuple(clean(str(v)) for v in values)

def save_to_csv(data, filepath, fieldnames=None):
    fieldnames = fieldnames or GOOGLE_MAPS_FIELDNAMES
    _ensure_parent_dir(filepath)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            csv_row = row.copy()
            for key in ("Images", "Reviews"):
                if key in csv_row:
                    csv_row[key] = json.dumps(csv_row[key], ensure_ascii=False)
            writer.writerow(csv_row)
    logger.info(f"Scraping completed. Output saved to {filepath}")
    return filepath

def save_to_json(data, filepath):
    _ensure_parent_dir(filepath)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Scraping completed. Output saved to {filepath}")
    return filepath

def run_scraper(query, output_file=None, limit=None):
    target_count = limit if limit is not None else 40
    timeout_ms = 180000 if limit is None else 60000

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        # Google serves a reviews-stripped page to the default headless UA: the
        # Reviews tab is absent entirely and div.jftiEf never renders. A desktop
        # UA plus a full-size viewport restores the normal UI.
        context = browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        search_url = f"https://www.google.com/maps/search/{quote_plus(query)}"
        logger.info(f"Navigating to {search_url}")
        page.goto(search_url, timeout=timeout_ms)

        collected = []
        seen_entries = {}  # place key -> index in collected
        visited_hrefs = set()

        max_scrolls = 100000 if limit is None else 20
        scrolls_done = 0
        last_cards_count = 0
        stagnant_feed_rounds = 0

        while len(collected) < target_count and scrolls_done < max_scrolls:
            try:
                page.wait_for_selector("a.hfpxzc", timeout=15000)
            except:
                logger.warning("⚠ No result cards found.")
                break

            cards = page.locator("a.hfpxzc").all()
            logger.info(f"Found {len(cards)} cards on scroll #{scrolls_done + 1}")

            new_cards = []
            for card in cards:
                href = _safe_attribute(card, "href", timeout=1000)
                if href and href not in visited_hrefs:
                    new_cards.append((card, href))
            logger.info(f"New cards to process: {len(new_cards)}")

            if not new_cards:
                scroll_feed(page, search_url)
                scrolls_done += 1
                if len(cards) == last_cards_count:
                    # The feed loads lazily: a round that adds nothing is common
                    # and is not the end of the list. Only give up after several.
                    stagnant_feed_rounds += 1
                    if stagnant_feed_rounds >= 3:
                        logger.info("No new cards after 3 scroll rounds, ending.")
                        break
                else:
                    stagnant_feed_rounds = 0
                    last_cards_count = len(cards)
                continue

            for card, href in new_cards:
                try:
                    # Each business must start from the results feed. Reviews only
                    # render when the panel is reached by an in-page card click;
                    # navigating to a place URL directly serves a stripped page.
                    if not _restore_results_feed(page, search_url):
                        logger.warning("Results feed unavailable; stopping this pass.")
                        break

                    if not _open_result_card(page, card, href):
                        visited_hrefs.add(href)
                        continue
                    page.wait_for_selector(".DUwDvf, h1.DUwDvf, div.Io6YTe", timeout=15000)
                    data = extract_card_data(page)

                    entry_key = _place_key(data)
                    existing_index = seen_entries.get(entry_key)
                    if existing_index is None:
                        seen_entries[entry_key] = len(collected)
                        collected.append(data)
                        logger.info(f"Collected: {data['Name']}")
                    elif len(data.get("Reviews") or []) > len(
                        collected[existing_index].get("Reviews") or []
                    ):
                        # A repeat visit often loads more reviews than the first
                        # pass did, so keep whichever row is richer.
                        collected[existing_index] = data
                        logger.info(f"Re-collected with more reviews: {data['Name'][:50]}")
                    else:
                        logger.debug(f"Skipped duplicate: {data['Name'][:50]}")

                    visited_hrefs.add(href)

                    if len(collected) >= target_count:
                        break
                except Exception as e:
                    logger.warning(f"Failed to process a card: {e}")
                    continue

            scroll_feed(page, search_url)
            scrolls_done += 1

        browser.close()

    if not output_file:
            safe_query = query.replace(" ", "_")
            output_file = os.path.abspath(os.path.join("static", f"{safe_query}_google_maps.csv"))
    else:
        # Ensure absolute path always
        output_file = os.path.abspath(output_file)

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    if output_file.lower().endswith(".json"):
        filepath = save_to_json(collected[:target_count], output_file)
    else:
        filepath = save_to_csv(collected[:target_count], output_file, GOOGLE_MAPS_FIELDNAMES)
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




