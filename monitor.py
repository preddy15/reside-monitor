import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# Scraper version stays at 3 because v3 fixed the Airtable virtualized scrolling.
# Keeping this value means upgrading from v4 -> v5 does NOT reset your baseline.
SCRAPER_VERSION = 3
FEATURE_VERSION = 5

FORM_URL = os.getenv(
    "AIRTABLE_FORM_URL",
    "https://airtable.com/appsseXTOVx59HC0W/pagcVengefPFQvMZC/form",
)
FIELD_LABEL = os.getenv("AIRTABLE_FIELD_LABEL", "Project Applying For")
ADD_UNIT_TEXT = os.getenv("AIRTABLE_ADD_UNIT_TEXT", "Add unit")

STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
HISTORY_PATH = Path(os.getenv("HISTORY_PATH", "history.json"))
HEALTH_PATH = Path(os.getenv("HEALTH_PATH", "health.json"))
DEBUG_DIR = Path("debug")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TEST_LISTING = os.getenv("TEST_LISTING", "").strip()

NY_TZ = ZoneInfo("America/New_York")

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

# Your target geography: Williamsburg plus Manhattan at/below the UWS/UES
# boundary you previously defined. These names are used as a fallback if the
# geocoder returns no coordinates.
TARGET_NEIGHBORHOODS = {
    "williamsburg",
    "upper west side",
    "lincoln square",
    "morningside heights",
    "upper east side",
    "yorkville",
    "carnegie hill",
    "hell's kitchen",
    "clinton",
    "hudson yards",
    "chelsea",
    "flatiron district",
    "flatiron",
    "gramercy park",
    "gramercy",
    "kips bay",
    "murray hill",
    "midtown",
    "midtown east",
    "midtown south",
    "greenwich village",
    "west village",
    "east village",
    "lower east side",
    "soho",
    "nolita",
    "noho",
    "tribeca",
    "chinatown",
    "civic center",
    "financial district",
    "battery park city",
    "two bridges",
}

HEALTH_ALERT_THRESHOLD = 3


class IncompleteScrapeError(RuntimeError):
    pass


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def format_et(value: str | None) -> str:
    dt = parse_iso(value)
    if not dt:
        return "Unknown"
    local = dt.astimezone(NY_TZ)
    hour = local.strftime("%I").lstrip("0") or "0"
    return f"{local.strftime('%b %d, %Y')} {hour}:{local.strftime('%M %p')} ET"


def human_duration(start_iso: str | None, end_iso: str | None) -> str | None:
    start = parse_iso(start_iso)
    end = parse_iso(end_iso)
    if not start or not end or end <= start:
        return None

    seconds = int((end - start).total_seconds())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


def send_telegram(message: str, parse_mode: str | None = None) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable."
        )

    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        data["parse_mode"] = parse_mode

    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API returned an error: {payload}")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"initialized": False, "options": [], "scraper_version": None}

    data = json.loads(STATE_PATH.read_text())
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
                "feature_version": FEATURE_VERSION,
                "options": sorted(options, key=str.casefold),
                "updated_at": utc_now_iso(),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def load_history() -> dict:
    if not HISTORY_PATH.exists():
        return {
            "version": 1,
            "created_at": utc_now_iso(),
            "listings": {},
        }

    try:
        data = json.loads(HISTORY_PATH.read_text())
        if not isinstance(data.get("listings"), dict):
            data["listings"] = {}
        return data
    except Exception as exc:
        raise RuntimeError(f"Could not read {HISTORY_PATH}: {exc}") from exc


