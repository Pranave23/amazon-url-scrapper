"""
amazon_scraper_final.py
=======================
Scrapes all colour variants and their high-resolution image URLs
from an Amazon UK product page.

Fixes applied vs all previous versions:
  - clean_image_url() uses anchored regex that catches every known
    Amazon modifier pattern (_AC_SX679_, _SS40_, _SL1500_, _CR0,0,..._)
  - clean_image_url() is applied to EVERY URL path including
    data-a-dynamic-image (was missing in all previous versions)
  - scroll_thumbnail_strip() triggers lazy loading before reading
    thumbnails — fixes the "7 instead of 8" missing image problem
  - collect_all_images_by_clicking_thumbnails() clicks each thumbnail
    individually and reads the main viewer — fixes duplicates and
    ensures the correct image loads for each slot
  - Deduplication uses the Amazon asset ID (the image hash in the URL)
    not the full URL — prevents the same image appearing twice under
    different modifier strings
  - browser.close() is guaranteed via try/finally — no browser leaks
  - matched_selector stored and reused for stable re-querying of
    swatch elements after DOM updates
  - Soft-block detection added (200 response but bot-detection page)
  - wait_for_gallery_ready() waits for DOM change after swatch click,
    not just for any visible image
"""

import asyncio
import json
import re
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Tried in order — most specific first to avoid false matches
COLOUR_SELECTORS = [
    "div#variation_color_name li",
    "div#inline-twister-row-color_name li",
    "div[id^='inline-twister-row-color_name'] li",
    "li[id^='color_name_']",
]

# Amazon CSS classes that indicate a swatch is unavailable
DISABLED_SWATCH_CLASSES = ("swatch-disabled", "swatch-out-of-stock", "a-disabled")

# Price text, currency codes, and filler phrases that are not colour names
NOISE_PATTERN = re.compile(
    r"1\s*option|option\s*from|\bINR\b|\bUSD\b|\bGBP\b|\bEUR\b|^\$|^£|^€",
    re.IGNORECASE,
)

# Keywords that identify non-photo gallery slots (video, 360, spinners)
SKIP_KEYWORDS = ("play-button", "360_icon", "transparent-pixel", "data:image", "spinner")


# ─────────────────────────────────────────────────────────────────
# URL UTILITIES
# ─────────────────────────────────────────────────────────────────

def clean_image_url(url: str) -> str:
    """
    Remove Amazon's image-dimension and quality modifiers from a URL.

    Amazon appends modifiers between the filename stem and the extension:
        XXXX._AC_SX679_.jpg   →   XXXX.jpg
        XXXX._SS40_.jpg       →   XXXX.jpg
        XXXX._SL1500_.jpg     →   XXXX.jpg
        XXXX._CR0,0,450,450_.jpg → XXXX.jpg

    The regex matches from the first ._ up to (and including) the
    extension dot, replacing everything with just .extension.
    Anchored to $ so it only strips the tail of the filename.
    """
    if not url:
        return ""
    return re.sub(
        r"\._[^.]+\.(jpg|jpeg|png|gif|webp)$",
        r".\1",
        url,
        flags=re.IGNORECASE,
    )


def image_asset_id(url: str) -> str:
    """
    Extract the Amazon image hash from a URL for deduplication.

    Example:
        https://m.media-amazon.com/images/I/61EyBfCXLrL.jpg
        → '61EyBfCXLrL'

    Using the asset ID instead of the full URL means the same image
    is recognised as a duplicate even if it appears under different
    modifier strings.
    """
    match = re.search(r"/images/I/([A-Za-z0-9+]+)", url)
    return match.group(1) if match else url


def is_valid_image_url(url: str) -> bool:
    """Return True only for real HTTP image links — not placeholders or data URIs."""
    if not url:
        return False
    if url.startswith("data:"):
        return False
    if any(kw in url for kw in SKIP_KEYWORDS):
        return False
    return url.startswith("http")


# ─────────────────────────────────────────────────────────────────
# LABEL UTILITIES
# ─────────────────────────────────────────────────────────────────

def is_noise_label(label: str) -> bool:
    """Return True if the label contains pricing or filler text, not a colour name."""
    return bool(NOISE_PATTERN.search(label))


def is_pagination_label(label: str) -> bool:
    """
    Return True if the label is a bare number like '1', '2' … '99'.
    These are pagination buttons that Amazon sometimes renders inside
    the same container as colour swatches.
    """
    return bool(re.fullmatch(r"\d{1,2}", label.strip()))


