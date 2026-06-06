"""
amazon_scraper_final.py
=======================
Scrapes all colour variants and their high-resolution image URLs
from an Amazon UK product page.

Approach for image collection (intentionally simple):
  1. After clicking a colour swatch, read data-a-dynamic-image on
     img#landingImage — this gives the hero image at max resolution.
  2. Scroll the thumbnail strip to trigger Amazon's lazy loading.
  3. Read every thumbnail img src and clean the URL modifiers.
  4. Deduplicate the combined list by Amazon asset ID.

This is the minimal, proven approach. The click-per-thumbnail method
was tried and caused regressions (wrong elements, same URL repeated).
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

# Amazon CSS classes that mark a swatch as unavailable
DISABLED_SWATCH_CLASSES = ("swatch-disabled", "swatch-out-of-stock", "a-disabled")

# Filler text and price patterns that are not colour names
NOISE_PATTERN = re.compile(
    r"1\s*option|option\s*from|\bINR\b|\bUSD\b|\bGBP\b|\bEUR\b|^\$|^£|^€",
    re.IGNORECASE,
)

# Keywords that identify non-photo slots (video, 360, spinners)
SKIP_KEYWORDS = ("play-button", "360_icon", "transparent-pixel", "data:image", "spinner")


# ─────────────────────────────────────────────────────────────────
# URL UTILITIES
# ─────────────────────────────────────────────────────────────────

def clean_image_url(url: str) -> str:
    """
    Remove Amazon image-dimension and quality modifiers from a URL.

    Amazon appends modifiers between the filename stem and extension:
        XXXX._AC_SX679_.jpg      →  XXXX.jpg
        XXXX._SS40_.jpg          →  XXXX.jpg
        XXXX._SL1500_.jpg        →  XXXX.jpg
        XXXX._CR0,0,450,450_.jpg →  XXXX.jpg

    The pattern matches from the first ._ to the last _. before the
    extension, anchored to end-of-string so it never touches path
    segments in the middle of a URL.
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
    Extract the Amazon image hash for deduplication.

    Example:
        https://m.media-amazon.com/images/I/61EyBfCXLrL.jpg
        → '61EyBfCXLrL'

    Using the asset ID instead of the full URL means the same image
    is recognised as a duplicate even if it appears under different
    modifier strings in different parts of the page.
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
    """Return True if label contains pricing or filler text, not a colour name."""
    return bool(NOISE_PATTERN.search(label))


def is_pagination_label(label: str) -> bool:
    """
    Return True if the label is a bare number like '1' … '99'.
    Amazon sometimes renders pagination buttons inside the same
    container as colour swatches.
    """
    return bool(re.fullmatch(r"\d{1,2}", label.strip()))


# ─────────────────────────────────────────────────────────────────
# SWATCH HELPERS
# ─────────────────────────────────────────────────────────────────