def save_history(history: dict) -> None:
    history["version"] = 1
    history["updated_at"] = utc_now_iso()
    HISTORY_PATH.write_text(
        json.dumps(history, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )


def load_health() -> dict:
    if not HEALTH_PATH.exists():
        return {
            "consecutive_failures": 0,
            "failure_alert_sent": False,
            "last_success": None,
            "last_failure": None,
            "last_error": None,
        }

    try:
        data = json.loads(HEALTH_PATH.read_text())
    except Exception:
        data = {}

    return {
        "consecutive_failures": int(data.get("consecutive_failures", 0)),
        "failure_alert_sent": bool(data.get("failure_alert_sent", False)),
        "last_success": data.get("last_success"),
        "last_failure": data.get("last_failure"),
        "last_error": data.get("last_error"),
    }


def save_health(health: dict) -> None:
    HEALTH_PATH.write_text(
        json.dumps(health, indent=2, ensure_ascii=False) + "\n"
    )


def record_success(health: dict) -> None:
    had_alert = bool(health.get("failure_alert_sent"))
    previous_failures = int(health.get("consecutive_failures", 0))

    health["consecutive_failures"] = 0
    health["failure_alert_sent"] = False
    health["last_success"] = utc_now_iso()
    health["last_error"] = None
    save_health(health)

    if had_alert:
        try:
            send_telegram(
                "✅ Reside monitor recovered\n"
                f"A successful check completed after {previous_failures} consecutive failures."
            )
        except Exception as exc:
            # Recovery messaging should never turn a healthy scrape into a failure.
            print(f"Could not send recovery Telegram message: {exc}")


def record_failure(health: dict, error: Exception) -> None:
    count = int(health.get("consecutive_failures", 0)) + 1
    health["consecutive_failures"] = count
    health["last_failure"] = utc_now_iso()
    health["last_error"] = f"{type(error).__name__}: {error}"

    should_alert = (
        count >= HEALTH_ALERT_THRESHOLD
        and not health.get("failure_alert_sent", False)
    )

    if should_alert:
        try:
            safe_error = normalize(str(error))
            if len(safe_error) > 450:
                safe_error = safe_error[:447] + "..."
            send_telegram(
                "⚠️ Reside monitor health alert\n\n"
                f"The monitor has failed {count} consecutive checks.\n"
                f"Latest error: {safe_error}\n\n"
                "Your previous listing baseline has been preserved. "
                "Check the latest GitHub Actions run/debug artifact."
            )
            health["failure_alert_sent"] = True
        except Exception as alert_exc:
            print(f"Could not send health alert: {alert_exc}")

    save_health(health)


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
            result["unit"] = normalize(unit_match.group(1))
            continue

        rent_match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", part)
        if rent_match and not result["rent"]:
            try:
                amount = float(rent_match.group(1).replace(",", ""))
                result["rent"] = f"${amount:,.2f}"
            except ValueError:
                result["rent"] = "$" + rent_match.group(1)

    if not result["rent"]:
        rent_match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", row)
        if rent_match:
            try:
                amount = float(rent_match.group(1).replace(",", ""))
                result["rent"] = f"${amount:,.2f}"
            except ValueError:
                result["rent"] = "$" + rent_match.group(1)

    return result


def listing_key(parsed: dict) -> str:
    """
    Identity is address + unit, intentionally excluding rent. This keeps a rent
    change from looking like a completely different apartment.
    """
    address = re.sub(r"[^a-z0-9]+", " ", parsed.get("address", "").casefold()).strip()
    unit = re.sub(r"[^a-z0-9]+", " ", (parsed.get("unit") or "").casefold()).strip()
    return f"{address}||{unit}"


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
    Uses the public OpenStreetMap Nominatim endpoint only for new/reappearing
    listings. Multiple new lookups are spaced by >1 second by the caller.
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
            "reside-airtable-monitor/5.0 "
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

        return {
            "neighborhood": neighborhood,
            "borough": borough,
            "postcode": normalize(addr.get("postcode")) or None,
            "lat": result.get("lat"),
            "lon": result.get("lon"),
            "display_name": result.get("display_name"),
        }

    except Exception as exc:
        # Enrichment should never block a listing alert.
        print(f'Location lookup failed for "{address}": {exc}')
        return None


def find_cached_location(history: dict, address: str) -> dict | None:
    wanted = normalize(address).casefold()
    if not wanted:
        return None

    for entry in history.get("listings", {}).values():
        if normalize(entry.get("address", "")).casefold() != wanted:
            continue
        location = entry.get("location")
        if isinstance(location, dict) and location:
            return location

    return None


def google_maps_url(address: str) -> str:
    return (
        "https://www.google.com/maps/search/?api=1&query="
        + quote_plus(f"{address}, New York, NY")
    )


def priority_for_location(location: dict | None) -> dict:
    """
    Score:
      3/3 HIGH PRIORITY: Williamsburg or Manhattan inside the user's target
                           UWS/UES-and-south geography.
      2/3 REVIEW:         Manhattan/Brooklyn but location detail is insufficient
                           to confidently apply the target-area rule.
      1/3 LOWER PRIORITY: known outside the target geography.

    Manhattan boundary approximation:
      west side -> at/below ~W 110th
      east side -> at/below ~E 96th
    The neighborhood-name fallback is used when coordinates are unavailable.
    """
    if not location:
        return {
            "score": 2,
            "label": "REVIEW",
            "emoji": "🟢",
            "reason": "location could not be verified",
        }

    neighborhood = normalize(location.get("neighborhood")).casefold()
    borough = normalize(location.get("borough"))

    if "williamsburg" in neighborhood:
        return {
            "score": 3,
            "label": "HIGH PRIORITY",
            "emoji": "🔥",
            "reason": "Williamsburg target",
        }

    if borough == "Manhattan":
        lat = None
        lon = None
        try:
            lat = float(location.get("lat"))
            lon = float(location.get("lon"))
        except (TypeError, ValueError):
            pass

        if lat is not None and lon is not None:
            # Approximate east/west split through Central Park.
            # West side uses the W110 target; east side uses the E96 target.
            max_lat = 40.8015 if lon <= -73.965 else 40.7925
            if lat <= max_lat:
                return {
                    "score": 3,
                    "label": "HIGH PRIORITY",
                    "emoji": "🔥",
                    "reason": "Manhattan target area",
                }
            return {
                "score": 1,
                "label": "LOWER PRIORITY",
                "emoji": "⚪",
                "reason": "north of preferred Manhattan boundary",
            }

        if neighborhood in TARGET_NEIGHBORHOODS:
            return {
                "score": 3,
                "label": "HIGH PRIORITY",
                "emoji": "🔥",
                "reason": "preferred Manhattan neighborhood",
            }

        return {
            "score": 2,
            "label": "REVIEW",
            "emoji": "🟢",
            "reason": "Manhattan location needs review",
        }

    if borough == "Brooklyn":
        return {
            "score": 1,
            "label": "LOWER PRIORITY",
            "emoji": "⚪",
            "reason": "Brooklyn outside confirmed Williamsburg match",
        }

    if borough:
        return {
            "score": 1,
            "label": "LOWER PRIORITY",
            "emoji": "⚪",
            "reason": f"outside target area ({borough})",
        }

    return {
        "score": 2,
        "label": "REVIEW",
        "emoji": "🟢",
        "reason": "borough could not be verified",
    }


def build_listing_notification(
    parsed: dict,
    location: dict | None,
    event_type: str,
    first_seen: str,
    last_removed_at: str | None = None,
    is_test: bool = False,
) -> str:
    priority = priority_for_location(location)

    if is_test:
        heading = "🧪 <b>TEST — RESIDE LISTING ALERT</b>"
    elif event_type == "reappeared":
        heading = "🔄 <b>RESIDE LISTING REAPPEARED</b>"
    else:
        heading = "🚨 <b>NEW RESIDE RE-RENTAL</b>"

    address = html.escape(parsed.get("address") or parsed.get("raw") or "Unknown")
    unit = html.escape(parsed.get("unit") or "")
    title = address + (f" — Apt {unit}" if unit else "")

    lines = [
        heading,
        f"{priority['emoji']} <b>{priority['label']} · {priority['score']}/3</b>",
        f"<i>{html.escape(priority['reason'])}</i>",
        "",
        f"🏠 <b>{title}</b>",
    ]

    if parsed.get("rent"):
        lines.append(f"💰 <b>{html.escape(parsed['rent'])}/mo</b>")

    if location:
        neighborhood = normalize(location.get("neighborhood"))
        borough = normalize(location.get("borough"))
        postcode = normalize(location.get("postcode"))

        if neighborhood and borough:
            lines.append(
                f"📍 <b>{html.escape(neighborhood)}, {html.escape(borough)}</b>"
            )
        elif borough:
            lines.append(f"📍 <b>{html.escape(borough)}</b>")
        elif neighborhood:
            lines.append(f"📍 <b>{html.escape(neighborhood)}</b>")

        if postcode:
            lines.append(f"📮 {html.escape(postcode)}")
    else:
        lines.append("📍 Location lookup unavailable")

    lines.append(f"🕐 First detected: <b>{html.escape(format_et(first_seen))}</b>")

    if event_type == "reappeared" and last_removed_at:
        duration = human_duration(last_removed_at, utc_now_iso())
        if duration:
            lines.append(f"↩️ Reappeared after <b>{html.escape(duration)}</b>")
        else:
            lines.append(
                f"↩️ Last removed: <b>{html.escape(format_et(last_removed_at))}</b>"
            )

    maps = html.escape(google_maps_url(parsed.get("address", "")), quote=True)
    form = html.escape(FORM_URL, quote=True)

    lines.extend(
        [
            "",
            f'🗺️ <a href="{maps}">Open in Google Maps</a>',
            f'📝 <a href="{form}">Open Reside application</a>',
            "",
            "<i>Location data: © OpenStreetMap contributors</i>",
        ]
    )

    return "\n".join(lines)


def ensure_history_seeded(history: dict, old_options: set[str]) -> None:
    """
    v5 introduces history.json. Existing baseline rows are seeded without
    generating alerts. Since we did not observe when they originally appeared,
    first_seen_source records that limitation.
    """
    listings = history.setdefault("listings", {})
    if listings or not old_options:
        return

    seeded_at = utc_now_iso()
    for row in sorted(old_options, key=str.casefold):
        parsed = parse_listing_row(row)
        key = listing_key(parsed)
        listings[key] = {
            "address": parsed.get("address"),
            "unit": parsed.get("unit"),
            "rent": parsed.get("rent"),
            "raw": parsed.get("raw"),
            "first_seen": seeded_at,
            "first_seen_source": "v5_history_migration",
            "last_seen": seeded_at,
            "active": True,
            "appearance_count": 1,
            "removal_count": 0,
            "last_removed_at": None,
            "location": None,
        }

    print(f"Seeded history for {len(listings)} existing baseline listing(s).")


def validate_scrape(current_options: set[str], previous_options: set[str]) -> None:
    """
    Protect the baseline from a partial virtualized-list scrape.

    A sudden collapse from a healthy list to a tiny subset is treated as a
    scraper failure, not as dozens of legitimate removals.
    """
    current_count = len(current_options)
    previous_count = len(previous_options)

    if current_count == 0:
        raise IncompleteScrapeError(
            "Captured 0 listings. Refusing to replace the previous baseline."
        )

    if previous_count == 0:
        return

    ratio = current_count / previous_count
    drop = previous_count - current_count

    # Explicitly catches the original "only 8 rendered rows" failure mode.
    if previous_count >= 20 and current_count <= 10:
        raise IncompleteScrapeError(
            f"Suspicious partial scrape: previous baseline had {previous_count} "
            f"listings, but this run captured only {current_count}. "
            "Baseline preserved."
        )

    # General large-drop protection.
    if drop >= 5 and ratio < 0.60:
        raise IncompleteScrapeError(
            f"Suspicious listing-count drop: {previous_count} -> {current_count} "
            f"({ratio:.0%} of prior count). Baseline preserved."
        )


def click_add_unit(page) -> None:
    button = page.get_by_role("button", name=re.compile(r"^\s*Add unit\s*$", re.I))
    if button.count() > 0:
        button.first.scroll_into_view_if_needed()
        button.first.click(timeout=10_000)
        return

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
                        break;
                    }
                    el = el.parentElement;
                    depth += 1;
                }
            }

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
        browser = p.chromium.launch(channel="chrome", headless=True)
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

            for pass_num in range(1, 251):
                visible = get_visible_options(page)
                before_count = len(collected)
                collected.update(visible)

                info = scroll_option_container_once(page)
                page.wait_for_timeout(350)

                if (
                    info.get("found")
                    and not info.get("moved")
                    and not info.get("atBottom")
                ):
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

                if info.get("atBottom"):
                    if len(collected) == before_count:
                        stable_at_bottom += 1
                    else:
                        stable_at_bottom = 0

                    if stable_at_bottom >= 3:
                        break
                else:
                    stable_at_bottom = 0

                if not info.get("found"):
                    wheel_fallback(page)
                    page.wait_for_timeout(400)

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


