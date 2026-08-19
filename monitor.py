import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SCRAPER_VERSION = 3
NOTIFICATION_VERSION = 4

FORM_URL = os.getenv(
    "AIRTABLE_FORM_URL",
    "https://airtable.com/appsseXTOVx59HC0W/pagcVengefPFQvMZC/form",
)
FIELD_LABEL = os.getenv("AIRTABLE_FIELD_LABEL", "Project Applying For")
ADD_UNIT_TEXT = os.getenv("AIRTABLE_ADD_UNIT_TEXT", "Add unit")
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
DEBUG_DIR = Path("debug")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TEST_LISTING = os.getenv("TEST_LISTING", "").strip()

IGNORE_TEXT = {
    "",
    "select an option",
    "select option",
    "choose an option",
    "choose the unit you want to apply for",
    "clear",
    "no options",
    "no results",
    "no results found",
    "add unit",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def send_telegram(message: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable."
        )

    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")



NYC_COUNTY_TO_BOROUGH = {
    "new york county": "Manhattan",
    "kings county": "Brooklyn",
    "bronx county": "Bronx",
    "queens county": "Queens",
    "richmond county": "Staten Island",
}

BOROUGH_ALIASES = {
    "manhattan": "Manhattan",
    "new york": "Manhattan",
    "brooklyn": "Brooklyn",
    "kings": "Brooklyn",
    "bronx": "Bronx",
    "the bronx": "Bronx",
    "queens": "Queens",
    "staten island": "Staten Island",
    "richmond": "Staten Island",
}


def parse_listing_row(row: str) -> dict:
    """
    Example:
      2140 Matthews Ave - Apt 4A - $2941.02 -

    Returns address/unit/rent while remaining tolerant of missing pieces.
    """
    row = normalize(row)
    parts = [normalize(p) for p in row.split(" - ") if normalize(p)]

    result = {
        "raw": row,
        "address": parts[0] if parts else row,
        "unit": None,
        "rent": None,
    }

    for part in parts[1:]:
        unit_match = re.match(
            r"^(?:apt|apartment|unit|#)\s*\.?\s*(.+)$",
            part,
            flags=re.I,
        )
        if unit_match and not result["unit"]:
            unit_value = normalize(unit_match.group(1))
            if unit_value:
                result["unit"] = unit_value
            continue

        rent_match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", part)
        if rent_match and not result["rent"]:
            try:
                amount = float(rent_match.group(1).replace(",", ""))
                result["rent"] = f"${amount:,.2f}"
            except ValueError:
                result["rent"] = "$" + rent_match.group(1)

    # Also scan the full row in case a slightly different delimiter is used.
    if not result["rent"]:
        rent_match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", row)
        if rent_match:
            try:
                amount = float(rent_match.group(1).replace(",", ""))
                result["rent"] = f"${amount:,.2f}"
            except ValueError:
                result["rent"] = "$" + rent_match.group(1)

    return result


def normalize_borough(value: str | None) -> str | None:
    if not value:
        return None

    cleaned = normalize(value)
    key = cleaned.casefold()

    if key in BOROUGH_ALIASES:
        return BOROUGH_ALIASES[key]

    if key.endswith(" county"):
        return NYC_COUNTY_TO_BOROUGH.get(key)

    return None


def geocode_nyc_address(address: str) -> dict | None:
    """
    Free geocoding through the public OpenStreetMap Nominatim endpoint.

    We use it only for newly-added listings (or a manual test), not on every
    5-minute scrape. That keeps usage extremely light and within the public
    service policy.
    """
    if not address:
        return None

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": f"{address}, New York City, New York, USA",
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 1,
        "countrycodes": "us",
    }
    headers = {
        "User-Agent": (
            "reside-airtable-monitor/4.0 "
            "(personal NYC housing availability notifier)"
        ),
        "Accept-Language": "en",
    }

    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        results = response.json()

        if not results:
            print(f'Geocoder found no result for "{address}".')
            return None

        result = results[0]
        addr = result.get("address") or {}

        county = normalize(addr.get("county"))
        borough = (
            NYC_COUNTY_TO_BOROUGH.get(county.casefold())
            if county
            else None
        )

        if not borough:
            for key in ("borough", "city_district", "suburb", "city"):
                borough = normalize_borough(addr.get(key))
                if borough:
                    break

        neighborhood = None
        for key in (
            "neighbourhood",
            "quarter",
            "residential",
            "suburb",
            "city_district",
        ):
            candidate = normalize(addr.get(key))
            if not candidate:
                continue
            if normalize_borough(candidate):
                continue
            if candidate.casefold() in {"new york", "new york city"}:
                continue
            neighborhood = candidate
            break

        postcode = normalize(addr.get("postcode")) or None

        return {
            "neighborhood": neighborhood,
            "borough": borough,
            "postcode": postcode,
            "lat": result.get("lat"),
            "lon": result.get("lon"),
            "display_name": result.get("display_name"),
        }

    except Exception as exc:
        # Location enrichment should never prevent a listing alert.
        print(f'Location lookup failed for "{address}": {exc}')
        return None