# ─────────────────────────────────────────────────────────────────
# SWATCH HELPERS
# ─────────────────────────────────────────────────────────────────

async def get_colour_name(element) -> str:
    """
    Extract a clean colour name from a swatch <li> element.

    Tries three strategies in order of reliability:
      1. title / aria-label attribute  (most common on Amazon UK)
      2. alt text of the swatch image  (image-swatch layouts)
      3. visible inner text            (text-swatch layouts, last resort)

    Returns an empty string if no usable name is found.
    """
    # Strategy 1 — attribute
    for attr in ("title", "aria-label"):
        raw = await element.get_attribute(attr)
        if raw:
            value = raw.replace("Click to select ", "").strip()
            if value and not is_noise_label(value) and not is_pagination_label(value):
                return value

    # Strategy 2 — swatch image alt text
    try:
        img = element.locator("img").first
        alt = await img.get_attribute("alt")
        if alt:
            alt = alt.strip()
            if not is_noise_label(alt) and not is_pagination_label(alt):
                return alt
    except Exception:
        pass

    # Strategy 3 — inner text
    try:
        text = await element.inner_text()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and not is_noise_label(lines[0]) and not is_pagination_label(lines[0]):
            return lines[0]
    except Exception:
        pass

    return ""


async def is_swatch_disabled(element) -> bool:
    """Return True if this swatch is greyed out (out of stock or unavailable)."""
    try:
        class_attr = (await element.get_attribute("class")) or ""
        return any(cls in class_attr.lower() for cls in DISABLED_SWATCH_CLASSES)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────
# GALLERY HELPERS
# ─────────────────────────────────────────────────────────────────

async def wait_for_gallery_update(page, old_src: str) -> None:
    """
    Wait until the main product image actually changes after a swatch click.

    Comparing the new src against old_src is more reliable than waiting
    for a selector that was already visible from the previous colour.
    Falls back to a short fixed delay if the JS function times out.
    """
    try:
        escaped = old_src.replace("'", "\\'")
        await page.wait_for_function(
            f"(document.querySelector('img#landingImage')?.src || '') !== '{escaped}'",
            timeout=5000,
        )
    except Exception:
        # Fallback: give JS a moment to swap image sources
        await page.wait_for_timeout(800)


async def scroll_thumbnail_strip(page) -> None:
    """
    Scroll the thumbnail strip to force Amazon to lazy-load all thumbnails.

    Amazon only populates thumbnail src attributes when the element is
    scrolled into the viewport. Without this step, the last 1-2 thumbnails
    remain empty and are missed — producing 7 images instead of 8.
    """
    try:
        strip = page.locator("div#altImages")
        if await strip.count() == 0:
            return
        # Scroll down in small steps to trigger lazy loading of every item
        for _ in range(8):
            await strip.evaluate("el => el.scrollTop += 150")
            await page.wait_for_timeout(100)
        # Reset scroll position so subsequent reads start from the top
        await strip.evaluate("el => el.scrollTop = 0")
        await page.wait_for_timeout(200)
    except Exception:
        pass


async def get_main_image_url(page) -> str:
    """
    Read the highest-resolution URL currently displayed in the main viewer.

    Method 1 — data-a-dynamic-image:
        Amazon stores a JSON dict { url: [width, height] } on img#landingImage.
        We pick the URL with the largest width and clean any modifiers.

    Method 2 — plain src fallback:
        Used when the data attribute is absent (some older page layouts).
    """
    # Method 1: JSON data attribute
    try:
        data_attr = await page.locator("img#landingImage").get_attribute(
            "data-a-dynamic-image", timeout=2000
        )
        if data_attr:
            url_map = json.loads(data_attr)
            if url_map:
                best_url = max(url_map, key=lambda u: url_map[u][0])
                # FIX: clean the URL — data-a-dynamic-image can contain modifiers
                cleaned = clean_image_url(best_url)
                if is_valid_image_url(cleaned):
                    return cleaned
    except Exception:
        pass

    # Method 2: plain src
    try:
        src = await page.locator("img#landingImage").get_attribute("src", timeout=2000)
        cleaned = clean_image_url(src or "")
        if is_valid_image_url(cleaned):
            return cleaned
    except Exception:
        pass

    return ""