async def get_colour_name(element) -> str:
    """
    Extract a clean colour name from a swatch <li> element.

    Strategy 1 — title / aria-label attribute  (most common on Amazon UK)
    Strategy 2 — alt text of the swatch image  (image-swatch layouts)
    Strategy 3 — visible inner text            (text-swatch layouts, last resort)

    Returns an empty string if no usable name is found.
    """
    for attr in ("title", "aria-label"):
        raw = await element.get_attribute(attr)
        if raw:
            value = raw.replace("Click to select ", "").strip()
            if value and not is_noise_label(value) and not is_pagination_label(value):
                return value

    try:
        img = element.locator("img").first
        alt = await img.get_attribute("alt")
        if alt:
            alt = alt.strip()
            if not is_noise_label(alt) and not is_pagination_label(alt):
                return alt
    except Exception:
        pass

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
    Compares against old_src so the wait does not return immediately when
    the gallery was already visible from the previous colour selection.
    Falls back to a fixed delay if the JS condition times out.
    """
    try:
        escaped = old_src.replace("'", "\\'")
        await page.wait_for_function(
            f"(document.querySelector('img#landingImage')?.src || '') !== '{escaped}'",
            timeout=5000,
        )
    except Exception:
        await page.wait_for_timeout(800)


async def scroll_thumbnail_strip(page) -> None:
    """
    Scroll the thumbnail strip to force Amazon to lazy-load all thumbnails.

    Amazon only populates thumbnail src attributes when the element enters
    the viewport. Without this, the last 1–2 thumbnails remain empty —
    causing 7 images to be collected instead of 8.
    """
    try:
        strip = page.locator("div#altImages")
        if await strip.count() == 0:
            return
        for _ in range(8):
            await strip.evaluate("el => el.scrollTop += 150")
            await page.wait_for_timeout(100)
        # Reset so subsequent reads start from the top
        await strip.evaluate("el => el.scrollTop = 0")
        await page.wait_for_timeout(200)
    except Exception:
        pass


async def collect_images_for_current_colour(page) -> list:
    """
    Collect all unique high-resolution image URLs for the currently
    selected colour variant.

    Two-source strategy:
      Source A — data-a-dynamic-image on img#landingImage:
          Amazon stores { url: [width, height] } JSON here.
          We pick the URL with the largest width. This is the
          hero/main image at true full resolution.
          clean_image_url() is applied to remove any modifiers.

      Source B — thumbnail strip (div#altImages):
          After scrolling to trigger lazy loading, we read every
          thumbnail img src and clean modifier strings.
          This gives the remaining 6–7 angle/detail images.

    Deduplication is done by Amazon asset ID (the image hash in the
    URL path) not the full URL. This prevents the same image appearing
    twice when it exists under different modifier strings across
    Source A and Source B.
    """
    all_urls = []
    seen_ids = set()

    def add_url(url: str) -> None:
        """Clean, validate, deduplicate, and append a URL."""
        cleaned = clean_image_url(url)
        if not is_valid_image_url(cleaned):
            return
        asset_id = image_asset_id(cleaned)
        if not asset_id or asset_id in seen_ids:
            return
        seen_ids.add(asset_id)
        all_urls.append(cleaned)

    # ── Source A: hero image from data-a-dynamic-image ──────────
    try:
        data_attr = await page.locator("img#landingImage").get_attribute(
            "data-a-dynamic-image", timeout=3000
        )
        if data_attr:
            url_map = json.loads(data_attr)
            if url_map:
                # Pick the URL with the largest width
                best_url = max(url_map, key=lambda u: url_map[u][0])
                add_url(best_url)
    except Exception:
        pass

    # ── Scroll to trigger lazy loading before reading thumbnails ─
    await scroll_thumbnail_strip(page)

    # ── Source B: thumbnail strip ────────────────────────────────
    # Primary selector — matches the standard Amazon thumbnail layout
    thumbnails = await page.locator(
        "li.imageThumbnail img, div#altImages ul li img"
    ).all()

    # Fallback if primary finds nothing
    if not thumbnails:
        thumbnails = await page.locator("div#altImages img").all()

    for img in thumbnails:
        # Prefer data-src for lazy-loaded images, fall back to src
        src = await img.get_attribute("src") or await img.get_attribute("data-src")
        if src:
            add_url(src)

    return all_urls


# ─────────────────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────────────────

def print_results(results: dict) -> None:
    """
    Print a readable colour-by-colour summary for manual cross-checking.
    Flags any URL where a modifier may not have been stripped.
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
      1.  Open the page
      2a. Detect CAPTCHA and soft-block pages
      2b. Dismiss cookie consent banner if present
      3a. Wait for product title (confirms JS has rendered)
      3b. Wait for image gallery
      4.  Detect colour swatches
      5.  For each available (non-disabled) colour:
          a. Capture current main image src (for change detection)
          b. Click the swatch
          c. Wait for main image src to change
          d. Collect hero image + all thumbnail images
      6.  Return { colour_name: [image_url, ...] }

    Browser is always closed via try/finally — no resource leaks.
    Returns an empty dict on any unrecoverable error.
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
            # ── 1. Load the page ──────────────────────────────────
            print(f"\nOpening: {url}")
            try:
                response = await page.goto(url, wait_until="load", timeout=60000)
                if response is None or response.status != 200:
                    status = response.status if response else "None"
                    print(f"Error: HTTP {status} — Amazon may be blocking this request.")
                    return {}
            except Exception as e:
                print(f"Error: Could not load page — {e}")
                return {}

            # ── 2a. CAPTCHA and soft-block detection ──────────────
            if "captcha" in page.url.lower() or \
               await page.locator("input#captchacharacters").count() > 0:
                print("Error: Amazon showed a CAPTCHA.")
                print("Tip: Set headless=False, solve manually, then re-run.")
                return {}

            if await page.locator("div#noResultsTitle").count() > 0:
                print("Error: Amazon returned a soft-block page.")
                return {}

            # ── 2b. Cookie consent banner ─────────────────────────
            try:
                btn = page.locator("input#sp-cc-accept")
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await page.wait_for_timeout(800)
            except Exception:
                pass

            # ── 3a. Wait for product title ────────────────────────
            try:
                await page.wait_for_selector("#productTitle", timeout=10000)
            except Exception:
                print("Warning: Product title not found — page may not have fully rendered.")

            # ── 3b. Wait for image gallery ────────────────────────
            try:
                await page.wait_for_selector("div#altImages", timeout=10000)
            except Exception:
                print("Warning: Image gallery (div#altImages) not found.")

            try:
                title = await page.locator("#productTitle").inner_text(timeout=5000)
                print(f"Product: {title.strip()}")
            except Exception:
                print("Product title not found.")

            # ── 4. Detect colour swatches ─────────────────────────
            colour_elements = []
            matched_selector = None

            for selector in COLOUR_SELECTORS:
                elements = await page.locator(selector).all()
                if elements:
                    colour_elements = elements
                    matched_selector = selector
                    print(f"Found {len(elements)} swatch(es) via: '{selector}'")
                    break

            # ── 5. Process each colour ────────────────────────────
            if colour_elements and matched_selector:
                seen_names = set()

                for index in range(len(colour_elements)):
                    # Re-query by index for DOM stability after each click
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

                    # Capture current src before clicking for change detection
                    old_src = (
                        await page.locator("img#landingImage").get_attribute("src")
                    ) or ""

                    try:
                        await element.click(force=True)
                    except Exception as e:
                        print(f"  Warning: Could not click swatch — {e}")
                        continue

                    # Wait until the main image actually changes
                    await wait_for_gallery_update(page, old_src)

                    images = await collect_images_for_current_colour(page)
                    results[colour_name] = images
                    print(f"  → Collected {len(images)} image(s)")

            else:
                print("No colour variants detected. Extracting default images...")
                images = await collect_images_for_current_colour(page)
                results["Default"] = images
                print(f"  → Collected {len(images)} image(s)")

        finally:
            # Guaranteed cleanup — runs even if an exception is raised
            await browser.close()

    return results


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    TEST_URL = "https://www.amazon.co.uk/dp/B0D8THGP6G"

    print("Starting Amazon Image Scraper...")
    data = asyncio.run(scrape_amazon_images(TEST_URL))

    print_results(data)

    output_file = "scraped_images.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nFull results saved to: {output_file}")