def google_maps_url(address: str) -> str:
    query = f"{address}, New York, NY"
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)


def build_listing_notification(row: str, is_test: bool = False) -> str:
    parsed = parse_listing_row(row)
    location = geocode_nyc_address(parsed["address"])

    heading = (
        "🧪 TEST — RESIDE LISTING ALERT"
        if is_test
        else "🚨 NEW RESIDE RE-RENTAL"
    )

    title = parsed["address"]
    if parsed["unit"]:
        title += f" — Apt {parsed['unit']}"

    lines = [
        heading,
        "",
        f"🏠 {title}",
    ]

    if parsed["rent"]:
        lines.append(f"💰 {parsed['rent']}/mo")

    if location:
        neighborhood = location.get("neighborhood")
        borough = location.get("borough")
        postcode = location.get("postcode")

        if neighborhood and borough:
            lines.append(f"📍 {neighborhood}, {borough}")
        elif borough:
            lines.append(f"📍 {borough}")
        elif neighborhood:
            lines.append(f"📍 {neighborhood}")

        if postcode:
            lines.append(f"📮 {postcode}")
    else:
        lines.append("📍 Location lookup unavailable")

    lines.extend(
        [
            "",
            "🗺️ Google Maps:",
            google_maps_url(parsed["address"]),
            "",
            "📝 Reside application:",
            FORM_URL,
            "",
            "Location data: © OpenStreetMap contributors",
        ]
    )

    return "\n".join(lines)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "initialized": False,
            "options": [],
            "scraper_version": None,
        }

    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception as exc:
        raise RuntimeError(f"Could not read {STATE_PATH}: {exc}") from exc

    return {
        "initialized": bool(data.get("initialized", False)),
        "options": list(data.get("options", [])),
        "updated_at": data.get("updated_at"),
        "scraper_version": data.get("scraper_version"),
    }


