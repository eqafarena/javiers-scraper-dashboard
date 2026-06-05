import os
import re
import time
import hashlib
import logging
from datetime import datetime
from playwright.sync_api import sync_playwright
from rapidfuzz import fuzz

GOOGLE_MAPS_REVIEWS_URL = (
    "https://www.google.com/maps/place/Javier%E2%80%99s+Downtown/"
    "@42.3368423,-83.0476069,16z/data=!4m8!3m7!"
    "1s0x883b2db5aa8d2b05:0x8123192805412075!"
    "8m2!3d42.3368384!4d-83.045032!9m1!1b1!"
    "16s%2Fg%2F11y4q2xl14?entry=ttu"
)

KEY_NAMES_FILE = "key_names.txt"
OUTPUT_FILE = "output.txt"

SHORT_NAME_THRESHOLD = 4
FUZZY_THRESHOLD = 80
LOOKBACK_DAYS = 7

log = logging.getLogger(__name__)


def load_key_names() -> list:
    if not os.path.exists(KEY_NAMES_FILE):
        log.warning("%s not found. No names will be matched.", KEY_NAMES_FILE)
        return []
    names = []
    with open(KEY_NAMES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 1)
            if len(parts) == 2:
                names.append({"name": parts[0].strip(), "display_name": parts[1].strip()})
    return names


def make_review_id(author: str, text: str) -> str:
    raw = f"{author}||{text}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def parse_days_ago(published_at: str) -> int:
    s = re.sub(r'\ban?\b', '1', published_at.lower().strip())
    m = re.search(r'(\d+)\s*(second|minute|hour|day|week|month|year)', s)
    if not m:
        return 0
    n = int(m.group(1))
    return n * {'second': 0, 'minute': 0, 'hour': 0,
                'day': 1, 'week': 7, 'month': 30, 'year': 365}[m.group(2)]


def match_names_in_text(review_text: str, key_names: list) -> list:
    matches = []
    seen_display_names = set()
    text_lower = review_text.lower()

    for entry in key_names:
        variant = entry["name"].lower()
        display = entry["display_name"]

        if display in seen_display_names:
            continue

        matched = False

        if len(variant) <= SHORT_NAME_THRESHOLD:
            pattern = rf'\b{re.escape(variant)}\b'
            if re.search(pattern, text_lower):
                matched = True
        else:
            words = re.findall(r'\b\w+\b', text_lower)
            for word in words:
                if fuzz.ratio(variant, word) >= FUZZY_THRESHOLD:
                    matched = True
                    break
            if not matched and " " in variant:
                for i in range(len(words) - 1):
                    bigram = f"{words[i]} {words[i+1]}"
                    if fuzz.ratio(variant, bigram) >= FUZZY_THRESHOLD:
                        matched = True
                        break

        if matched:
            matches.append({"name": variant, "display_name": display})
            seen_display_names.add(display)

    return matches


def _try_sort_newest(page) -> bool:
    sort_button_selectors = [
        '[data-value="Sort"]',
        'button[aria-label="Sort reviews"]',
        '[aria-label="Sort reviews"]',
        'button[aria-label*="Sort"]',
        '[jsaction*="reviewSort"]',
        '[jsaction*="sort"]',
    ]

    clicked_sort = False
    for sel in sort_button_selectors:
        try:
            page.click(sel, timeout=2000)
            clicked_sort = True
            break
        except Exception:
            continue

    if not clicked_sort:
        try:
            page.get_by_role("button", name=re.compile("sort", re.IGNORECASE)).first.click(timeout=2000)
            clicked_sort = True
        except Exception:
            pass

    if not clicked_sort:
        return False

    time.sleep(1)

    newest_selectors = [
        'li[data-index="1"]',
        '[data-index="1"]',
        '[role="menuitemradio"]:nth-child(2)',
        '[role="option"]:nth-child(2)',
    ]

    for sel in newest_selectors:
        try:
            page.click(sel, timeout=2000)
            time.sleep(2)
            return True
        except Exception:
            continue

    try:
        page.get_by_text("Newest", exact=True).click(timeout=2000)
        time.sleep(2)
        return True
    except Exception:
        pass

    try:
        page.get_by_role("menuitemradio", name=re.compile("newest", re.IGNORECASE)).click(timeout=2000)
        time.sleep(2)
        return True
    except Exception:
        pass

    return False


