import asyncio
import json
import re
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# All known selectors for colour swatches on Amazon UK
COLOUR_SELECTORS = [
    "div#variation_color_name li",
    "div#inline-twister-row-color_name li",
    "div[id^='inline-twister-row-color_name'] li",
    "li[id^='color_name_']",
    "div.a-carousel-col li",
]

# Filter out price text, noise, and single-digit pagination labels
NOISE_PATTERN = re.compile(
    r"1 option|option from|\bINR\b|\bUSD\b|\bGBP\b|\bEUR\b|^\$|^£|^€",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def clean_image_url(url: str) -> str:
    """
    Strip ALL Amazon dimension/quality modifiers from image URLs.
    Handles patterns like:
      _AC_SX679_   _AC_US40_   _SS40_   _SL1500_   _CR0,0,450,450_
    
    Before: https://.../XXXX._AC_SX679_.jpg
    After:  https://.../XXXX.jpg
    """
    if not url:
        return ""
    # FIX: Use a broader pattern — matches any modifier between ._ and _.extension
    # Added lowercase letters, handles numbers-only like _SL1500_
    cleaned = re.sub(r"\._[A-Za-z0-9,_ ]+_\.", ".", url)
    return cleaned


def is_noise(label: str) -> bool:
    """Return True if the label is junk (price, currency, etc.)"""
    return bool(NOISE_PATTERN.search(label))


def is_pagination_label(label: str) -> bool:
    """
    FIX: Reject single digits or short numbers — these are pagination
    buttons (1, 2, 3, 4, 5) being mistaken for colour swatches.
    """
    return bool(re.fullmatch(r"\d{1,2}", label.strip()))


async def get_colour_name(element) -> str:
    """
    Try multiple ways to get the colour name from a swatch element.
    Amazon uses different attributes depending on page layout.
    """
    # 1. Try title and aria-label attributes (most common)
    for attr in ("title", "aria-label"):
        value = await element.get_attribute(attr)
        if value:
            value = value.replace("Click to select ", "").strip()
            if value and not is_noise(value) and not is_pagination_label(value):
                return value

    # 2. Try alt text of the image inside the swatch
    try:
        img = element.locator("img").first
        alt = await img.get_attribute("alt")
        if alt and not is_noise(alt) and not is_pagination_label(alt):
            return alt.strip()
    except Exception:
        pass

    # 3. Try inner text as last resort
    try:
        text = await element.inner_text()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if lines and not is_noise(lines[0]) and not is_pagination_label(lines[0]):
            return lines[0]
    except Exception:
        pass

    return ""


async def get_best_resolution_urls(page) -> list:
    """
    Extract the highest resolution image URLs from the current view.

    Strategy:
    1. First try data-a-dynamic-image on the main image — this gives
       a JSON map of { url: [width, height] } — pick the largest.
    2. Then collect thumbnails from div#altImages and clean their URLs.
    """
    all_urls = []
    seen = set()

    # ── Strategy 1: data-a-dynamic-image (highest quality) ──
    try:
        main_img = page.locator("img#landingImage")
        data_attr = await main_img.get_attribute("data-a-dynamic-image")
        if data_attr:
            url_map = json.loads(data_attr)
            # Pick the URL with the largest width
            best_url = max(url_map, key=lambda u: url_map[u][0])
            # This URL is already full-res — no cleaning needed
            if best_url not in seen:
                seen.add(best_url)
                all_urls.append(best_url)
    except Exception:
        pass

    # ── Strategy 2: thumbnail strip ──
    thumbnails = await page.locator(
        "li.imageThumbnail img, div#altImages ul li img"
    ).all()

    if not thumbnails:
        thumbnails = await page.locator("div#altImages img").all()

    for img in thumbnails:
        src = await img.get_attribute("src")
        if not src:
            continue

        # FIX: clean the URL to remove size modifiers
        high_res = clean_image_url(src)

        # Skip non-product icons
        if any(skip in high_res for skip in ["play-button", "360_icon", "transparent-pixel"]):
            continue

        if high_res not in seen:
            seen.add(high_res)
            all_urls.append(high_res)

    return all_urls


async def extract_images_from_current_view(page) -> list:
    """
    Get all images for the currently selected colour.
    Handles pagination (pages 1, 2, 3, 4) in the image gallery.
    """
    all_urls = []
    seen = set()

    async def collect_and_merge():
        urls = await get_best_resolution_urls(page)
        for url in urls:
            if url not in seen:
                seen.add(url)
                all_urls.append(url)

    # Collect from first/current page
    await collect_and_merge()

    # Handle pagination — click Next until disabled
    try:
        next_btn = page.locator("div#imageBlock ul.a-pagination li.a-last a")
        while await next_btn.count() > 0:
            disabled = await next_btn.first.get_attribute("aria-disabled")
            if disabled == "true":
                break
            await next_btn.first.click()
            await page.wait_for_timeout(800)
            await collect_and_merge()
    except Exception:
        pass  # No pagination — that's fine

    return all_urls


def print_results(results: dict):
    """
    Print colour names with all their image URLs listed below —
    easy to visually cross-check.
    """
    print("\n" + "=" * 65)
    print("  SCRAPE RESULTS")
    print("=" * 65)

    if not results:
        print("No data found.")
        return

    for colour, images in results.items():
        print(f"\nColour: {colour}")
        print(f"  Total images: {len(images)}")
        for i, url in enumerate(images, start=1):
            # Flag any URLs that still have size modifiers (for debugging)
            flag = " ⚠️  (still has modifier)" if "._" in url and "_." in url else ""
            print(f"  Image {i}: {url}{flag}")
        if not images:
            print("  (no images found for this colour)")

    print("\n" + "=" * 65)
    print(f"  Total colours found : {len(results)}")
    print(f"  Total images found  : {sum(len(v) for v in results.values())}")
    print("=" * 65)


# ─────────────────────────────────────────────
# MAIN SCRAPER
# ─────────────────────────────────────────────

async def scrape_amazon_images(url: str) -> dict:
    results = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        # ── Open the page ──
        print(f"\nOpening: {url}")
        try:
            response = await page.goto(url, wait_until="load", timeout=60000)
            if response.status != 200:
                print(f"Error: HTTP {response.status} — Amazon may be blocking this request.")
                await browser.close()
                return {}
        except Exception as e:
            print(f"Error: Could not load page — {e}")
            await browser.close()
            return {}

        # Give JS extra time to fully render all swatches
        await page.wait_for_timeout(2000)

        # ── CAPTCHA check ──
        if "captcha" in page.url.lower() or await page.locator("input#captchacharacters").count() > 0:
            print("Error: Amazon showed a CAPTCHA.")
            print("Tip: Set headless=False in the code and solve it manually, then run again.")
            await browser.close()
            return {}

        # ── Cookie consent banner ──
        try:
            btn = page.locator("input#sp-cc-accept")
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # ── Wait for image gallery ──
        try:
            await page.wait_for_selector("div#altImages", timeout=10000)
        except Exception:
            print("Warning: Image gallery (div#altImages) not found.")

        # ── Product title ──
        try:
            title = await page.locator("#productTitle").inner_text(timeout=5000)
            print(f"Product: {title.strip()}")
        except Exception:
            print("Product title not found.")

        # ── Find colour swatches ──
        colour_elements = []
        for selector in COLOUR_SELECTORS:
            elements = await page.locator(selector).all()
            if elements:
                colour_elements = elements
                print(f"Found {len(elements)} swatch elements using: '{selector}'")
                break

        # ── Process each colour ──
        if colour_elements:
            seen_names = set()

            for element in colour_elements:
                colour_name = await get_colour_name(element)

                # FIX: Skip empty, duplicate, or pagination labels (1,2,3,4,5)
                if not colour_name:
                    continue
                if is_pagination_label(colour_name):
                    print(f"  Skipping pagination label: '{colour_name}'")
                    continue
                if colour_name in seen_names:
                    continue

                seen_names.add(colour_name)
                print(f"\nProcessing colour: {colour_name} ...")

                # Click the swatch
                try:
                    await element.click(force=True)
                    await page.wait_for_timeout(1500)
                except Exception as e:
                    print(f"  Warning: Could not click swatch — {e}")
                    continue

                # Extract all images for this colour
                images = await extract_images_from_current_view(page)
                results[colour_name] = images
                print(f"  → Collected {len(images)} image(s)")

        else:
            # No colour variants found — extract default view
            print("No colour variants detected. Extracting default images...")
            images = await extract_images_from_current_view(page)
            results["Default"] = images
            print(f"  → Collected {len(images)} image(s)")

        await browser.close()

    return results


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    TEST_URL = "https://www.amazon.co.uk/dp/B0D8THGP6G"

    print("Starting Amazon Image Scraper...")
    data = asyncio.run(scrape_amazon_images(TEST_URL))

    # Print clean colour-by-colour output for cross-checking
    print_results(data)

    # Save full data to JSON
    output_file = "scraped_images.json"
    with open(output_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nFull results saved to: {output_file}")