async def collect_all_images_by_clicking_thumbnails(page) -> list:
    """
    Collect one high-resolution image URL per gallery thumbnail.

    Why click-through instead of reading src directly:
      - Amazon lazy-loads thumbnail src — empty until scrolled into view
      - data-a-dynamic-image only reflects the currently displayed image
      - Reading thumbnail src directly gives low-res cropped images

    Process:
      1. Scroll the thumbnail strip to trigger lazy loading of all items
      2. Locate every thumbnail list item
      3. Skip non-photo slots (video, 360-view)
      4. Click each thumbnail — this updates the main image viewer
      5. Read the high-res URL from data-a-dynamic-image on the main viewer
      6. Deduplicate by Amazon asset ID (not full URL) to prevent duplicates
         caused by the same image appearing under different modifier strings

    Falls back to reading src attributes directly if click-through yields
    no results (network-blocked or unusual page layout).
    """
    all_urls = []
    seen_ids = set()

    # Step 1: trigger lazy loading
    await scroll_thumbnail_strip(page)

    # Step 2: locate thumbnails
    thumb_locator = page.locator(
        "li.imageThumbnail, div#altImages ul li.item"
    )
    count = await thumb_locator.count()

    if count == 0:
        # Wider fallback selector
        thumb_locator = page.locator("div#altImages ul li")
        count = await thumb_locator.count()

    if count == 0:
        return []

    print(f"    Found {count} thumbnail slot(s) in gallery strip.")

    for i in range(count):
        thumb = thumb_locator.nth(i)

        # Step 3: skip non-photo slots
        try:
            class_attr = (await thumb.get_attribute("class")) or ""
            if "videoThumbnail" in class_attr or "360" in class_attr:
                continue
        except Exception:
            continue

        # Step 4: click the thumbnail
        try:
            await thumb.scroll_into_view_if_needed()
            await thumb.click(force=True)
            # Short wait for the main viewer to update its src
            await page.wait_for_timeout(350)
        except Exception:
            continue

        # Step 5: read high-res URL from main viewer
        url = await get_main_image_url(page)
        if not url:
            continue

        # Step 6: deduplicate by asset ID
        asset_id = image_asset_id(url)
        if not asset_id or asset_id in seen_ids:
            continue

        seen_ids.add(asset_id)
        all_urls.append(url)

    # Fallback if click-through yielded nothing
    if not all_urls:
        print("    Warning: click-through yielded nothing — falling back to src scraping.")
        thumbnails = await page.locator(
            "li.imageThumbnail img, div#altImages ul li img"
        ).all()
        if not thumbnails:
            thumbnails = await page.locator("div#altImages img").all()

        seen_ids_fb = set()
        for img in thumbnails:
            src = await img.get_attribute("src") or await img.get_attribute("data-src")
            if not src:
                continue
            cleaned = clean_image_url(src)
            if not is_valid_image_url(cleaned):
                continue
            aid = image_asset_id(cleaned)
            if aid and aid not in seen_ids_fb:
                seen_ids_fb.add(aid)
                all_urls.append(cleaned)

    return all_urls


# ─────────────────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────────────────

def print_results(results: dict) -> None:
    """
    Print a readable colour-by-colour summary for manual cross-checking.
    Flags any URL where a modifier may not have been stripped correctly.
    """
    print("\n" + "=" * 65)
    print("  SCRAPE RESULTS")
    print("=" * 65)

    if not results:
        print("  No data found.")
        return

    for colour, images in results.items():
        print(f"\n  Colour : {colour}")
        print(f"  Images : {len(images)}")
        for i, url in enumerate(images, start=1):
            # Flag residual modifiers so they are visible during review
            flag = "  ⚠️  modifier may remain" if "._" in url and "_." in url else ""
            print(f"    {i}. {url}{flag}")
        if not images:
            print("    (no images found for this colour)")

    print("\n" + "=" * 65)
    print(f"  Colours found : {len(results)}")
    print(f"  Images total  : {sum(len(v) for v in results.values())}")
    print("=" * 65)


# ─────────────────────────────────────────────────────────────────
# MAIN SCRAPER
# ─────────────────────────────────────────────────────────────────