def _parse_card(el) -> dict | None:
    try:
        review_id = el.get_attribute("data-review-id", timeout=2000) or ""

        try:
            author = el.locator('div[class*="d4r55"]').first.inner_text(timeout=2000).strip()
        except Exception:
            author = "Unknown"

        stars_label = ""
        try:
            stars_label = el.locator('span[role="img"]').first.get_attribute("aria-label", timeout=2000) or ""
        except Exception:
            pass
        rating = 0
        if stars_label:
            m = re.search(r'(\d)', stars_label)
            if m:
                rating = int(m.group(1))

        try:
            text = el.locator('span[class*="wiI7pd"]').first.inner_text(timeout=2000).strip()
        except Exception:
            text = ""

        try:
            published_at = el.locator('span[class*="rsqaWe"]').first.inner_text(timeout=2000).strip()
        except Exception:
            published_at = ""

        if not text:
            return None

        if not review_id:
            review_id = make_review_id(author, text)

        return {
            "id": review_id,
            "author": author,
            "rating": rating,
            "text": text,
            "published_at": published_at,
            "days_ago": parse_days_ago(published_at),
        }
    except Exception:
        return None


def scrape_reviews() -> list:
    seen: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            java_script_enabled=True,
        )

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = context.new_page()

        log.info("Warming up via Google homepage...")
        page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        log.info("Navigating to Javier's reviews...")
        page.goto(GOOGLE_MAPS_REVIEWS_URL, wait_until="domcontentloaded", timeout=60000)
        log.info("Waiting for page to fully render...")
        time.sleep(8)

        try:
            page.click('button:has-text("Accept all")', timeout=3000)
            time.sleep(2)
        except Exception:
            pass

        log.info("Page title: %s", page.title())
        log.info("Current URL: %s", page.url)

        scrollable = None
        try:
            feed = page.locator('div[role="feed"]').first
            if feed.count() > 0:
                scrollable = feed
                log.info("Found reviews feed container.")
        except Exception:
            pass

        if scrollable is None:
            try:
                all_containers = page.locator('.m6QErb').all()
                best, best_count = None, 0
                for container in all_containers:
                    try:
                        count = container.evaluate("el => el.children.length")
                        if count > best_count:
                            best_count, best = count, container
                    except Exception:
                        pass
                if best and best_count > 5:
                    scrollable = best
                    log.info("Found scroll container with %d children.", best_count)
            except Exception as e:
                log.error("Error finding scroll container: %s", e)

        if scrollable is None:
            log.error("Could not find reviews container. Saving page_dump.html for inspection.")
            html = page.content()
            with open("page_dump.html", "w", encoding="utf-8") as f:
                f.write(html)
            browser.close()
            return []

        if _try_sort_newest(page):
            log.info("Sorted by newest.")
        else:
            log.warning("Could not sort by newest — sorting client-side by parsed date instead.")

        scroll_box = scrollable.bounding_box()
        if scroll_box:
            scroll_cx = scroll_box["x"] + scroll_box["width"] / 2
            scroll_cy = scroll_box["y"] + scroll_box["height"] / 2
            page.mouse.move(scroll_cx, scroll_cy)
            log.info("Scroll target centre: (%.0f, %.0f)", scroll_cx, scroll_cy)
        else:
            scroll_cx = scroll_cy = None
            log.warning("Could not get bounding box — falling back to scrollTop.")

        CARD_SELS = ['div[data-review-id]', 'div.jftiEf', 'div.GHT2ce']

        log.info("Scrolling and collecting reviews...")
        stall_count = 0
        last_seen_count = 0
        old_streak = 0

        for _ in range(120):
            if stall_count >= 5:
                break

            if scroll_cx is not None:
                page.mouse.wheel(0, 2000)
            else:
                scrollable.evaluate("el => el.scrollTop += 2000")
            time.sleep(1.5)

            batch_new = []
            for card_sel in CARD_SELS:
                els = page.locator(card_sel).all()
                if not els:
                    continue
                for el in els:
                    review = _parse_card(el)
                    if review and review["id"] not in seen:
                        seen[review["id"]] = review
                        batch_new.append(review)
                break

            if batch_new:
                if all(r["days_ago"] > LOOKBACK_DAYS for r in batch_new):
                    old_streak += 1
                    if old_streak >= 2:
                        log.info("Scrolled past the lookback window, stopping early.")
                        break
                else:
                    old_streak = 0

            current_count = len(seen)
            if current_count == last_seen_count:
                stall_count += 1
            else:
                stall_count = 0
                last_seen_count = current_count
            log.info("  Unique reviews collected: %d", current_count)

        log.info("Done. Total unique reviews collected: %d", len(seen))
        browser.close()

    reviews = list(seen.values())
    reviews.sort(key=lambda r: r["days_ago"])
    recent = [r for r in reviews if r["days_ago"] <= LOOKBACK_DAYS]
    log.info("Scraped %d total. %d within the last %d days.", len(reviews), len(recent), LOOKBACK_DAYS)
    return recent