def process_listings(
    current_options: set[str],
    old_options: set[str],
    history: dict,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Returns (new_events, reappeared_events, removed_entries).
    History is updated in-memory and saved by the caller after alerts succeed.
    """
    ensure_history_seeded(history, old_options)
    listings = history.setdefault("listings", {})
    now = utc_now_iso()

    current_by_key = {}
    for row in sorted(current_options, key=str.casefold):
        parsed = parse_listing_row(row)
        current_by_key[listing_key(parsed)] = parsed

    new_events = []
    reappeared_events = []
    removed_entries = []

    # Mark removals first, based on the previously active history.
    current_keys = set(current_by_key)
    for key, entry in listings.items():
        if entry.get("active", False) and key not in current_keys:
            entry["active"] = False
            entry["last_removed_at"] = now
            entry["removal_count"] = int(entry.get("removal_count", 0)) + 1
            removed_entries.append(entry)

    # Process current rows.
    geocode_requests = 0
    for key, parsed in current_by_key.items():
        entry = listings.get(key)

        if entry is None:
            location = find_cached_location(history, parsed["address"])
            if location is None:
                if geocode_requests:
                    time.sleep(1.1)
                location = geocode_nyc_address(parsed["address"])
                geocode_requests += 1

            entry = {
                "address": parsed.get("address"),
                "unit": parsed.get("unit"),
                "rent": parsed.get("rent"),
                "raw": parsed.get("raw"),
                "first_seen": now,
                "first_seen_source": "observed",
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": location,
            }
            listings[key] = entry
            new_events.append(
                {
                    "parsed": parsed,
                    "entry": entry,
                    "event_type": "new",
                }
            )
            continue

        was_active = bool(entry.get("active", False))
        last_removed_at = entry.get("last_removed_at")

        entry["address"] = parsed.get("address")
        entry["unit"] = parsed.get("unit")
        entry["rent"] = parsed.get("rent")
        entry["raw"] = parsed.get("raw")
        entry["last_seen"] = now
        entry["active"] = True

        if not was_active:
            if not entry.get("location"):
                location = find_cached_location(history, parsed["address"])
                if location is None:
                    if geocode_requests:
                        time.sleep(1.1)
                    location = geocode_nyc_address(parsed["address"])
                    geocode_requests += 1
                entry["location"] = location

            entry["appearance_count"] = int(entry.get("appearance_count", 1)) + 1
            reappeared_events.append(
                {
                    "parsed": parsed,
                    "entry": entry,
                    "event_type": "reappeared",
                    "last_removed_at": last_removed_at,
                }
            )

    return new_events, reappeared_events, removed_entries


def run_normal_monitor() -> None:
    state = load_state()
    history = load_history()
    health = load_health()

    old_options = {
        normalize(x) for x in state.get("options", []) if normalize(x)
    }

    try:
        print(f"Opening Airtable form: {FORM_URL}")
        print(f'Watching "{FIELD_LABEL}" -> "{ADD_UNIT_TEXT}" -> all unit rows')

        current_options = set(scrape_options())
        validate_scrape(current_options, old_options)

        print(
            f"Validated scrape: {len(current_options)} current option(s); "
            f"{len(old_options)} previous option(s)."
        )

        # Upgrade protection from pre-v3 scraper versions.
        if state.get("scraper_version") != SCRAPER_VERSION:
            save_state(list(current_options))
            history = {
                "version": 1,
                "created_at": utc_now_iso(),
                "listings": {},
            }
            ensure_history_seeded(history, current_options)
            save_history(history)
            send_telegram(
                "🔄 Reside monitor recalibrated.\n"
                f"I found {len(current_options)} total current unit option(s) after "
                "scrolling the full Add unit list.\n\n"
                "This is the new baseline. I will alert you only for future additions."
            )
            record_success(health)
            return

        if not state.get("initialized"):
            save_state(list(current_options))
            ensure_history_seeded(history, current_options)
            save_history(history)
            send_telegram(
                "✅ Reside Airtable monitor initialized.\n"
                f"Tracking {len(current_options)} current unit option(s).\n\n"
                "I will alert you only when a new option appears."
            )
            record_success(health)
            return

        new_events, reappeared_events, removed_entries = process_listings(
            current_options,
            old_options,
            history,
        )

        print(f"New listings: {len(new_events)}")
        print(f"Reappeared listings: {len(reappeared_events)}")
        print(f"Removed listings: {len(removed_entries)}")

        # Send alerts before saving state/history. If Telegram fails, the run fails
        # and the prior baseline stays intact so the alert can be retried later.
        for event in new_events + reappeared_events:
            entry = event["entry"]
            message = build_listing_notification(
                parsed=event["parsed"],
                location=entry.get("location"),
                event_type=event["event_type"],
                first_seen=entry.get("first_seen"),
                last_removed_at=event.get("last_removed_at"),
            )
            send_telegram(message, parse_mode="HTML")

        for entry in removed_entries:
            print(
                "Removed: "
                f"{entry.get('address')} "
                f"{entry.get('unit') or ''} "
                f"(removal #{entry.get('removal_count', 1)})"
            )

        save_state(list(current_options))
        save_history(history)
        record_success(health)

    except Exception as exc:
        print(f"Monitor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        record_failure(health, exc)
        raise


def run_test_listing(row: str) -> None:
    history = load_history()
    parsed = parse_listing_row(row)

    location = find_cached_location(history, parsed["address"])
    if location is None:
        location = geocode_nyc_address(parsed["address"])

    now = utc_now_iso()
    message = build_listing_notification(
        parsed=parsed,
        location=location,
        event_type="new",
        first_seen=now,
        is_test=True,
    )
    send_telegram(message, parse_mode="HTML")
    print("Test notification sent. Baseline/history/health were not changed.")


def main() -> int:
    if TEST_LISTING:
        run_test_listing(TEST_LISTING)
        return 0

    run_normal_monitor()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlaywrightTimeoutError as exc:
        print(f"Playwright timed out: {exc}", file=sys.stderr)
        raise