async def scrape_amazon_images(url: str) -> dict:
    """
    Scrape all colour variants and their high-resolution image URLs
    from an Amazon UK product page.

    Flow:
      1.  Open the page (guard against None response and non-200 status)
      2a. Detect CAPTCHA
      2b. Dismiss cookie consent banner if present
      3a. Wait for product title (confirms JS has rendered)
      3b. Wait for image gallery
      4.  Detect colour swatches using ordered selector list
      5.  For each available colour:
          a. Click the swatch
          b. Wait for main image to update (not just any visible image)
          c. Scroll thumbnail strip to trigger lazy loading
          d. Click each thumbnail and capture high-res URL
      6.  Return { colour_name: [image_url, ...] }

    Returns an empty dict on any unrecoverable error.
    Browser is always closed via try/finally — no resource leaks.
    """
    results = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            # ── 1. Load the page ──────────────────────────────────────
            print(f"\nOpening: {url}")
            try:
                response = await page.goto(url, wait_until="load", timeout=60000)

                # Playwright returns None on certain navigation failures
                if response is None or response.status != 200:
                    status = response.status if response else "None"
                    print(f"Error: HTTP {status} — Amazon may be blocking this request.")
                    return {}

            except Exception as e:
                print(f"Error: Could not load page — {e}")
                return {}

            # ── 2a. CAPTCHA detection ─────────────────────────────────
            captcha_in_url = "captcha" in page.url.lower()
            captcha_on_page = await page.locator("input#captchacharacters").count() > 0
            if captcha_in_url or captcha_on_page:
                print("Error: Amazon showed a CAPTCHA.")
                print("Tip: Set headless=False, solve manually, then re-run.")
                return {}

            # Soft-block: Amazon sometimes returns 200 but shows a bot-detection page
            if await page.locator("div#noResultsTitle").count() > 0:
                print("Error: Amazon returned a soft-block page.")
                return {}

            # ── 2b. Cookie consent ────────────────────────────────────
            try:
                btn = page.locator("input#sp-cc-accept")
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await page.wait_for_timeout(800)
            except Exception:
                pass

            # ── 3a. Wait for product title ────────────────────────────
            try:
                await page.wait_for_selector("#productTitle", timeout=10000)
            except Exception:
                print("Warning: Product title not found — page may not have fully rendered.")

            # ── 3b. Wait for image gallery ────────────────────────────
            try:
                await page.wait_for_selector("div#altImages", timeout=10000)
            except Exception:
                print("Warning: Image gallery (div#altImages) not found.")

            # ── Product title ─────────────────────────────────────────
            try:
                title = await page.locator("#productTitle").inner_text(timeout=5000)
                print(f"Product: {title.strip()}")
            except Exception:
                print("Product title not found.")

            # ── 4. Detect colour swatches ─────────────────────────────
            colour_elements = []
            matched_selector = None

            for selector in COLOUR_SELECTORS:
                elements = await page.locator(selector).all()
                if elements:
                    colour_elements = elements
                    matched_selector = selector
                    print(f"Found {len(elements)} swatch(es) via: '{selector}'")
                    break

            # ── 5. Process each colour ────────────────────────────────
            if colour_elements and matched_selector:
                seen_names = set()
                total = len(colour_elements)

                for index in range(total):
                    # Re-query by index using the matched selector for DOM stability
                    element = page.locator(matched_selector).nth(index)

                    if await is_swatch_disabled(element):
                        continue

                    colour_name = await get_colour_name(element)

                    if not colour_name:
                        continue
                    if is_pagination_label(colour_name):
                        print(f"  Skipping pagination label: '{colour_name}'")
                        continue
                    if colour_name in seen_names:
                        continue

                    seen_names.add(colour_name)
                    print(f"\nProcessing: {colour_name} ...")

                    # Capture current main image src before clicking
                    old_src = (
                        await page.locator("img#landingImage").get_attribute("src")
                    ) or ""

                    # Click the swatch
                    try:
                        await element.click(force=True)
                    except Exception as e:
                        print(f"  Warning: Could not click swatch — {e}")
                        continue

                    # Wait for main image to actually change (not just be visible)
                    await wait_for_gallery_update(page, old_src)

                    # Collect all images for this colour
                    images = await collect_all_images_by_clicking_thumbnails(page)
                    results[colour_name] = images
                    print(f"  → Collected {len(images)} image(s)")

            else:
                # No colour variants — scrape the default view
                print("No colour variants detected. Extracting default images...")
                images = await collect_all_images_by_clicking_thumbnails(page)
                results["Default"] = images
                print(f"  → Collected {len(images)} image(s)")

        finally:
            # Always close the browser — even if an unexpected exception occurs
            await browser.close()

    return results


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    TEST_URL = "https://www.amazon.co.uk/dp/B0D8THGP6G"

    print("Starting Amazon Image Scraper...")
    data = asyncio.run(scrape_amazon_images(TEST_URL))

    # Print human-readable results for manual cross-checking
    print_results(data)

    # Save full data to JSON
    output_file = "scraped_images.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nFull results saved to: {output_file}")