def save_results(reviews: list, key_names: list):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append(f"=== Run: {timestamp} ===")
    lines.append(f"Reviews in last {LOOKBACK_DAYS} days: {len(reviews)}")
    lines.append("")

    for review in reviews:
        mentions = match_names_in_text(review["text"], key_names)
        lines.append("--- Review ---")
        lines.append(f"ID:        {review['id']}")
        lines.append(f"Author:    {review['author']}")
        lines.append(f"Rating:    {review['rating']}")
        lines.append(f"Published: {review['published_at']}")
        lines.append(f"Text:      {review['text']}")
        if mentions:
            mention_str = ", ".join(f"{m['name']} ({m['display_name']})" for m in mentions)
            lines.append(f"Mentions:  {mention_str}")
        lines.append("")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info("Saved %d reviews to %s.", len(reviews), OUTPUT_FILE)


def generate_dashboard(reviews: list, key_names: list):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    tally: dict[str, dict] = {}
    for review in reviews:
        for match in match_names_in_text(review["text"], key_names):
            name = match["display_name"]
            if name not in tally:
                tally[name] = {"mentions": 0, "ratings": []}
            tally[name]["mentions"] += 1
            if review["rating"]:
                tally[name]["ratings"].append(review["rating"])

    ranked = sorted(tally.items(), key=lambda x: (-x[1]["mentions"], x[0]))

    lines = []
    lines.append("# Javier's Reviews Dashboard")
    lines.append("")
    lines.append(
        f"> **Period:** Last {LOOKBACK_DAYS} days &nbsp;|&nbsp; "
        f"**Reviews:** {len(reviews)} &nbsp;|&nbsp; "
        f"**Updated:** {timestamp}"
    )
    lines.append("")
    lines.append("## Staff Mentions")
    lines.append("")
    lines.append("| Name | Mentions | Avg Rating |")
    lines.append("|:---|:---:|:---:|")

    if ranked:
        for display_name, data in ranked:
            mentions = data["mentions"]
            ratings = data["ratings"]
            avg = f"{sum(ratings)/len(ratings):.1f} ★" if ratings else "—"
            lines.append(f"| {display_name} | {mentions} | {avg} |")
    else:
        lines.append("| *(no mentions this period)* | — | — |")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by [scraper.py](./scraper.py)*")
    lines.append("")

    with open("README.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info("Dashboard written to README.md (%d staff members mentioned).", len(ranked))


def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler("log.txt", mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    log.info("=== Javier's Review Scraper Starting ===")
    key_names = load_key_names()
    log.info("Loaded %d name variants to match.", len(key_names))

    reviews = scrape_reviews()
    if reviews:
        save_results(reviews, key_names)
        generate_dashboard(reviews, key_names)
    else:
        log.warning("No reviews scraped.")

    log.info("=== Done ===")


if __name__ == "__main__":
    run()