def save_state(options: list[str]) -> None:
    STATE_PATH.write_text(
        json.dumps(
            {
                "initialized": True,
                "scraper_version": SCRAPER_VERSION,
                "options": sorted(options, key=str.casefold),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def save_debug(page, reason: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason)[:50]
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{safe}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def click_add_unit(page) -> None:
    # Exact visible "Add unit" button first.
    button = page.get_by_role("button", name=re.compile(r"^\s*Add unit\s*$", re.I))
    if button.count() > 0:
        button.first.scroll_into_view_if_needed()
        button.first.click(timeout=10_000)
        return

    # Fallback: anchor to Project Applying For and look nearby.
    label = page.get_by_text(re.compile(re.escape(FIELD_LABEL), re.I))
    if label.count() == 0:
        raise RuntimeError(f'Could not find field label "{FIELD_LABEL}".')

    label = label.first
    for levels_up in range(1, 9):
        ancestor = label.locator("xpath=" + "/.." * levels_up)
        candidate = ancestor.get_by_role(
            "button", name=re.compile(re.escape(ADD_UNIT_TEXT), re.I)
        )
        if candidate.count() > 0:
            candidate.first.scroll_into_view_if_needed()
            candidate.first.click(timeout=10_000)
            return

    raise RuntimeError(
        f'Found "{FIELD_LABEL}" but could not find the "{ADD_UNIT_TEXT}" button.'
    )


def get_visible_options(page) -> set[str]:
    """
    The current Reside picker exposes the visible unit rows as role=option.
    We include a few fallback roles in case Airtable changes markup.
    """
    selectors = [
        '[role="option"]',
        '[role="listbox"] [role="option"]',
        '[role="menuitem"]',
        '[role="dialog"] [role="listitem"]',
    ]

    result = set()
    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = loc.count()
        except Exception:
            continue

        for i in range(count):
            item = loc.nth(i)
            try:
                if not item.is_visible():
                    continue
                text = normalize(item.inner_text())
            except Exception:
                continue

            if text and text.casefold() not in IGNORE_TEXT:
                result.add(text)

    return result


def open_picker_if_needed(page) -> None:
    """
    After Add unit, choices may already be visible. If not, open the
    combobox/listbox control Airtable presents.
    """
    if get_visible_options(page):
        return

    scopes = []
    dialogs = page.locator('[role="dialog"]')
    if dialogs.count() > 0:
        scopes.append(dialogs.last)
    scopes.append(page)

    selectors = [
        '[role="combobox"]',
        'button[aria-haspopup="listbox"]',
        'button[aria-haspopup="menu"]',
        'input[role="combobox"]',
    ]

    for scope in scopes:
        for selector in selectors:
            loc = scope.locator(selector)
            try:
                count = loc.count()
            except Exception:
                continue

            for i in range(min(count, 12)):
                candidate = loc.nth(i)
                try:
                    if not candidate.is_visible():
                        continue
                    text = normalize(candidate.inner_text()).casefold()
                    if text == "add unit":
                        continue

                    candidate.click(timeout=5_000)
                    page.wait_for_timeout(600)

                    if get_visible_options(page):
                        return
                except Exception:
                    continue


def scroll_option_container_once(page) -> dict:
    """
    Find the scrollable ancestor of the currently visible option rows.

    This is the important v3 change: Airtable virtualizes the picker, so only
    ~8 rows may exist in the DOM at once. We must scroll the *row container*,
    let Airtable render the next rows, collect them, and repeat.
    """
    return page.evaluate(
        """() => {
            const isVisible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 &&
                       r.height > 0 &&
                       s.visibility !== 'hidden' &&
                       s.display !== 'none';
            };

            const optionNodes = [
                ...document.querySelectorAll(
                    '[role="option"], [role="menuitem"], [role="listitem"]'
                )
            ].filter(isVisible);

            let candidates = [];

            // Best signal: walk up from actual visible option rows.
            for (const option of optionNodes) {
                let el = option.parentElement;
                let depth = 0;

                while (el && el !== document.body && depth < 15) {
                    if (
                        isVisible(el) &&
                        el.scrollHeight > el.clientHeight + 4 &&
                        el.clientHeight > 40
                    ) {
                        candidates.push(el);
                        // The nearest scrollable ancestor is usually the virtual list.
                        break;
                    }
                    el = el.parentElement;
                    depth += 1;
                }
            }

            // Fallback: search visible scrollable elements inside the latest dialog.
            if (!candidates.length) {
                const dialogs = [
                    ...document.querySelectorAll('[role="dialog"]')
                ].filter(isVisible);

                const root = dialogs.length
                    ? dialogs[dialogs.length - 1]
                    : document.body;

                candidates = [...root.querySelectorAll('*')].filter((el) => {
                    if (!isVisible(el)) return false;
                    if (el.scrollHeight <= el.clientHeight + 4) return false;
                    if (el.clientHeight < 40) return false;

                    const s = getComputedStyle(el);
                    return ['auto', 'scroll'].includes(s.overflowY) ||
                           ['auto', 'scroll'].includes(s.overflow);
                });
            }

            // Deduplicate DOM nodes.
            candidates = [...new Set(candidates)];

            if (!candidates.length) {
                return {
                    found: false,
                    moved: false,
                    atBottom: true,
                    top: 0,
                    max: 0,
                    clientHeight: 0
                };
            }

            // Prefer a candidate containing the most visible options.
            // Tie-break toward the smaller viewport, which is usually the list itself
            // rather than the entire modal.
            candidates.sort((a, b) => {
                const aCount = optionNodes.filter(n => a.contains(n)).length;
                const bCount = optionNodes.filter(n => b.contains(n)).length;
                if (aCount !== bCount) return bCount - aCount;
                return a.clientHeight - b.clientHeight;
            });

            const el = candidates[0];

            const before = el.scrollTop;
            const maxScroll = Math.max(0, el.scrollHeight - el.clientHeight);
            const step = Math.max(120, Math.floor(el.clientHeight * 0.72));
            const target = Math.min(maxScroll, before + step);

            el.scrollTop = target;
            el.dispatchEvent(new Event('scroll', { bubbles: true }));

            const after = el.scrollTop;

            return {
                found: true,
                moved: Math.abs(after - before) > 1,
                atBottom: after >= maxScroll - 3,
                top: after,
                max: maxScroll,
                clientHeight: el.clientHeight,
                visibleOptionCount: optionNodes.length
            };
        }"""
    )


def wheel_fallback(page) -> bool:
    """
    If direct scrollTop manipulation doesn't move the list, hover the last
    visible option and use a real mouse wheel event.
    """
    options = page.locator('[role="option"]:visible')
    if options.count() == 0:
        return False

    last = options.last
    try:
        box = last.bounding_box()
        if not box:
            return False
        page.mouse.move(
            box["x"] + min(box["width"] / 2, 120),
            box["y"] + box["height"] / 2,
        )
        page.mouse.wheel(0, 650)
        return True
    except Exception:
        return False


def scrape_options() -> list[str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )

        try:
            page.goto(FORM_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)

            print('Step 1: clicking "Add unit"...')
            click_add_unit(page)
            page.wait_for_timeout(1_000)

            print("Step 2: opening unit picker if needed...")
            open_picker_if_needed(page)
            page.wait_for_timeout(800)

            initial = get_visible_options(page)
            if not initial:
                save_debug(page, "picker_open_but_no_options")
                raise RuntimeError(
                    'Clicked "Add unit", but no visible unit options were found.'
                )

            print(
                f"Initially visible rows: {len(initial)}. "
                "Now scrolling the virtualized Airtable list..."
            )

            collected = set(initial)
            stable_at_bottom = 0
            previous_scroll_top = -1

            # 250 partial-page scrolls is intentionally generous.
            for pass_num in range(1, 251):
                visible = get_visible_options(page)
                before_count = len(collected)
                collected.update(visible)

                info = scroll_option_container_once(page)
                page.wait_for_timeout(350)

                # If JS scrolling couldn't move it, try a genuine wheel event.
                if info.get("found") and not info.get("moved") and not info.get("atBottom"):
                    if wheel_fallback(page):
                        page.wait_for_timeout(450)
                        collected.update(get_visible_options(page))
                        info = scroll_option_container_once(page)
                        page.wait_for_timeout(250)

                collected.update(get_visible_options(page))

                print(
                    f"Scroll pass {pass_num}: "
                    f"{len(collected)} unique option(s), "
                    f"scroll={info.get('top', 0)}/{info.get('max', 0)}, "
                    f"bottom={info.get('atBottom', False)}"
                )

                # Require multiple stable reads at the bottom. This avoids stopping
                # while Airtable is still rendering the final virtualized batch.
                if info.get("atBottom"):
                    if len(collected) == before_count:
                        stable_at_bottom += 1
                    else:
                        stable_at_bottom = 0

                    if stable_at_bottom >= 3:
                        break
                else:
                    stable_at_bottom = 0

                # Safety fallback if Airtable reports no scroll container at all.
                if not info.get("found"):
                    wheel_fallback(page)
                    page.wait_for_timeout(400)

                # Extra escape hatch for a truly stuck list.
                current_top = info.get("top", 0)
                if (
                    current_top == previous_scroll_top
                    and not info.get("atBottom")
                    and len(collected) == before_count
                ):
                    wheel_fallback(page)
                    page.wait_for_timeout(500)
                previous_scroll_top = current_top

            options = sorted(
                {
                    normalize(x)
                    for x in collected
                    if normalize(x).casefold() not in IGNORE_TEXT
                },
                key=str.casefold,
            )

            if not options:
                save_debug(page, "no_unit_options_found")
                raise RuntimeError("No unit options were captured.")

            print(f"Finished: captured {len(options)} total unique option(s).")
            return options

        except Exception:
            save_debug(page, "scrape_failure")
            raise
        finally:
            browser.close()


def main() -> int:
    # Manual GitHub Actions test mode. If a test row was provided, enrich it,
    # send one Telegram message, and exit without touching the saved baseline.
    if TEST_LISTING:
        print(f"Sending enrichment test for: {TEST_LISTING}")
        send_telegram(build_listing_notification(TEST_LISTING, is_test=True))
        print("Test notification sent.")
        return 0

    state = load_state()
    old_options = {
        normalize(x) for x in state.get("options", []) if normalize(x)
    }

    print(f"Opening Airtable form: {FORM_URL}")
    print(f'Watching "{FIELD_LABEL}" -> "{ADD_UNIT_TEXT}" -> all unit rows')

    current_options = set(scrape_options())

    # IMPORTANT:
    # v1/v2 could capture only the first ~8 virtualized rows. The first v3 run
    # therefore recalibrates the baseline instead of treating all other existing
    # rows as newly added listings.
    if state.get("scraper_version") != SCRAPER_VERSION:
        save_state(list(current_options))
        send_telegram(
            "🔄 Reside monitor recalibrated.\n"
            f"I found {len(current_options)} total current unit option(s) after "
            "scrolling the full Add unit list.\n\n"
            "This is the new baseline. I will alert you only for future additions."
        )
        print(
            "Scraper upgraded/recalibrated. "
            "Saved the complete current list as the baseline."
        )
        return 0

    if not state.get("initialized"):
        save_state(list(current_options))
        send_telegram(
            "✅ Reside Airtable monitor initialized.\n"
            f"Tracking {len(current_options)} current unit option(s).\n\n"
            "I will alert you only when a new option appears."
        )
        return 0

    new_options = sorted(current_options - old_options, key=str.casefold)
    removed_options = sorted(old_options - current_options, key=str.casefold)

    if new_options:
        print("New option(s):")
        for index, item in enumerate(new_options):
            print(f"  + {item}")

            # Nominatim's public-service policy requires very light usage.
            # There is normally only one new listing at a time, but if several
            # arrive together we space requests out.
            if index > 0:
                time.sleep(1.1)

            send_telegram(build_listing_notification(item))
    else:
        print("No new options.")

    if removed_options:
        print("Removed option(s):")
        for item in removed_options:
            print(f"  - {item}")

    if current_options != old_options:
        save_state(list(current_options))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlaywrightTimeoutError as exc:
        print(f"Playwright timed out: {exc}", file=sys.stderr)
        raise
