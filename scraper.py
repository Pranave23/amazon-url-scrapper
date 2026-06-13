"""
amazon_scraper.py
=======================
Scrapes all colour variants and their high-resolution image URLs
from an Amazon product page.

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
import ast
import json
import re
# pyrefly: ignore [missing-import]
from playwright.async_api import async_playwright


#tells the user which browser is being used
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

    This greedily matches from the first ._ to the last _. before the
    extension to completely remove chained modifiers.
    """
    if not url:
        return ""
    return re.sub(
        r"\._.*\.(jpg|jpeg|png|gif|webp)$",
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
    match = re.search(r"/images/I/([^/.]+)", url)
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


def extract_color_images(html_content: str) -> dict:
    """
    Extract the colorImages JSON block from HTML script tags.
    Returns a dict mapping color names to list of image dictionaries,
    or None if not found.
    """
    for match in re.finditer(r"colorImages", html_content):
        start_idx = match.start()
        brace_start = html_content.find("{", start_idx)
        if brace_start == -1 or brace_start - start_idx > 50:
            continue
            
        brace_count = 0
        in_string = False
        escape = False
        for i in range(brace_start, len(html_content)):
            char = html_content[i]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"' and not escape:
                in_string = not in_string
                continue
            if not in_string:
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        json_str = html_content[brace_start:i+1]
                        # Convert JavaScript literals to Python compatible representation
                        json_str_py = json_str.replace("true", "True").replace("false", "False").replace("null", "None")
                        try:
                            data = ast.literal_eval(json_str_py)
                            if isinstance(data, dict) and len(data) > 0:
                                return data
                        except Exception:
                            try:
                                return json.loads(json_str)
                            except Exception:
                                pass
                        break
    return None


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
        img_locator = element.locator("img")
        if await img_locator.count() > 0:
            alt = await img_locator.first.get_attribute("alt", timeout=500)
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
    Compares against old_src and supports multiple common landing image IDs.
    """
    try:
        escaped = old_src.replace("'", "\\'")
        await page.wait_for_function(
            f"""
            (() => {{
                const img = document.querySelector('img#landingImage, img#imgBlkFront, img#main-image');
                return (img ? img.src : '') !== '{escaped}';
            }})()
            """,
            timeout=5000,
        )
    except Exception:
        await page.wait_for_timeout(800)


async def collect_images_for_current_colour(page) -> list:
    """
    Collect all unique high-resolution image URLs for the currently
    selected colour variant (DOM fallback).
    """
    # Inject CSS to hide all popovers and overlays so they cannot intercept clicks
    try:
        await page.add_style_tag(content="""
            .a-popover-container, 
            .a-popover-modal, 
            .a-modal-scroller, 
            #a-popover-lgtbox, 
            #a-page-overlay, 
            [class*="popover"],
            [class*="modal"] {
                display: none !important;
                pointer-events: none !important;
                z-index: -9999 !important;
            }
        """)
        await page.wait_for_timeout(200)
    except Exception:
        pass

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
        landing = page.locator("img#landingImage, img#imgBlkFront, img#main-image, div#img-wrapper img").first
        data_attr = await landing.get_attribute("data-a-dynamic-image")
        if data_attr:
            url_map = json.loads(data_attr)
            if url_map:
                # Pick the URL with the largest width
                best_url = max(url_map, key=lambda u: url_map[u][0])
                add_url(best_url)
    except Exception:
        pass

    # ── Source B: thumbnail strip ────────────────────────────────
    thumbnails = await page.locator(
        """
    li.imageThumbnail img,
    div#altImages img,
    #imageBlockThumbs img,
    span.a-button-thumbnail img
    """
    ).all()

    if not thumbnails:
        thumbnails = await page.locator("div#altImages img").all()

    for img in thumbnails:
        # Scroll the thumbnail into view
        try:
            await img.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass

        # Poll up to 1 second for thumbnail to load a valid source URL
        src = None
        for _ in range(10):
            for attr in ("data-a-hires", "data-src", "src"):
                val = await img.get_attribute(attr)
                if val and not any(kw in val for kw in SKIP_KEYWORDS) and val.startswith("http"):
                    src = val
                    break
            if src:
                break
            await page.wait_for_timeout(100)

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

async def scrape_amazon_images(url: str, proxy_url: str = None) -> dict:
    """
    Scrape all colour variants and their high-resolution image URLs
    from an Amazon product page. Uses script-based parsing primarily
    and a robust DOM-based fallback.
    """
    from urllib.parse import urlparse
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    
    results = {}

    launch_kwargs = {"headless": True}
    if proxy_url:
        try:
            parsed = urlparse(proxy_url)
            # Reconstruct server part without credentials
            scheme = parsed.scheme or "http"
            hostname = parsed.hostname or ""
            port = parsed.port
            
            server_str = f"{scheme}://{hostname}"
            if port:
                server_str += f":{port}"
            
            proxy_dict = {"server": server_str}
            if parsed.username:
                proxy_dict["username"] = parsed.username
            if parsed.password:
                proxy_dict["password"] = parsed.password
                
            launch_kwargs["proxy"] = proxy_dict
            print(f"Using Playwright proxy: {server_str}")
        except Exception as e:
            print(f"Error parsing proxy URL: {e}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
             # ── 1. Load the page ──────────────────────────────────
            print(f"\nOpening: {url}")
            try:
                # Wait for domcontentloaded instead of full load to speed it up significantly
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response is None or response.status != 200:
                    status = response.status if response else "None"
                    raise Exception(f"HTTP {status} — Amazon is blocking this request. A residential proxy is required for cloud hosting.")
            except Exception as e:
                raise Exception(f"Could not load page — {e}. If this is a timeout, Amazon might be blocking the request.")

            # ── 2a. CAPTCHA and soft-block detection ──────────────
            if "captcha" in page.url.lower() or \
               await page.locator("input#captchacharacters").count() > 0:
                raise Exception("Amazon showed a CAPTCHA. Your proxy IP might be flagged.")

            if await page.locator("div#noResultsTitle").count() > 0:
                raise Exception("Amazon returned a soft-block page.")

            # ── 2b. Cookie consent banner ─────────────────────────
            try:
                btn = page.locator("input#sp-cc-accept")
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await page.wait_for_timeout(800)
            except Exception:
                pass

            # ── 3. Wait for product title ────────────────────────
            try:
                await page.wait_for_selector("#productTitle", timeout=10000)
                title = await page.locator("#productTitle").inner_text(timeout=5000)
                print(f"Product: {title.strip()}")
            except Exception:
                print("Warning: Product title not found — page may not have fully rendered.")

            # ── 4. Try Script-Based Parsing (Primary Strategy) ────
            print("Extracting configuration scripts...")
            html = await page.content()
            color_images = extract_color_images(html)

            # Detect colour swatches from DOM to get ASINs
            swatches = []
            matched_selector = None
            for selector in COLOUR_SELECTORS:
                elements = await page.locator(selector).all()
                if elements:
                    matched_selector = selector
                    for el in elements:
                        if await is_swatch_disabled(el):
                            continue
                        asin = await el.get_attribute("data-asin") or await el.get_attribute("data-csa-c-item-id")
                        color_name = await get_colour_name(el)
                        if color_name and is_pagination_label(color_name):
                            continue
                        if asin and color_name:
                            swatches.append({"color": color_name, "asin": asin})
                    break

            if color_images:
                print("Successfully parsed colorImages config block.")
                
                # Check for direct variant mappings in the initial page config
                for color, imgs in color_images.items():
                    if color != 'initial':
                        results[color] = [clean_image_url(x.get('hiRes') or x.get('large') or x.get('thumb') or '') for x in imgs]
                
                # Resolve the 'initial' color
                selected_color = None
                try:
                    selected_el = page.locator("div#variation_color_name span.selection, div#inline-twister-row-color_name span.selection").first
                    if await selected_el.count() > 0:
                        selected_color = (await selected_el.inner_text()).strip()
                except Exception:
                    pass
                    
                if not selected_color and swatches:
                    selected_color = swatches[0]["color"]
                elif not selected_color:
                    selected_color = "Default"
                    
                results[selected_color] = [clean_image_url(x.get('hiRes') or x.get('large') or x.get('thumb') or '') for x in color_images.get('initial', [])]
                
                # For any swatches not yet in results, navigate to their ASIN page
                for swatch in swatches:
                    color = swatch["color"]
                    asin = swatch["asin"]
                    if color not in results:
                        print(f"Loading page for variant '{color}' (ASIN: {asin})...")
                        swatch_url = f"{base_url}/dp/{asin}/"
                        try:
                            # Wait for domcontentloaded with a shorter timeout for swatches
                            await page.goto(swatch_url, wait_until="domcontentloaded", timeout=15000)
                            swatch_html = await page.content()
                            swatch_color_images = extract_color_images(swatch_html)
                            if swatch_color_images and 'initial' in swatch_color_images:
                                results[color] = [clean_image_url(x.get('hiRes') or x.get('large') or x.get('thumb') or '') for x in swatch_color_images['initial']]
                                print(f"  -> Extracted {len(results[color])} images.")
                            else:
                                print(f"  -> Failed to find images for variant '{color}'.")
                        except Exception as e:
                            print(f"  -> Error loading swatch page: {e}")

            # ── 5. Fallback DOM-based Scraping ────────────────────
            if not results:
                print("Script-based parsing failed or returned no data. Falling back to DOM-based crawler...")
                if swatches and matched_selector:
                    seen_names = set()
                    for index in range(len(swatches)):
                        element = page.locator(matched_selector).nth(index)
                        if await is_swatch_disabled(element):
                            continue
                        colour_name = await get_colour_name(element)
                        if not colour_name or is_pagination_label(colour_name) or colour_name in seen_names:
                            continue

                        seen_names.add(colour_name)
                        print(f"\nProcessing swatch: {colour_name} ...")

                        # Capture current main image src before click for change detection
                        old_src = ""
                        try:
                            landing = page.locator("img#landingImage, img#imgBlkFront, img#main-image").first
                            old_src = await landing.get_attribute("src") or ""
                        except Exception:
                            pass

                        try:
                            # Scroll the swatch into view and click
                            await element.scroll_into_view_if_needed()
                            await element.click(force=True)
                        except Exception as e:
                            print(f"  Warning: Could not click swatch — {e}")
                            continue

                        # Wait for main image to change
                        await wait_for_gallery_update(page, old_src)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=3000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1000)

                        images = await collect_images_for_current_colour(page)
                        results[colour_name] = images
                        print(f"  → Collected {len(images)} image(s)")
                else:
                    print("No colour variants detected. Extracting default images from DOM...")
                    images = await collect_images_for_current_colour(page)
                    results["Default"] = images
                    print(f"  → Collected {len(images)} image(s)")

        finally:
            await browser.close()

    return results


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    TEST_URL = "https://www.amazon.co.uk/dp/B0D8THGP6G?th=1"

    print("Starting Amazon Image Scraper...")
    data = asyncio.run(scrape_amazon_images(TEST_URL))

    print_results(data)

    output_file = "scraped_images.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nFull results saved to: {output_file}")