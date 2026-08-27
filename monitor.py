import html
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
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

RESIDE_LISTINGS_URL = "https://residenewyork.com/property-listings/"
RESIDE_REQUEST_TIMEOUT = (2.5, 4.0)

FAC_URL = "https://fifthave.org/re-rental-availabilities/"
FAC_REQUEST_TIMEOUT = (2.5, 5.0)
FAC_MAX_ATTEMPTS = 3
FAC_RETRY_DELAYS = (0, 2, 5)
FAC_STATE_PATH = Path(os.getenv("FAC_STATE_PATH", "fac_state.json"))
FAC_HISTORY_PATH = Path(os.getenv("FAC_HISTORY_PATH", "fac_history.json"))

ROCKROSE_URL = "https://rockrose.com/affordable-availabilities/"
ROCKROSE_REQUEST_TIMEOUT = (2.5, 5.0)
ROCKROSE_STATE_PATH = Path(os.getenv("ROCKROSE_STATE_PATH", "rockrose_state.json"))
ROCKROSE_HISTORY_PATH = Path(os.getenv("ROCKROSE_HISTORY_PATH", "rockrose_history.json"))

MNS_URL = "https://www.mns.com/affordable_units"
MNS_REQUEST_TIMEOUT = (2.5, 5.0)
MNS_MAX_ATTEMPTS = 3
MNS_RETRY_DELAYS = (0, 2, 5)
MNS_STATE_PATH = Path(os.getenv("MNS_STATE_PATH", "mns_state.json"))
MNS_HISTORY_PATH = Path(os.getenv("MNS_HISTORY_PATH", "mns_history.json"))

STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
HISTORY_PATH = Path(os.getenv("HISTORY_PATH", "history.json"))
HEALTH_PATH = Path(os.getenv("HEALTH_PATH", "health.json"))
DEBUG_DIR = Path("debug")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TEST_LISTING = os.getenv("TEST_LISTING", "").strip()
RESIDE_AUTOFILL_URL = os.getenv("RESIDE_AUTOFILL_URL", "").strip()

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
        return {
            "initialized": False,
            "options": [],
            "project_record_ids": {},
            "scraper_version": None,
        }

    data = json.loads(STATE_PATH.read_text())

    ids = {}
    for name, rec_id in dict(data.get("project_record_ids", {})).items():
        clean_name = normalize(name)
        if clean_name and re.fullmatch(r"rec[A-Za-z0-9]{10,}", rec_id or ""):
            ids[clean_name] = rec_id

    return {
        "initialized": bool(data.get("initialized", False)),
        "options": list(data.get("options", [])),
        "project_record_ids": ids,
        "updated_at": data.get("updated_at"),
        "scraper_version": data.get("scraper_version"),
    }
def save_state(
    options: list[str],
    project_record_ids: dict[str, str] | None = None,
) -> None:
    safe_ids = {}
    for name, rec_id in (project_record_ids or {}).items():
        clean_name = normalize(name)
        if clean_name and re.fullmatch(r"rec[A-Za-z0-9]{10,}", rec_id or ""):
            safe_ids[clean_name] = rec_id

    STATE_PATH.write_text(
        json.dumps(
            {
                "initialized": True,
                "scraper_version": SCRAPER_VERSION,
                "feature_version": FEATURE_VERSION,
                "options": sorted(options, key=str.casefold),
                "project_record_ids": dict(
                    sorted(safe_ids.items(), key=lambda item: item[0].casefold())
                ),
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



STREET_TOKEN_ALIASES = {
    "street": "st",
    "st.": "st",
    "avenue": "ave",
    "ave.": "ave",
    "road": "rd",
    "rd.": "rd",
    "boulevard": "blvd",
    "blvd.": "blvd",
    "place": "pl",
    "pl.": "pl",
    "drive": "dr",
    "dr.": "dr",
    "lane": "ln",
    "ln.": "ln",
    "court": "ct",
    "ct.": "ct",
    "west": "w",
    "east": "e",
    "north": "n",
    "south": "s",
    "first": "1",
    "second": "2",
    "third": "3",
}


def normalize_street_for_match(value: str) -> str:
    value = normalize(value).casefold()
    value = value.replace("–", " ").replace("—", " ")
    value = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", value)
    value = re.sub(r"[^a-z0-9#]+", " ", value)

    tokens = []
    for token in value.split():
        token = STREET_TOKEN_ALIASES.get(token, token)
        if token in {"apartment", "apt", "unit"}:
            continue
        tokens.append(token)

    return " ".join(tokens)


def candidate_listing_identity(title: str) -> tuple[str, str | None]:
    """
    Normalize a Reside title like:
      128 West 167 Street Apartment – Unit 3D
    into an address-ish string and unit.
    """
    title = normalize(title)
    unit = None

    m = re.search(
        r"(?:apartment|apt|unit)\s*(?:-|–|—|:)?\s*(?:unit|apt)?\s*#?\s*([A-Za-z0-9-]+)\s*$",
        title,
        flags=re.I,
    )
    if m:
        unit = normalize(m.group(1))
        title = title[:m.start()].strip(" -–—")

    # Strip common descriptive suffixes.
    title = re.sub(
        r"\s+(?:apartment|apartments)\s*$",
        "",
        title,
        flags=re.I,
    )

    return normalize_street_for_match(title), (unit.casefold() if unit else None)



def slugify_reside_piece(value: str) -> str:
    value = normalize(value).casefold()
    value = value.replace("–", " ").replace("—", " ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def ordinalize_street_number(token: str) -> str:
    """
    Reside slugs often write numbered streets as 184th / 32nd / 167th even
    when Airtable says "184th Street" or "184 Street".
    """
    m = re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", token.casefold())
    if not m:
        return token

    n = int(m.group(1))
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def direct_reside_url_candidates(parsed: dict) -> list[str]:
    """
    Build a small set of likely WordPress property slugs directly from the
    Airtable address + unit.

    Example Airtable:
      364 East 184th Street - Apt 3A

    Known Reside page:
      /property/364-east-184th-apartments-unit-3a/

    We deliberately generate several variants because Reside's historical
    slugs are inconsistent about Street/St and Apartment/Apartments.
    """
    address = normalize(parsed.get("address", ""))
    unit = normalize(parsed.get("unit", ""))
    if not address or not unit:
        return []

    tokens = address.split()
    if not tokens:
        return []

    # Canonicalize street suffixes and numbered street tokens.
    suffixes = {"street", "st", "st.", "avenue", "ave", "ave.", "road", "rd", "rd."}
    core_tokens = []
    street_suffix = None

    for token in tokens:
        clean = token.strip(",.")
        if clean.casefold() in suffixes:
            street_suffix = clean.casefold().rstrip(".")
            continue

        # Ordinalize numbered street names after the house number/direction.
        if re.fullmatch(r"\d+(?:st|nd|rd|th)?", clean.casefold()):
            # Do not ordinalize the first token (house number).
            if core_tokens:
                clean = ordinalize_street_number(clean)

        core_tokens.append(clean)

    if not core_tokens:
        return []

    base_no_suffix = "-".join(slugify_reside_piece(t) for t in core_tokens if t)
    unit_slug = slugify_reside_piece(unit)

    # Generate only a handful of highly plausible variants to keep latency low.
    stems = [
        # Unit-specific pages.
        f"{base_no_suffix}-apartments-unit-{unit_slug}",
        f"{base_no_suffix}-apartment-unit-{unit_slug}",

        # Building-level pages. Some Reside properties contain many apartment
        # tiers on one page and omit the apartment number from the URL entirely.
        f"{base_no_suffix}-apartments",
        f"{base_no_suffix}-apartment",
    ]

    if street_suffix:
        suffix_variants = {
            "street": ["st", "street"],
            "st": ["st", "street"],
            "avenue": ["ave", "avenue"],
            "ave": ["ave", "avenue"],
            "road": ["rd", "road"],
            "rd": ["rd", "road"],
            "boulevard": ["blvd", "boulevard"],
            "blvd": ["blvd", "boulevard"],
            "place": ["pl", "place"],
            "pl": ["pl", "place"],
            "drive": ["dr", "drive"],
            "dr": ["dr", "drive"],
            "lane": ["ln", "lane"],
            "ln": ["ln", "lane"],
            "court": ["ct", "court"],
            "ct": ["ct", "court"],
        }.get(street_suffix, [street_suffix])

        for normalized_suffix in suffix_variants:
            stems.extend(
                [
                    f"{base_no_suffix}-{normalized_suffix}-apartments-unit-{unit_slug}",
                    f"{base_no_suffix}-{normalized_suffix}-apartment-unit-{unit_slug}",
                    f"{base_no_suffix}-{normalized_suffix}-apartments",
                    f"{base_no_suffix}-{normalized_suffix}-apartment",
                ]
            )

    # Also support pages phrased "-apt-3a".
    stems.extend(
        [
            f"{base_no_suffix}-apt-{unit_slug}",
            f"{base_no_suffix}-unit-{unit_slug}",
        ]
    )

    seen = set()
    urls = []
    for stem in stems:
        stem = re.sub(r"-+", "-", stem).strip("-")
        url = urljoin(RESIDE_LISTINGS_URL, f"/property/{stem}/")
        if url not in seen:
            seen.add(url)
            urls.append(url)

    return urls



def rent_to_float(value: str | None) -> float | None:
    if not value:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", value)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def page_contains_matching_rent(page_text: str, rent_value: str | None) -> bool:
    target = rent_to_float(rent_value)
    if target is None:
        return False

    amounts = []
    for raw in re.findall(r"\$\s*([\d,]+(?:\.\d{1,2})?)", page_text):
        try:
            amounts.append(float(raw.replace(",", "")))
        except ValueError:
            pass

    # Reside commonly displays whole-dollar rent while Airtable may include
    # cents, so allow a sub-$1 difference.
    return any(abs(amount - target) < 1.0 for amount in amounts)


def validate_direct_reside_page(parsed: dict, url: str) -> dict | None:
    """
    GET a candidate direct URL and verify the resulting property page contains
    the expected unit and enough of the street address to trust the match.
    """
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "reside-airtable-monitor/5.4 "
                    "(personal NYC housing availability notifier)"
                )
            },
            timeout=RESIDE_REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        if response.status_code != 200:
            return None
    except Exception as exc:
        print(f"Direct Reside candidate failed: {url} -> {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    title = normalize(
        (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
    )
    page_text = normalize(soup.get_text(" ", strip=True))

    if not title or "property" not in response.url:
        return None

    expected_unit = normalize(parsed.get("unit", "")).casefold()

    # A direct Reside page may represent either one specific unit or an entire
    # building. For unit-specific pages, require the exact apartment. For
    # building-level pages, allow the unit to be absent only when the page
    # contains the Airtable rent tier; address validation below still applies.
    unit_pattern = (
        rf"\b(?:unit|apt|apartment)\s*#?\s*{re.escape(expected_unit)}\b"
        if expected_unit
        else None
    )
    unit_present = bool(
        unit_pattern and re.search(unit_pattern, page_text, flags=re.I)
    )
    rent_present = page_contains_matching_rent(page_text, parsed.get("rent"))

    if expected_unit and not unit_present and not rent_present:
        return None

    target_address = normalize_street_for_match(parsed.get("address", ""))
    cand_address, cand_unit = candidate_listing_identity(title)

    # Require the same house number.
    target_num = re.match(r"^(\d+)\b", target_address)
    cand_num = re.match(r"^(\d+)\b", cand_address)
    if target_num and cand_num and target_num.group(1) != cand_num.group(1):
        return None

    # Require either strong normalized title similarity or strong token overlap.
    similarity = SequenceMatcher(None, target_address, cand_address).ratio()
    target_tokens = set(target_address.split())
    cand_tokens = set(cand_address.split())
    overlap = len(target_tokens & cand_tokens) / max(1, len(target_tokens))

    if similarity < 0.55 and overlap < 0.60:
        return None

    print(f'Direct Reside match: "{title}" -> {response.url}')
    return {
        "title": title,
        "url": response.url,
        "score": max(similarity, overlap),
        "match_type": "unit" if unit_present else "building_rent",
    }


def try_direct_reside_match(parsed: dict) -> dict | None:
    candidates = direct_reside_url_candidates(parsed)

    if not candidates:
        return None

    # v5.7 speed optimization:
    # Reside slug variants are independent HTTP requests, so try a bounded
    # number concurrently instead of paying each failed timeout sequentially.
    #
    # Four workers is enough to cover the most common variants in parallel
    # without hammering Reside.
    max_workers = min(4, len(candidates))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {
            executor.submit(validate_direct_reside_page, parsed, url): url
            for url in candidates
        }

        try:
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    match = future.result()
                except Exception as exc:
                    print(f"Parallel Reside candidate failed: {url} -> {exc}")
                    continue

                if match:
                    # Cancel candidates that have not started yet. Running HTTP
                    # requests cannot always be interrupted, but we stop waiting
                    # for additional validation results once a valid match exists.
                    for other in future_to_url:
                        if other is not future:
                            other.cancel()
                    return match
        finally:
            # Executor cleanup happens automatically at context exit.
            pass

    return None


def find_reside_property(parsed: dict) -> dict | None:
    """
    Fetch Reside's listings index and find the best address+unit match.

    The request happens only for a genuinely new/reappearing listing.
    A 3s connect / 5s read timeout keeps this enrichment from delaying the
    Telegram alert for long.
    """
    target_address = normalize_street_for_match(parsed.get("address", ""))
    target_unit = normalize(parsed.get("unit", "")).casefold() or None

    if not target_address:
        return None

    try:
        response = requests.get(
            RESIDE_LISTINGS_URL,
            headers={
                "User-Agent": (
                    "reside-airtable-monitor/5.2 "
                    "(personal NYC housing availability notifier)"
                )
            },
            timeout=RESIDE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"Reside listing-index lookup failed: {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    candidates = []

    # Each property card has a /property/... link. Depending on theme markup,
    # the link itself may say "View Listing", so search nearby headings/text.
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        if "/property/" not in href:
            continue

        url = urljoin(RESIDE_LISTINGS_URL, href)

        title = ""
        # First, use anchor text if it looks like a property title.
        anchor_text = normalize(anchor.get_text(" ", strip=True))
        if anchor_text and anchor_text.casefold() not in {
            "view listing",
            "image",
            "learn more",
        }:
            title = anchor_text

        # Otherwise walk upward and find the nearest heading.
        if not title:
            parent = anchor
            for _ in range(7):
                parent = getattr(parent, "parent", None)
                if parent is None:
                    break
                heading = parent.find(["h1", "h2", "h3", "h4", "h5", "h6"])
                if heading:
                    candidate_text = normalize(heading.get_text(" ", strip=True))
                    if candidate_text:
                        title = candidate_text
                        break

        if not title:
            # Last fallback: derive a matchable phrase from the URL slug.
            slug = href.rstrip("/").split("/")[-1].replace("-", " ")
            title = normalize(slug)

        cand_address, cand_unit = candidate_listing_identity(title)

        # Strong requirement: same house number.
        target_num = re.match(r"^(\d+)\b", target_address)
        cand_num = re.match(r"^(\d+)\b", cand_address)
        if target_num and cand_num and target_num.group(1) != cand_num.group(1):
            continue

        # If Airtable supplies a unit, require the candidate's unit to match.
        if target_unit:
            if not cand_unit or cand_unit != target_unit:
                continue

        similarity = SequenceMatcher(
            None,
            target_address,
            cand_address,
        ).ratio()

        # Address token overlap helps with St/Street and directional variations.
        target_tokens = set(target_address.split())
        cand_tokens = set(cand_address.split())
        overlap = (
            len(target_tokens & cand_tokens) / max(1, len(target_tokens))
        )

        score = (similarity * 0.65) + (overlap * 0.35)
        candidates.append(
            {
                "title": title,
                "url": url,
                "score": score,
            }
        )

    if not candidates:
        print(
            f'No Reside property-page match found for '
            f'"{parsed.get("address")}" unit "{parsed.get("unit")}".'
        )
        return None

    best = max(candidates, key=lambda x: x["score"])
    if best["score"] < 0.62:
        print(
            "Best Reside match was too weak: "
            f'{best["title"]} ({best["score"]:.2f})'
        )
        return None

    print(
        f'Reside match: "{best["title"]}" '
        f'({best["score"]:.2f}) -> {best["url"]}'
    )
    return best



def extract_rent_tier_details(soup, parsed: dict) -> dict:
    """
    For building-level Reside pages, locate the Available Units tier whose rent
    matches the Airtable row. This lets us derive bedroom type and 1-person
    eligibility even when Apt 406 is not named on the page.
    """
    target_rent = rent_to_float(parsed.get("rent"))
    if target_rent is None:
        return {}

    # Build a compact ordered text stream from headings/paragraph-like elements.
    nodes = soup.find_all(["h2", "h3", "h4", "h5", "p", "div", "li"])
    chunks = []
    for node in nodes:
        txt = normalize(node.get_text(" ", strip=True))
        if txt and txt not in chunks[-3:]:
            chunks.append(txt)

    # The raw page text is more reliable than DOM structure across theme changes.
    page_text = normalize(soup.get_text(" ", strip=True))

    # Find occurrences of the target rent, allowing whole-dollar display.
    whole = int(round(target_rent))
    rent_patterns = [
        rf"\\$\\s*{whole:,}(?:\\.00)?\\b",
        rf"\\$\\s*{whole}(?:\\.00)?\\b",
    ]

    match = None
    for pat in rent_patterns:
        match = re.search(pat, page_text)
        if match:
            break
    if not match:
        return {}

    # Inspect a window around the matching rent. Reside pages list:
    # [Bedroom Type] Available Units ... Rent $X ... Required Income ...
    start = max(0, match.start() - 350)
    end = min(len(page_text), match.end() + 650)
    window = page_text[start:end]

    unit_size = None
    size_matches = list(
        re.finditer(
            r"\\b(Studio|[1-5]\\s*Bedroom|[1-5]\\s*BR)\\b",
            window,
            flags=re.I,
        )
    )
    # Prefer the last size before the rent occurrence within this window.
    rent_pos = match.start() - start
    before = [m for m in size_matches if m.start() <= rent_pos]
    if before:
        unit_size = normalize(before[-1].group(1))
    elif size_matches:
        unit_size = normalize(size_matches[0].group(1))

    one_person_income = None
    m = re.search(
        r"\\b1\\s*(?:person|people)\\s+"
        r"(\\$[\\d,]+(?:\\.\\d{1,2})?\\s*[-–—]\\s*\\$[\\d,]+(?:\\.\\d{1,2})?)",
        window,
        flags=re.I,
    )
    if m:
        one_person_income = normalize(m.group(1))

    # Determine minimum household size in this matched rent block.
    household_sizes = [
        int(x)
        for x in re.findall(
            r"\\b([1-9])\\s*(?:person|people)\\b",
            window,
            flags=re.I,
        )
    ]

    return {
        "unit_size": unit_size,
        "one_person_income": one_person_income,
        "one_person_eligible": (
            True
            if one_person_income
            else (min(household_sizes) <= 1 if household_sizes else None)
        ),
    }


def extract_reside_details(property_match: dict, parsed: dict | None = None) -> dict | None:
    """
    Open one matched property page and parse useful fields.

    Current Reside property pages visibly expose:
      - AMI in the "Rent & Income Eligibility Guidelines" section
      - unit size / bedroom type
      - household size
      - required income
      - property address
    The parser uses both table markup and tolerant text regexes.
    """
    if not property_match or not property_match.get("url"):
        return None

    url = property_match["url"]

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "reside-airtable-monitor/5.2 "
                    "(personal NYC housing availability notifier)"
                )
            },
            timeout=RESIDE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"Reside property detail lookup failed: {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = normalize(soup.get_text(" ", strip=True))

    details = {
        "url": url,
        "title": property_match.get("title"),
        "ami": None,
        "unit_size": None,
        "one_person_income": None,
        "one_person_eligible": None,
        "property_address": None,
    }

    # Table-aware extraction first.
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_cells = rows[0].find_all(["th", "td"])
        headers = [
            normalize(c.get_text(" ", strip=True)).casefold()
            for c in header_cells
        ]

        if "ami" not in headers:
            continue

        for row in rows[1:]:
            cells = [
                normalize(c.get_text(" ", strip=True))
                for c in row.find_all(["th", "td"])
            ]
            if not cells:
                continue

            mapping = {}
            for i, header in enumerate(headers):
                if i < len(cells):
                    mapping[header] = cells[i]

            for key, value in mapping.items():
                if key == "ami" and not details["ami"]:
                    m = re.search(r"\b(\d{2,3}\s*%)\b", value)
                    if m:
                        details["ami"] = m.group(1).replace(" ", "")
                if "unit size" in key and not details["unit_size"]:
                    details["unit_size"] = value or None

            # Reside often formats the first household row with all columns,
            # then subsequent household sizes as shorter continuation rows.
            row_text = normalize(row.get_text(" ", strip=True))
            if re.search(r"\b1\s*(?:person|people)\b", row_text, flags=re.I):
                income_match = re.search(
                    r"(\$[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*\$[\d,]+(?:\.\d{1,2})?)",
                    row_text,
                )
                if income_match:
                    details["one_person_income"] = normalize(income_match.group(1))
                    details["one_person_eligible"] = True

    # Tolerant page-text fallbacks for pages whose eligibility content is not
    # rendered as a literal <table>.
    if not details["ami"]:
        ami_patterns = [
            r"\bAMI\b.{0,120}?\b(\d{2,3}\s*%)\b",
            r"\b(\d{2,3}\s*%)\b.{0,120}?\bAMI\b",
            r"\b(40|50|60|70|80|90|100|110|120|130|140|165)\s*%\b",
        ]
        for pattern in ami_patterns:
            m = re.search(pattern, page_text, flags=re.I)
            if m:
                details["ami"] = m.group(1).replace(" ", "") + (
                    "" if "%" in m.group(1) else "%"
                )
                break

    if not details["unit_size"]:
        m = re.search(
            r"\b(Studio|\d+\s*(?:Bedroom|BR))\b",
            page_text,
            flags=re.I,
        )
        if m:
            details["unit_size"] = normalize(m.group(1))

    if not details["one_person_income"]:
        # Prefer the explicit 1-person line in the eligibility section.
        one_person_patterns = [
            r"\b1\s*(?:person|people)\b\s*[:|]?\s*(\$[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*\$[\d,]+(?:\.\d{1,2})?)",
            r"\b1\s*(?:person|people)\b.{0,80}?(\$[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*\$[\d,]+(?:\.\d{1,2})?)",
        ]
        for pattern in one_person_patterns:
            m = re.search(pattern, page_text, flags=re.I)
            if m:
                details["one_person_income"] = normalize(m.group(1))
                details["one_person_eligible"] = True
                break

    if details["one_person_eligible"] is None:
        # If the page explicitly says the household range begins at 2+,
        # then a one-person household is not eligible for this unit.
        household_range = re.search(
            r"Household Size\s*:?\s*(\d+)\s*[-–—]\s*(\d+)",
            page_text,
            flags=re.I,
        )
        if household_range:
            minimum_household = int(household_range.group(1))
            details["one_person_eligible"] = minimum_household <= 1
        else:
            # Also catch eligibility rows whose smallest listed household is 2+.
            listed_sizes = [
                int(x)
                for x in re.findall(
                    r"\b(\d+)\s*(?:person|people)\b",
                    page_text,
                    flags=re.I,
                )
            ]
            if listed_sizes:
                details["one_person_eligible"] = min(listed_sizes) <= 1

    # Property Address heading + nearby text.
    address_heading = soup.find(
        lambda tag: (
            tag.name in {"h2", "h3", "h4", "h5", "h6"}
            and "property address" in normalize(tag.get_text(" ", strip=True)).casefold()
        )
    )
    if address_heading:
        node = address_heading.find_next()
        for _ in range(8):
            if not node:
                break
            candidate = normalize(node.get_text(" ", strip=True))
            if (
                candidate
                and candidate.casefold() != "property address"
                and re.search(r"\bNY\s+\d{5}\b", candidate, flags=re.I)
            ):
                details["property_address"] = candidate
                break
            node = node.find_next()

    # On building-level pages, use Airtable rent to select the correct unit tier.
    if parsed:
        tier = extract_rent_tier_details(soup, parsed)
        if tier.get("unit_size"):
            details["unit_size"] = tier["unit_size"]
        if tier.get("one_person_income"):
            details["one_person_income"] = tier["one_person_income"]
            details["one_person_eligible"] = True
        elif tier.get("one_person_eligible") is False:
            details["one_person_eligible"] = False

    return details


def enrich_from_reside(parsed: dict) -> dict | None:
    # Reside's /property-listings/ page does not always surface every live
    # direct property page. Try predictable direct slugs first, then fall back
    # to the listings index matcher.
    match = try_direct_reside_match(parsed)

    if not match:
        match = find_reside_property(parsed)

    if not match:
        return None

    details = extract_reside_details(match, parsed=parsed) or {
        "url": match.get("url"),
        "title": match.get("title"),
    }
    return details


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


TARGET_HIGH_PRIORITY_ZIPS = {
    "10001","10002","10003","10004","10005","10006","10007","10009",
    "10010","10011","10012","10013","10014","10016","10017","10018",
    "10019","10020","10021","10022","10023","10024","10028","10036",
    "10038","10044","10065","10069","10075","10128","10280","10282",
    "11211","11249",
}


def priority_for_location(location: dict | None) -> dict:
    """
    High Priority is determined only by the geocoded ZIP allowlist.
    Existing enrichment/geocoding behavior is unchanged.
    """
    if not location:
        return {
            "score": 2,
            "label": "REVIEW",
            "emoji": "🟢",
            "reason": "Location could not be verified.",
        }

    postcode = normalize(location.get("postcode"))
    match = re.search(r"\b(\d{5})\b", postcode or "")
    zip_code = match.group(1) if match else ""

    if zip_code in TARGET_HIGH_PRIORITY_ZIPS:
        return {
            "score": 3,
            "label": "HIGH PRIORITY",
            "emoji": "🔥",
            "reason": f"Target ZIP {zip_code}.",
        }

    if not zip_code:
        return {
            "score": 2,
            "label": "REVIEW",
            "emoji": "🟢",
            "reason": "ZIP code could not be verified.",
        }

    return {
        "score": 1,
        "label": "LOWER PRIORITY",
        "emoji": "⚪",
        "reason": f"ZIP {zip_code} is outside the target area.",
    }


def build_reside_application_url(
    base_url: str,
    project_record_id: str | None,
) -> str:
    """
    Add/replace Airtable's linked-record prefill for "Project Applying For".

    Airtable linked-record fields require the record ID (rec...), not the
    visible project name. Every other private autofill parameter is preserved.
    The field remains visible.
    """
    base_url = normalize(base_url)
    project_record_id = normalize(project_record_id)

    if not base_url:
        return ""

    parts = urlsplit(base_url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)

    target_key = "prefill_Project Applying For"
    filtered = [
        (key, value)
        for key, value in query_pairs
        if key.casefold() != target_key.casefold()
    ]

    if re.fullmatch(r"rec[A-Za-z0-9]{10,}", project_record_id):
        filtered.append((target_key, project_record_id))

    new_query = urlencode(filtered, doseq=True)

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            new_query,
            parts.fragment,
        )
    )
def build_listing_notification(
    parsed: dict,
    location: dict | None,
    event_type: str,
    first_seen: str,
    last_removed_at: str | None = None,
    is_test: bool = False,
    reside: dict | None = None,
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

    if reside:
        if reside.get("unit_size"):
            lines.append(f"🛏️ <b>{html.escape(reside['unit_size'])}</b>")
        if reside.get("ami"):
            lines.append(f"📊 AMI: <b>{html.escape(reside['ami'])}</b>")
        if reside.get("one_person_income"):
            income = normalize(reside["one_person_income"])
            lines.append(
                f"👤 1-person income: <b>{html.escape(income)}</b>"
            )
        elif reside.get("one_person_eligible") is False:
            lines.append("👤 1-person household: <b>Not eligible</b>")
        elif reside.get("one_person_eligible") is True:
            lines.append("👤 1-person household: <b>Eligible</b>")

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

    # Sensitive autofill URL is supplied only at runtime via GitHub Actions
    # secret RESIDE_AUTOFILL_URL. Never persist or log it.
    #
    # "Project Applying For" is an Airtable linked-record field, so it must
    # receive the Airtable rec... ID captured from the picker—not the visible
    # project name.
    base_application_url = RESIDE_AUTOFILL_URL or FORM_URL
    project_record_id = (
        parsed.get("airtable_record_id")
        or ((reside or {}).get("airtable_record_id"))
    )
    application_url = build_reside_application_url(
        base_application_url,
        project_record_id,
    )
    form = html.escape(application_url, quote=True)

    links = [""]

    if reside and reside.get("url"):
        reside_url = html.escape(reside["url"], quote=True)
        links.append(f'🏢 <a href="{reside_url}">View full Reside listing</a>')

    links.extend(
        [
            f'🗺️ <a href="{maps}">Open in Google Maps</a>',
            f'📝 <a href="{form}">Open Reside application</a>',
            "",
            "<i>Location data: © OpenStreetMap contributors</i>",
        ]
    )
    lines.extend(links)

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


def get_visible_option_records(page) -> dict[str, str | None]:
    """
    Return currently visible Airtable picker rows as:
        {visible project name: linked-record id or None}

    Airtable linked-record IDs look like recXXXXXXXXXXXXXX. Depending on the
    current Airtable renderer, the ID can live on the option itself, a child,
    or a nearby ancestor/data attribute, so inspect a small DOM neighborhood.
    """
    selectors = [
        '[role="option"]',
        '[role="listbox"] [role="option"]',
        '[role="menuitem"]',
        '[role="dialog"] [role="listitem"]',
    ]

    result = {}

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
                row_text = normalize(item.inner_text())
            except Exception:
                continue

            if not row_text or row_text.casefold() in IGNORE_TEXT:
                continue

            record_id = None

            try:
                record_id = item.evaluate(
                    """el => {
                        const recPattern = /rec[A-Za-z0-9]{10,}/;

                        const inspect = (node) => {
                            if (!node) return null;

                            // Attributes are the cheapest/cleanest location.
                            if (node.attributes) {
                                for (const attr of node.attributes) {
                                    const m = String(attr.value || '').match(recPattern);
                                    if (m) return m[0];
                                }
                            }

                            // Some Airtable versions serialize metadata into
                            // descendant markup/data attributes.
                            const html = String(node.outerHTML || '');
                            const m = html.match(recPattern);
                            return m ? m[0] : null;
                        };

                        let found = inspect(el);
                        if (found) return found;

                        // Walk only a few levels to avoid accidentally pairing
                        // this row with a sibling row's record ID.
                        let node = el.parentElement;
                        for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
                            found = inspect(node);
                            if (found) return found;
                        }

                        return null;
                    }"""
                )
            except Exception:
                record_id = None

            if record_id and not re.fullmatch(r"rec[A-Za-z0-9]{10,}", record_id):
                record_id = None

            # Prefer an actual ID over an earlier None from another selector.
            if row_text not in result or (record_id and not result[row_text]):
                result[row_text] = record_id

    return result


def get_visible_options(page) -> set[str]:
    """Backward-compatible visible-name helper."""
    return set(get_visible_option_records(page))
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


def scrape_options() -> tuple[list[str], dict[str, str]]:
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

            initial_records = get_visible_option_records(page)
            initial = set(initial_records)
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
            record_ids = {
                normalize(name): rec_id
                for name, rec_id in initial_records.items()
                if normalize(name) and rec_id
            }
            stable_at_bottom = 0
            previous_scroll_top = -1

            for pass_num in range(1, 251):
                visible_records = get_visible_option_records(page)
                visible = set(visible_records)
                before_count = len(collected)
                collected.update(visible)
                for name, rec_id in visible_records.items():
                    if rec_id:
                        record_ids[normalize(name)] = rec_id

                info = scroll_option_container_once(page)
                page.wait_for_timeout(350)

                if (
                    info.get("found")
                    and not info.get("moved")
                    and not info.get("atBottom")
                ):
                    if wheel_fallback(page):
                        page.wait_for_timeout(450)
                        extra_records = get_visible_option_records(page)
                        collected.update(extra_records)
                        for name, rec_id in extra_records.items():
                            if rec_id:
                                record_ids[normalize(name)] = rec_id
                        info = scroll_option_container_once(page)
                        page.wait_for_timeout(250)

                after_records = get_visible_option_records(page)
                collected.update(after_records)
                for name, rec_id in after_records.items():
                    if rec_id:
                        record_ids[normalize(name)] = rec_id

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

            clean_record_ids = {
                normalize(name): rec_id
                for name, rec_id in record_ids.items()
                if normalize(name) in set(options)
                and re.fullmatch(r"rec[A-Za-z0-9]{10,}", rec_id or "")
            }

            print(
                f"Finished: captured {len(options)} total unique option(s); "
                f"{len(clean_record_ids)} linked-record ID(s)."
            )
            return options, clean_record_ids

        except Exception:
            save_debug(page, "scrape_failure")
            raise
        finally:
            browser.close()


def process_listings(
    current_options: set[str],
    old_options: set[str],
    history: dict,
    project_record_ids: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Returns (new_events, reappeared_events, removed_entries).
    History is updated in-memory and saved by the caller after alerts succeed.
    """
    ensure_history_seeded(history, old_options)
    listings = history.setdefault("listings", {})
    now = utc_now_iso()

    current_by_key = {}
    project_record_ids = project_record_ids or {}

    for row in sorted(current_options, key=str.casefold):
        parsed = parse_listing_row(row)
        rec_id = project_record_ids.get(normalize(row))
        if rec_id and re.fullmatch(r"rec[A-Za-z0-9]{10,}", rec_id):
            parsed["airtable_record_id"] = rec_id
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
            # Try Reside first. This is optional enrichment and is attempted only
            # for genuinely new/reappearing listings.
            reside = enrich_from_reside(parsed)

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
                "airtable_record_id": parsed.get("airtable_record_id"),
                "first_seen": now,
                "first_seen_source": "observed",
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": location,
                "reside": reside,
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

        if not parsed.get("airtable_record_id") and entry.get("airtable_record_id"):
            parsed["airtable_record_id"] = entry.get("airtable_record_id")

        entry["address"] = parsed.get("address")
        entry["unit"] = parsed.get("unit")
        entry["rent"] = parsed.get("rent")
        entry["raw"] = parsed.get("raw")
        if parsed.get("airtable_record_id"):
            entry["airtable_record_id"] = parsed.get("airtable_record_id")
        entry["last_seen"] = now
        entry["active"] = True

        if not was_active:
            if not entry.get("reside"):
                entry["reside"] = enrich_from_reside(parsed)

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



# ---------------------------------------------------------------------------
# Fifth Avenue Committee (FAC) monitor
# ---------------------------------------------------------------------------

def clean_fac_ami(value: str | None) -> str | None:
    if not value:
        return None
    compact = re.sub(r"\s+", "", value)
    m = re.search(r"(\d{1,3})%", compact)
    return f"{m.group(1)}%" if m else normalize(value)


def parse_fac_money(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", value)
    if not m:
        return normalize(value)
    try:
        amount = float(m.group(1).replace(",", ""))
        return f"${amount:,.2f}"
    except ValueError:
        return "$" + m.group(1)


def fac_household_info(value: str | None) -> dict:
    """
    FAC publishes one household-size range plus one min/max income range per
    unit. Only call the income range "1-person income" when the FAC row is
    explicitly limited to one person; otherwise label it as FAC's published
    range rather than pretending it is household-specific.
    """
    value = normalize(value)
    nums = [int(x) for x in re.findall(r"\d+", value)]
    if not nums:
        return {
            "one_person_eligible": None,
            "one_person_only": False,
        }

    minimum = nums[0]
    maximum = nums[-1]
    return {
        "one_person_eligible": minimum <= 1,
        "one_person_only": minimum == 1 and maximum == 1,
    }


def fac_extract_address(building: str) -> str | None:
    """
    Pull a street address from headings such as:
      "The Axel -539 Vanderbilt Avenue, Brooklyn NY"
      "551 Warren Street, Brooklyn NY"

    When FAC only publishes a building name, keep the name for display/search
    rather than inventing an address.
    """
    building = normalize(building)

    after_dash = re.search(
        r"(?:^|[-–—])\s*(\d+\s+.+?)(?:,\s*(?:Brooklyn|New York|Bronx|Queens|Staten Island)\b|$)",
        building,
        flags=re.I,
    )
    if after_dash:
        return normalize(after_dash.group(1))

    direct = re.search(
        r"^(\d+\s+.+?)(?:,\s*(?:Brooklyn|New York|Bronx|Queens|Staten Island)\b|$)",
        building,
        flags=re.I,
    )
    if direct:
        return normalize(direct.group(1))

    return None


def fac_listing_key(listing: dict) -> str:
    building_or_address = (
        listing.get("address")
        or listing.get("building")
        or ""
    )
    base = re.sub(
        r"[^a-z0-9]+",
        " ",
        normalize(building_or_address).casefold(),
    ).strip()
    unit = re.sub(
        r"[^a-z0-9]+",
        " ",
        normalize(listing.get("unit", "")).casefold(),
    ).strip()
    return f"{base}||{unit}"


def scrape_fac_listings() -> list[dict]:
    """
    FAC listings are server-rendered HTML, so this is one lightweight HTTP
    request with no Playwright/Chrome.

    We walk the page's H2/H3/H4/list markup in document order:
      H2 -> current/upcoming section
      H3 -> building/address
      H4 -> unit
      LI -> unit fields
    """
    response = requests.get(
        FAC_URL,
        headers={
            "User-Agent": (
                "nyc-rerental-monitor/5.8 "
                "(personal affordable-housing availability notifier)"
            )
        },
        timeout=FAC_REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    section = None
    building = None
    current = None
    listings = []

    def finish_current():
        nonlocal current
        if current and current.get("unit"):
            current["address"] = fac_extract_address(current.get("building", ""))
            current["ami"] = clean_fac_ami(current.get("ami"))
            current["rent"] = parse_fac_money(current.get("rent"))
            current["min_income"] = parse_fac_money(current.get("min_income"))
            current["max_income"] = parse_fac_money(current.get("max_income"))
            current.update(fac_household_info(current.get("household_size")))
            listings.append(current)
        current = None

    for node in soup.find_all(["h2", "h3", "h4", "li"]):
        text_value = normalize(node.get_text(" ", strip=True))
        if not text_value:
            continue

        if node.name == "h2":
            lowered = text_value.casefold()
            if "current units available for re-rental" in lowered:
                finish_current()
                section = "current"
                building = None
            elif "upcoming units for re-rental" in lowered:
                finish_current()
                section = "upcoming"
                building = None
            continue

        if section not in {"current", "upcoming"}:
            continue

        if node.name == "h3":
            # Ignore repeated "Apply for a Re-Rental Unit!" headings.
            if "apply for a re-rental" in text_value.casefold():
                continue
            finish_current()
            building = text_value
            continue

        if node.name == "h4":
            if re.match(r"^Unit\b", text_value, flags=re.I):
                finish_current()
                unit = re.sub(r"^Unit\s*", "", text_value, flags=re.I).strip()
                current = {
                    "source": "FAC",
                    "status": section,
                    "building": building or "Fifth Avenue Committee listing",
                    "unit": unit,
                    "unit_size": None,
                    "household_size": None,
                    "ami": None,
                    "rent": None,
                    "min_income": None,
                    "max_income": None,
                }
            continue

        if node.name == "li" and current is not None:
            if ":" not in text_value:
                continue
            label, value = text_value.split(":", 1)
            label = normalize(label).casefold()
            value = normalize(value)

            if label == "unit size":
                current["unit_size"] = value
            elif label == "household size":
                current["household_size"] = value
            elif label == "ami":
                current["ami"] = value
            elif label == "rent":
                current["rent"] = value
            elif label == "min income":
                current["min_income"] = value
            elif label == "max income":
                current["max_income"] = value

    finish_current()

    if not listings:
        title = normalize(soup.title.get_text(" ", strip=True)) if soup.title else "(no title)"
        content_type = response.headers.get("content-type", "(unknown)")
        raise RuntimeError(
            "FAC page loaded but no re-rental units were parsed. "
            "Refusing to update the FAC baseline. "
            f"status={response.status_code}; "
            f"final_url={response.url}; "
            f"bytes={len(response.content)}; "
            f"content_type={content_type}; "
            f"title={title!r}"
        )

    print(
        "FAC scrape: "
        f"{len(listings)} total listing(s) "
        f"({sum(x['status'] == 'current' for x in listings)} current, "
        f"{sum(x['status'] == 'upcoming' for x in listings)} upcoming)."
    )
    return listings


def load_fac_state() -> dict:
    if not FAC_STATE_PATH.exists():
        return {
            "initialized": False,
            "listings": {},
        }
    try:
        data = json.loads(FAC_STATE_PATH.read_text())
        if not isinstance(data.get("listings"), dict):
            data["listings"] = {}
        return data
    except Exception as exc:
        raise RuntimeError(f"Could not read {FAC_STATE_PATH}: {exc}") from exc


def save_fac_state(listings: list[dict]) -> None:
    payload = {
        fac_listing_key(item): {
            "status": item.get("status"),
            "building": item.get("building"),
            "address": item.get("address"),
            "unit": item.get("unit"),
            "rent": item.get("rent"),
        }
        for item in listings
    }
    FAC_STATE_PATH.write_text(
        json.dumps(
            {
                "initialized": True,
                "updated_at": utc_now_iso(),
                "listings": payload,
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )


def load_fac_history() -> dict:
    if not FAC_HISTORY_PATH.exists():
        return {
            "version": 1,
            "created_at": utc_now_iso(),
            "listings": {},
        }
    try:
        data = json.loads(FAC_HISTORY_PATH.read_text())
        if not isinstance(data.get("listings"), dict):
            data["listings"] = {}
        return data
    except Exception as exc:
        raise RuntimeError(f"Could not read {FAC_HISTORY_PATH}: {exc}") from exc


def save_fac_history(history: dict) -> None:
    history["version"] = 1
    history["updated_at"] = utc_now_iso()
    FAC_HISTORY_PATH.write_text(
        json.dumps(history, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )


def validate_fac_scrape(current: list[dict], previous_state: dict) -> None:
    current_count = len(current)
    previous_count = len(previous_state.get("listings", {}))

    if current_count == 0:
        raise IncompleteScrapeError(
            "FAC captured 0 listings. Previous FAC baseline preserved."
        )

    if previous_count == 0:
        return

    ratio = current_count / previous_count
    drop = previous_count - current_count

    # FAC currently has a relatively small list, so use a more conservative
    # protection than the Reside threshold.
    if drop >= 3 and ratio < 0.55:
        raise IncompleteScrapeError(
            f"Suspicious FAC listing-count drop: {previous_count} -> "
            f"{current_count}. FAC baseline preserved."
        )


def fac_location_query(listing: dict) -> str:
    if listing.get("address"):
        # The FAC page currently serves Brooklyn listings, but keep NYC generic
        # enough for future Manhattan/Queens/etc entries.
        return listing["address"]

    # Building-name fallback (e.g. "Paseo on Fifth"). Nominatim can often
    # resolve a named NYC building even when FAC omits the street address.
    return listing.get("building") or ""


def build_fac_notification(
    listing: dict,
    location: dict | None,
    event_type: str,
    first_seen: str,
    last_removed_at: str | None = None,
    status_changed_from: str | None = None,
) -> str:
    priority = priority_for_location(location)

    if status_changed_from and listing.get("status") == "current":
        heading = "🟢 <b>FAC LISTING NOW CURRENT</b>"
    elif event_type == "reappeared":
        heading = "🔄 <b>FAC RE-RENTAL REAPPEARED</b>"
    else:
        heading = "🏢 <b>NEW FAC RE-RENTAL</b>"

    status = listing.get("status")
    status_line = (
        "🟢 <b>CURRENTLY AVAILABLE</b>"
        if status == "current"
        else "🟡 <b>UPCOMING</b>"
    )

    building = html.escape(normalize(listing.get("building")) or "FAC listing")
    unit = html.escape(normalize(listing.get("unit")))

    lines = [
        heading,
        status_line,
        f"{priority['emoji']} <b>{priority['label']} · {priority['score']}/3</b>",
        f"<i>{html.escape(priority['reason'])}</i>",
        "",
        f"🏠 <b>{building} — Unit {unit}</b>",
    ]

    if listing.get("address"):
        lines.append(f"📫 {html.escape(listing['address'])}")

    if listing.get("rent"):
        lines.append(f"💰 <b>{html.escape(listing['rent'])}/mo</b>")

    if listing.get("unit_size"):
        lines.append(f"🛏️ <b>{html.escape(listing['unit_size'])}</b>")

    if listing.get("ami"):
        lines.append(f"📊 AMI: <b>{html.escape(listing['ami'])}</b>")

    one_person_eligible = listing.get("one_person_eligible")
    one_person_only = listing.get("one_person_only")

    if one_person_eligible is False:
        lines.append("👤 1-person household: <b>Not eligible</b>")
    elif one_person_eligible is True:
        lines.append("👤 1-person household: <b>Eligible</b>")

        if listing.get("min_income") and listing.get("max_income"):
            range_text = (
                f"{listing['min_income']} – {listing['max_income']}"
            )
            if one_person_only:
                lines.append(
                    f"💵 1-person income: <b>{html.escape(range_text)}</b>"
                )
            else:
                lines.append(
                    "💵 FAC published income range: "
                    f"<b>{html.escape(range_text)}</b>"
                )

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

    lines.append(f"🕐 First detected: <b>{html.escape(format_et(first_seen))}</b>")

    if event_type == "reappeared" and last_removed_at:
        duration = human_duration(last_removed_at, utc_now_iso())
        if duration:
            lines.append(f"↩️ Reappeared after <b>{html.escape(duration)}</b>")

    if status_changed_from and status_changed_from != status:
        lines.append(
            "🔁 Status: "
            f"<b>{html.escape(status_changed_from.title())} → "
            f"{html.escape(status.title())}</b>"
        )

    map_query = fac_location_query(listing)
    maps = html.escape(google_maps_url(map_query), quote=True)
    fac_url = html.escape(FAC_URL, quote=True)

    lines.extend(
        [
            "",
            f'🗺️ <a href="{maps}">Open in Google Maps</a>',
            f'📝 <a href="{fac_url}">Open FAC re-rental page</a>',
            "",
            "<i>Location data: © OpenStreetMap contributors</i>",
        ]
    )

    return "\n".join(lines)


def seed_fac_history(history: dict, listings: list[dict]) -> None:
    if history.get("listings"):
        return

    now = utc_now_iso()
    for listing in listings:
        key = fac_listing_key(listing)
        history["listings"][key] = {
            **listing,
            "first_seen": now,
            "first_seen_source": "fac_history_migration",
            "last_seen": now,
            "active": True,
            "appearance_count": 1,
            "removal_count": 0,
            "last_removed_at": None,
            "location": None,
        }



def fetch_fac_with_retries(state: dict) -> list[dict] | None:
    """
    Retry transient FAC fetch/parse failures without touching the last good
    baseline. A failed attempt can be an HTTP/CDN hiccup, a partial page, or a
    temporary HTML variant that parses to zero rows.

    Returns None after all attempts fail. That is a soft FAC-source failure:
    Reside/Rockrose continue, FAC state/history remain untouched, and the next
    scheduled run tries again.
    """
    last_error = None

    for attempt in range(1, FAC_MAX_ATTEMPTS + 1):
        delay = FAC_RETRY_DELAYS[min(attempt - 1, len(FAC_RETRY_DELAYS) - 1)]
        if delay:
            print(f"FAC retry {attempt}/{FAC_MAX_ATTEMPTS}: waiting {delay}s...")
            time.sleep(delay)

        try:
            listings = scrape_fac_listings()
            validate_fac_scrape(listings, state)
            if attempt > 1:
                print(f"FAC recovered on attempt {attempt}/{FAC_MAX_ATTEMPTS}.")
            return listings
        except (requests.RequestException, IncompleteScrapeError, RuntimeError) as exc:
            last_error = exc
            print(
                f"FAC attempt {attempt}/{FAC_MAX_ATTEMPTS} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    print(
        "FAC unavailable after "
        f"{FAC_MAX_ATTEMPTS} attempts. Preserving fac_state.json and "
        "fac_history.json and skipping FAC for this run. "
        f"Last error: {type(last_error).__name__}: {last_error}",
        file=sys.stderr,
    )
    return None



def run_fac_monitor() -> None:
    """
    Runs independently of Reside. FAC alerts are sent from this worker as soon
    as they are detected; it does not wait for the Airtable worker to finish.
    """
    state = load_fac_state()
    history = load_fac_history()

    listings = fetch_fac_with_retries(state)
    if listings is None:
        return

    if not state.get("initialized"):
        save_fac_state(listings)
        seed_fac_history(history, listings)
        save_fac_history(history)
        print(
            f"FAC initialized with {len(listings)} existing listing(s); "
            "no existing listings alerted."
        )
        return

    old_state = state.get("listings", {})
    history_entries = history.setdefault("listings", {})
    now = utc_now_iso()

    current_by_key = {
        fac_listing_key(item): item
        for item in listings
    }
    current_keys = set(current_by_key)

    removed = []
    for key, entry in history_entries.items():
        if entry.get("active", False) and key not in current_keys:
            entry["active"] = False
            entry["last_removed_at"] = now
            entry["removal_count"] = int(entry.get("removal_count", 0)) + 1
            removed.append(entry)

    alert_events = []

    for key, listing in current_by_key.items():
        entry = history_entries.get(key)
        old_snapshot = old_state.get(key) or {}

        if entry is None:
            query = fac_location_query(listing)
            location = geocode_nyc_address(query) if query else None

            entry = {
                **listing,
                "first_seen": now,
                "first_seen_source": "observed",
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": location,
            }
            history_entries[key] = entry
            alert_events.append(
                {
                    "listing": listing,
                    "entry": entry,
                    "event_type": "new",
                    "last_removed_at": None,
                    "status_changed_from": None,
                }
            )
            continue

        was_active = bool(entry.get("active", False))
        last_removed_at = entry.get("last_removed_at")
        prior_status = entry.get("status") or old_snapshot.get("status")

        for field, value in listing.items():
            entry[field] = value
        entry["last_seen"] = now
        entry["active"] = True

        if not entry.get("location"):
            query = fac_location_query(listing)
            if query:
                entry["location"] = geocode_nyc_address(query)

        if not was_active:
            entry["appearance_count"] = int(entry.get("appearance_count", 1)) + 1
            alert_events.append(
                {
                    "listing": listing,
                    "entry": entry,
                    "event_type": "reappeared",
                    "last_removed_at": last_removed_at,
                    "status_changed_from": None,
                }
            )
        elif prior_status and prior_status != listing.get("status"):
            # Particularly useful when an "Upcoming" unit becomes "Current".
            alert_events.append(
                {
                    "listing": listing,
                    "entry": entry,
                    "event_type": "status_changed",
                    "last_removed_at": None,
                    "status_changed_from": prior_status,
                }
            )

    # Alert before committing the new baseline. If Telegram errors, the FAC
    # state stays on the prior snapshot and the event can be retried next run.
    for event in alert_events:
        message = build_fac_notification(
            listing=event["listing"],
            location=event["entry"].get("location"),
            event_type=event["event_type"],
            first_seen=event["entry"].get("first_seen"),
            last_removed_at=event.get("last_removed_at"),
            status_changed_from=event.get("status_changed_from"),
        )
        send_telegram(message, parse_mode="HTML")

    for entry in removed:
        print(
            "FAC removed: "
            f"{entry.get('building')} Unit {entry.get('unit')} "
            f"(removal #{entry.get('removal_count', 1)})"
        )

    save_fac_state(listings)
    save_fac_history(history)

    print(
        f"FAC complete: {len(alert_events)} alert event(s), "
        f"{len(removed)} removal(s)."
    )




# ---------------------------------------------------------------------------
# Rockrose affordable availabilities monitor
# ---------------------------------------------------------------------------

def rockrose_listing_key(listing: dict) -> str:
    address = re.sub(
        r"[^a-z0-9]+", " ", normalize(listing.get("address", "")).casefold()
    ).strip()
    unit = re.sub(
        r"[^a-z0-9]+", " ", normalize(listing.get("unit", "")).casefold()
    ).strip()
    return f"{address}||{unit}"


def scrape_rockrose_index() -> tuple[list[dict], bool]:
    response = requests.get(
        ROCKROSE_URL,
        headers={
            "User-Agent": (
                "nyc-rerental-monitor/5.9 "
                "(personal affordable-housing availability notifier)"
            )
        },
        timeout=ROCKROSE_REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    listings = []

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        url = urljoin(ROCKROSE_URL, href)

        if "/affordable-availabilities/" not in url:
            continue
        if url.rstrip("/") == ROCKROSE_URL.rstrip("/"):
            continue

        text_value = normalize(anchor.get_text(" ", strip=True))
        if not text_value:
            continue
        if "waitlist application" in text_value.casefold():
            continue

        unit_match = re.search(r"#\s*([A-Za-z0-9-]+)\b", text_value)
        if not unit_match:
            continue

        address = normalize(text_value[:unit_match.start()])
        if not address:
            continue

        rent_match = re.search(
            r"1\s*Year\s*Rent\s*:\s*\$\s*([\d,\s]+(?:\.\d{1,2})?)",
            text_value,
            flags=re.I,
        )
        type_match = re.search(
            r"(?:Apartment|Unit)\s*Type\s*:\s*"
            r"(.+?)(?=\s+Income\s+Range\s*:|\s+View\b|$)",
            text_value,
            flags=re.I,
        )
        income_match = re.search(
            r"Income\s*Range\s*:\s*"
            r"(\$\s*[\d,\s]+(?:\.\d{1,2})?\s*[-–—]\s*"
            r"\$\s*[\d,\s]+(?:\.\d{1,2})?)",
            text_value,
            flags=re.I,
        )

        def clean_money(raw):
            if not raw:
                return None
            raw = re.sub(r"\s+", "", raw)
            m = re.search(r"\$?([\d,]+(?:\.\d{1,2})?)", raw)
            if not m:
                return normalize(raw)
            try:
                return f"${float(m.group(1).replace(',', '')):,.2f}"
            except ValueError:
                return "$" + m.group(1)

        listings.append(
            {
                "source": "Rockrose",
                "address": address,
                "unit": normalize(unit_match.group(1)),
                "rent": clean_money(rent_match.group(1)) if rent_match else None,
                "unit_size": normalize(type_match.group(1)) if type_match else None,
                "published_income_range": normalize(income_match.group(1)) if income_match else None,
                "url": url,
            }
        )

    deduped = {rockrose_listing_key(item): item for item in listings}
    listings = list(deduped.values())

    page_text = normalize(soup.get_text(" ", strip=True)).casefold()
    explicitly_empty = (
        "there is currently no affordable housing availability at this time"
        in page_text
    )

    if not listings:
        if explicitly_empty:
            print(
                "Rockrose scrape: page explicitly reports no affordable "
                "housing availability; treating as 0 live listing(s)."
            )
            return [], True

        raise RuntimeError(
            "Rockrose page loaded but no live affordable availability cards "
            "were parsed and no explicit zero-availability message was found. "
            "Previous Rockrose baseline preserved."
        )

    print(f"Rockrose scrape: {len(listings)} live listing(s).")
    return listings, False


def enrich_rockrose_listing(listing: dict) -> dict:
    details = {
        "ami": None,
        "one_person_income": None,
        "one_person_eligible": None,
        "application_deadline": None,
        "application_email": None,
    }

    url = listing.get("url")
    if not url:
        return details

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "nyc-rerental-monitor/5.9 "
                    "(personal affordable-housing availability notifier)"
                )
            },
            timeout=ROCKROSE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"Rockrose detail enrichment failed: {exc}")
        return details

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = normalize(soup.get_text(" ", strip=True))

    ami_match = re.search(
        r"\b(\d{1,3})\s*%\s*AMI\b|\bAMI\b.{0,40}?\b(\d{1,3})\s*%",
        page_text,
        flags=re.I,
    )
    if ami_match:
        pct = ami_match.group(1) or ami_match.group(2)
        details["ami"] = f"{pct}%"

    one_match = re.search(
        r"(\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*"
        r"\$\s*[\d,]+(?:\.\d{1,2})?)\s*"
        r"for\s+a\s+one-person\s+household",
        page_text,
        flags=re.I,
    )
    if not one_match:
        one_match = re.search(
            r"one-person\s+household.{0,80}?"
            r"(\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*"
            r"\$\s*[\d,]+(?:\.\d{1,2})?)",
            page_text,
            flags=re.I,
        )

    if one_match:
        details["one_person_income"] = normalize(one_match.group(1))
        details["one_person_eligible"] = True
    elif (
        re.search(r"two-person household", page_text, flags=re.I)
        and not re.search(r"one-person household", page_text, flags=re.I)
    ):
        details["one_person_eligible"] = False

    deadline_match = re.search(
        r"(?:Must\s+apply\s+by|Apply\s+by)\s+"
        r"([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})",
        page_text,
        flags=re.I,
    )
    if deadline_match:
        details["application_deadline"] = normalize(deadline_match.group(1))

    emails = re.findall(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        page_text,
        flags=re.I,
    )
    if emails:
        preferred = [
            e for e in emails
            if "housingpartnership" in e.casefold()
            or "affordable" in e.casefold()
        ]
        details["application_email"] = preferred[0] if preferred else emails[0]

    type_match = re.search(
        r"(?:Apartment|Unit)\s*Type\s*:\s*(Studio|\d+\s*Bedroom)",
        page_text,
        flags=re.I,
    )
    if type_match:
        listing["unit_size"] = normalize(type_match.group(1))

    return details


def load_rockrose_state() -> dict:
    if not ROCKROSE_STATE_PATH.exists():
        return {"initialized": False, "listings": {}}
    data = json.loads(ROCKROSE_STATE_PATH.read_text())
    if not isinstance(data.get("listings"), dict):
        data["listings"] = {}
    return data


def save_rockrose_state(listings: list[dict]) -> None:
    payload = {
        rockrose_listing_key(item): {
            "address": item.get("address"),
            "unit": item.get("unit"),
            "rent": item.get("rent"),
            "url": item.get("url"),
        }
        for item in listings
    }
    ROCKROSE_STATE_PATH.write_text(
        json.dumps(
            {
                "initialized": True,
                "updated_at": utc_now_iso(),
                "listings": payload,
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
    )


def load_rockrose_history() -> dict:
    if not ROCKROSE_HISTORY_PATH.exists():
        return {"version": 1, "created_at": utc_now_iso(), "listings": {}}
    data = json.loads(ROCKROSE_HISTORY_PATH.read_text())
    if not isinstance(data.get("listings"), dict):
        data["listings"] = {}
    return data


def save_rockrose_history(history: dict) -> None:
    history["version"] = 1
    history["updated_at"] = utc_now_iso()
    ROCKROSE_HISTORY_PATH.write_text(
        json.dumps(history, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )


def validate_rockrose_scrape(
    current: list[dict],
    previous_state: dict,
    explicitly_empty: bool = False,
) -> None:
    current_count = len(current)
    previous_count = len(previous_state.get("listings", {}))

    if current_count == 0:
        if explicitly_empty:
            return
        raise IncompleteScrapeError(
            "Rockrose captured 0 listings without an explicit "
            "zero-availability message. Previous baseline preserved."
        )

    if previous_count == 0:
        return

    ratio = current_count / previous_count
    drop = previous_count - current_count
    if previous_count >= 4 and drop >= 3 and ratio < 0.40:
        raise IncompleteScrapeError(
            f"Suspicious Rockrose listing-count drop: "
            f"{previous_count} -> {current_count}. Baseline preserved."
        )


def seed_rockrose_history(history: dict, listings: list[dict]) -> None:
    if history.get("listings"):
        return
    now = utc_now_iso()
    for listing in listings:
        key = rockrose_listing_key(listing)
        history["listings"][key] = {
            **listing,
            "first_seen": now,
            "first_seen_source": "rockrose_history_migration",
            "last_seen": now,
            "active": True,
            "appearance_count": 1,
            "removal_count": 0,
            "last_removed_at": None,
            "location": None,
            "details": None,
        }


def build_rockrose_notification(
    listing: dict,
    details: dict,
    location: dict | None,
    event_type: str,
    first_seen: str,
    last_removed_at: str | None = None,
) -> str:
    priority = priority_for_location(location)
    heading = (
        "🔄 <b>ROCKROSE LISTING REAPPEARED</b>"
        if event_type == "reappeared"
        else "🌹 <b>NEW ROCKROSE AFFORDABLE LISTING</b>"
    )

    address = html.escape(normalize(listing.get("address")))
    unit = html.escape(normalize(listing.get("unit")))

    lines = [
        heading,
        f"{priority['emoji']} <b>{priority['label']} · {priority['score']}/3</b>",
        f"<i>{html.escape(priority['reason'])}</i>",
        "",
        f"🏠 <b>{address} — Apt {unit}</b>",
    ]

    if listing.get("rent"):
        lines.append(f"💰 <b>{html.escape(listing['rent'])}/mo</b>")
    if listing.get("unit_size"):
        lines.append(f"🛏️ <b>{html.escape(listing['unit_size'])}</b>")
    if details.get("ami"):
        lines.append(f"📊 AMI: <b>{html.escape(details['ami'])}</b>")

    if details.get("one_person_income"):
        lines.append(
            "👤 1-person income: "
            f"<b>{html.escape(details['one_person_income'])}</b>"
        )
    elif details.get("one_person_eligible") is False:
        lines.append("👤 1-person household: <b>Not eligible</b>")

    if location:
        neighborhood = normalize(location.get("neighborhood"))
        borough = normalize(location.get("borough"))
        postcode = normalize(location.get("postcode"))
        if neighborhood and borough:
            lines.append(f"📍 <b>{html.escape(neighborhood)}, {html.escape(borough)}</b>")
        elif borough:
            lines.append(f"📍 <b>{html.escape(borough)}</b>")
        elif neighborhood:
            lines.append(f"📍 <b>{html.escape(neighborhood)}</b>")
        if postcode:
            lines.append(f"📮 {html.escape(postcode)}")

    if details.get("application_deadline"):
        lines.append(f"⏳ Apply by: <b>{html.escape(details['application_deadline'])}</b>")
    if details.get("application_email"):
        lines.append(f"✉️ Application email: <b>{html.escape(details['application_email'])}</b>")

    lines.append(f"🕐 First detected: <b>{html.escape(format_et(first_seen))}</b>")

    if event_type == "reappeared" and last_removed_at:
        duration = human_duration(last_removed_at, utc_now_iso())
        if duration:
            lines.append(f"↩️ Reappeared after <b>{html.escape(duration)}</b>")

    maps = html.escape(google_maps_url(listing.get("address", "")), quote=True)
    detail_url = html.escape(listing.get("url") or ROCKROSE_URL, quote=True)
    archive_url = html.escape(ROCKROSE_URL, quote=True)

    lines.extend(
        [
            "",
            f'🌹 <a href="{detail_url}">View Rockrose listing</a>',
            f'🗺️ <a href="{maps}">Open in Google Maps</a>',
            f'📋 <a href="{archive_url}">Rockrose affordable availabilities</a>',
            "",
            "<i>Location data: © OpenStreetMap contributors</i>",
        ]
    )
    return "\n".join(lines)


def run_rockrose_monitor() -> None:
    state = load_rockrose_state()
    history = load_rockrose_history()

    listings, explicitly_empty = scrape_rockrose_index()
    validate_rockrose_scrape(listings, state, explicitly_empty=explicitly_empty)

    if not state.get("initialized"):
        save_rockrose_state(listings)
        seed_rockrose_history(history, listings)
        save_rockrose_history(history)
        print(
            f"Rockrose initialized with {len(listings)} existing listing(s); "
            "no existing listings alerted."
        )
        return

    history_entries = history.setdefault("listings", {})
    current_by_key = {rockrose_listing_key(item): item for item in listings}
    current_keys = set(current_by_key)
    now = utc_now_iso()

    removed = []
    for key, entry in history_entries.items():
        if entry.get("active", False) and key not in current_keys:
            entry["active"] = False
            entry["last_removed_at"] = now
            entry["removal_count"] = int(entry.get("removal_count", 0)) + 1
            removed.append(entry)

    alerts = []

    for key, listing in current_by_key.items():
        entry = history_entries.get(key)

        if entry is None:
            details = enrich_rockrose_listing(listing)
            location = geocode_nyc_address(listing.get("address", ""))

            entry = {
                **listing,
                "first_seen": now,
                "first_seen_source": "observed",
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": location,
                "details": details,
            }
            history_entries[key] = entry
            alerts.append(
                {
                    "listing": listing,
                    "entry": entry,
                    "event_type": "new",
                    "last_removed_at": None,
                }
            )
            continue

        was_active = bool(entry.get("active", False))
        last_removed_at = entry.get("last_removed_at")
        for field, value in listing.items():
            entry[field] = value
        entry["last_seen"] = now
        entry["active"] = True

        if not was_active:
            details = entry.get("details") or enrich_rockrose_listing(listing)
            location = entry.get("location") or geocode_nyc_address(listing.get("address", ""))
            entry["details"] = details
            entry["location"] = location
            entry["appearance_count"] = int(entry.get("appearance_count", 1)) + 1
            alerts.append(
                {
                    "listing": listing,
                    "entry": entry,
                    "event_type": "reappeared",
                    "last_removed_at": last_removed_at,
                }
            )

    for event in alerts:
        entry = event["entry"]
        send_telegram(
            build_rockrose_notification(
                listing=event["listing"],
                details=entry.get("details") or {},
                location=entry.get("location"),
                event_type=event["event_type"],
                first_seen=entry.get("first_seen"),
                last_removed_at=event.get("last_removed_at"),
            ),
            parse_mode="HTML",
        )

    for entry in removed:
        print(
            "Rockrose removed: "
            f"{entry.get('address')} Apt {entry.get('unit')} "
            f"(removal #{entry.get('removal_count', 1)})"
        )

    save_rockrose_state(listings)
    save_rockrose_history(history)
    print(
        f"Rockrose complete: {len(alerts)} alert event(s), "
        f"{len(removed)} removal(s)."
    )




# ---------------------------------------------------------------------------
# MNS affordable units monitor
# ---------------------------------------------------------------------------

def mns_listing_key(listing: dict) -> str:
    address = re.sub(
        r"[^a-z0-9]+", " ",
        normalize(listing.get("address", "")).casefold(),
    ).strip()
    unit = re.sub(
        r"[^a-z0-9]+", " ",
        normalize(listing.get("unit", "")).casefold(),
    ).strip()
    return f"{address}||{unit}"


def scrape_mns_index() -> list[dict]:
    response = requests.get(
        MNS_URL,
        headers={
            "User-Agent": (
                "nyc-rerental-monitor/5.11 "
                "(personal affordable-housing availability notifier)"
            )
        },
        timeout=MNS_REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    listings = []

    for row in soup.find_all("tr"):
        link = row.find("a", href=re.compile(r"/details/", re.I))
        if not link:
            continue

        raw_cells = [normalize(td.get_text(" ", strip=True)) for td in row.find_all("td")]
        if len(raw_cells) < 2:
            continue

        address = raw_cells[0]
        unit = raw_cells[1]
        if not address or not unit:
            continue

        rent = None
        if len(raw_cells) > 6:
            price_match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", raw_cells[6])
            if price_match:
                try:
                    rent = f"${float(price_match.group(1).replace(',', '')):,.2f}"
                except ValueError:
                    rent = "$" + price_match.group(1)

        sqft = raw_cells[2] if len(raw_cells) > 2 and re.fullmatch(r"[\d,]+", raw_cells[2] or "") else None
        beds = raw_cells[4] if len(raw_cells) > 4 and re.fullmatch(r"\d+(?:\.\d+)?", raw_cells[4] or "") else None
        baths = raw_cells[5] if len(raw_cells) > 5 and re.fullmatch(r"\d+(?:\.\d+)?", raw_cells[5] or "") else None

        if beds == "0":
            unit_size = "Studio"
        elif beds:
            unit_size = f"{beds} Bedroom" + ("" if beds == "1" else "s")
        else:
            unit_size = None

        listings.append(
            {
                "source": "MNS",
                "address": address,
                "unit": unit,
                "rent": rent,
                "unit_size": unit_size,
                "bedrooms": beds,
                "bathrooms": baths,
                "sqft": sqft,
                "url": urljoin(MNS_URL, link.get("href")),
            }
        )

    listings = list({mns_listing_key(item): item for item in listings}.values())

    if not listings:
        title = normalize(soup.title.get_text(" ", strip=True)) if soup.title else "(no title)"
        content_type = response.headers.get("content-type", "(unknown)")
        raise RuntimeError(
            "MNS page loaded but no affordable-unit rows were parsed. "
            "Refusing to update the MNS baseline. "
            f"status={response.status_code}; final_url={response.url}; "
            f"bytes={len(response.content)}; content_type={content_type}; "
            f"title={title!r}"
        )

    print(f"MNS scrape: {len(listings)} live affordable listing(s).")
    return listings


def enrich_mns_listing(listing: dict) -> dict:
    details = {
        "minimum_income": None,
        "one_person_max_income": None,
        "one_person_income_range": None,
        "one_person_eligible": None,
        "neighborhood": None,
        "contact_phone": None,
        "available_now": None,
    }

    url = listing.get("url")
    if not url:
        return details

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "nyc-rerental-monitor/5.11 "
                    "(personal affordable-housing availability notifier)"
                )
            },
            timeout=MNS_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"MNS detail enrichment failed: {exc}")
        return details

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = normalize(soup.get_text(" ", strip=True))

    min_match = re.search(
        r"MINIMUM\s+INCOME\s*:\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
        page_text,
        flags=re.I,
    )
    if min_match:
        try:
            details["minimum_income"] = f"${float(min_match.group(1).replace(',', '')):,.2f}"
        except ValueError:
            details["minimum_income"] = "$" + min_match.group(1)

    one_match = re.search(
        r"(?:MAXIMUM\s+INCOME\s*:?\s*)?1\s*PERSON\s*:\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
        page_text,
        flags=re.I,
    )
    if one_match:
        try:
            one_max = f"${float(one_match.group(1).replace(',', '')):,.2f}"
        except ValueError:
            one_max = "$" + one_match.group(1)
        details["one_person_max_income"] = one_max
        details["one_person_eligible"] = True
        if details["minimum_income"]:
            details["one_person_income_range"] = f"{details['minimum_income']} – {one_max}"
    elif re.search(r"\b2\s*PERSON\s*:", page_text, flags=re.I):
        details["one_person_eligible"] = False

    neighborhood_match = re.search(
        r"rental\s+unit\s+in\s+([A-Za-z0-9 '&.\-]+)",
        page_text,
        flags=re.I,
    )
    if neighborhood_match:
        details["neighborhood"] = normalize(neighborhood_match.group(1))

    phone_match = re.search(
        r"\b(?:\+1[-.\s]?)?\(?(\d{3})\)?[-.\s](\d{3})[-.\s](\d{4})\b",
        page_text,
    )
    if phone_match:
        details["contact_phone"] = (
            f"{phone_match.group(1)}-{phone_match.group(2)}-{phone_match.group(3)}"
        )

    if re.search(r"\bAvailable Now\b", page_text, flags=re.I):
        details["available_now"] = True

    return details


def load_mns_state() -> dict:
    if not MNS_STATE_PATH.exists():
        return {"initialized": False, "listings": {}}
    try:
        data = json.loads(MNS_STATE_PATH.read_text())
        if not isinstance(data.get("listings"), dict):
            data["listings"] = {}
        return data
    except Exception as exc:
        raise RuntimeError(f"Could not read {MNS_STATE_PATH}: {exc}") from exc


def save_mns_state(listings: list[dict]) -> None:
    payload = {
        mns_listing_key(item): {
            "address": item.get("address"),
            "unit": item.get("unit"),
            "rent": item.get("rent"),
            "url": item.get("url"),
        }
        for item in listings
    }
    MNS_STATE_PATH.write_text(
        json.dumps(
            {
                "initialized": True,
                "updated_at": utc_now_iso(),
                "listings": payload,
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
    )


def load_mns_history() -> dict:
    if not MNS_HISTORY_PATH.exists():
        return {"version": 1, "created_at": utc_now_iso(), "listings": {}}
    try:
        data = json.loads(MNS_HISTORY_PATH.read_text())
        if not isinstance(data.get("listings"), dict):
            data["listings"] = {}
        return data
    except Exception as exc:
        raise RuntimeError(f"Could not read {MNS_HISTORY_PATH}: {exc}") from exc


def save_mns_history(history: dict) -> None:
    history["version"] = 1
    history["updated_at"] = utc_now_iso()
    MNS_HISTORY_PATH.write_text(
        json.dumps(history, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )


def validate_mns_scrape(current: list[dict], previous_state: dict) -> None:
    current_count = len(current)
    previous_count = len(previous_state.get("listings", {}))
    if current_count == 0:
        raise IncompleteScrapeError(
            "MNS captured 0 listings. Previous MNS baseline preserved."
        )
    if previous_count == 0:
        return
    ratio = current_count / previous_count
    drop = previous_count - current_count
    if previous_count >= 5 and drop >= 3 and ratio < 0.40:
        raise IncompleteScrapeError(
            f"Suspicious MNS listing-count drop: {previous_count} -> "
            f"{current_count}. Baseline preserved."
        )


def fetch_mns_with_retries(state: dict) -> list[dict] | None:
    last_error = None
    for attempt in range(1, MNS_MAX_ATTEMPTS + 1):
        delay = MNS_RETRY_DELAYS[min(attempt - 1, len(MNS_RETRY_DELAYS) - 1)]
        if delay:
            print(f"MNS retry {attempt}/{MNS_MAX_ATTEMPTS}: waiting {delay}s...")
            time.sleep(delay)
        try:
            listings = scrape_mns_index()
            validate_mns_scrape(listings, state)
            if attempt > 1:
                print(f"MNS recovered on attempt {attempt}/{MNS_MAX_ATTEMPTS}.")
            return listings
        except (requests.RequestException, IncompleteScrapeError, RuntimeError) as exc:
            last_error = exc
            print(
                f"MNS attempt {attempt}/{MNS_MAX_ATTEMPTS} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    print(
        f"MNS unavailable after {MNS_MAX_ATTEMPTS} attempts. "
        "Preserving MNS state/history and skipping MNS for this run. "
        f"Last error: {type(last_error).__name__}: {last_error}",
        file=sys.stderr,
    )
    return None


def seed_mns_history(history: dict, listings: list[dict]) -> None:
    if history.get("listings"):
        return
    now = utc_now_iso()
    for listing in listings:
        key = mns_listing_key(listing)
        history["listings"][key] = {
            **listing,
            "first_seen": now,
            "first_seen_source": "mns_history_migration",
            "last_seen": now,
            "active": True,
            "appearance_count": 1,
            "removal_count": 0,
            "last_removed_at": None,
            "location": None,
            "details": None,
        }


def build_mns_notification(
    listing: dict,
    details: dict,
    location: dict | None,
    event_type: str,
    first_seen: str,
    last_removed_at: str | None = None,
) -> str:
    priority = priority_for_location(location)
    heading = (
        "🔄 <b>MNS AFFORDABLE LISTING REAPPEARED</b>"
        if event_type == "reappeared"
        else "🏙️ <b>NEW MNS AFFORDABLE LISTING</b>"
    )

    lines = [
        heading,
        f"{priority['emoji']} <b>{priority['label']} · {priority['score']}/3</b>",
        f"<i>{html.escape(priority['reason'])}</i>",
        "",
        f"🏠 <b>{html.escape(normalize(listing.get('address')))} — "
        f"Apt {html.escape(normalize(listing.get('unit')))}</b>",
    ]

    if listing.get("rent"):
        lines.append(f"💰 <b>{html.escape(listing['rent'])}/mo</b>")

    size_parts = []
    if listing.get("unit_size"):
        size_parts.append(listing["unit_size"])
    if listing.get("bathrooms"):
        size_parts.append(f"{listing['bathrooms']} bath")
    if listing.get("sqft"):
        size_parts.append(f"{listing['sqft']} sq ft")
    if size_parts:
        lines.append(f"🛏️ <b>{html.escape(' · '.join(size_parts))}</b>")

    if details.get("one_person_income_range"):
        lines.append(
            "👤 1-person income: "
            f"<b>{html.escape(details['one_person_income_range'])}</b>"
        )
    elif details.get("one_person_eligible") is False:
        lines.append("👤 1-person household: <b>Not eligible</b>")
    elif details.get("one_person_max_income"):
        lines.append(
            "👤 1-person max income: "
            f"<b>{html.escape(details['one_person_max_income'])}</b>"
        )

    neighborhood = normalize(location.get("neighborhood")) if location else ""
    borough = normalize(location.get("borough")) if location else ""
    postcode = normalize(location.get("postcode")) if location else ""
    if not neighborhood:
        neighborhood = normalize(details.get("neighborhood"))

    if neighborhood and borough:
        lines.append(f"📍 <b>{html.escape(neighborhood)}, {html.escape(borough)}</b>")
    elif borough:
        lines.append(f"📍 <b>{html.escape(borough)}</b>")
    elif neighborhood:
        lines.append(f"📍 <b>{html.escape(neighborhood)}</b>")
    if postcode:
        lines.append(f"📮 {html.escape(postcode)}")

    if details.get("available_now"):
        lines.append("🟢 <b>AVAILABLE NOW</b>")
    if details.get("contact_phone"):
        lines.append(f"☎️ {html.escape(details['contact_phone'])}")

    lines.append(f"🕐 First detected: <b>{html.escape(format_et(first_seen))}</b>")

    if event_type == "reappeared" and last_removed_at:
        duration = human_duration(last_removed_at, utc_now_iso())
        if duration:
            lines.append(f"↩️ Reappeared after <b>{html.escape(duration)}</b>")

    maps = html.escape(google_maps_url(listing.get("address", "")), quote=True)
    detail_url = html.escape(listing.get("url") or MNS_URL, quote=True)
    archive_url = html.escape(MNS_URL, quote=True)

    lines.extend(
        [
            "",
            f'🏙️ <a href="{detail_url}">View MNS listing</a>',
            f'🗺️ <a href="{maps}">Open in Google Maps</a>',
            f'📋 <a href="{archive_url}">MNS affordable units</a>',
            "",
            "<i>Location data: © OpenStreetMap contributors</i>",
        ]
    )
    return "\n".join(lines)


def run_mns_monitor() -> None:
    state = load_mns_state()
    history = load_mns_history()

    listings = fetch_mns_with_retries(state)
    if listings is None:
        return

    if not state.get("initialized"):
        save_mns_state(listings)
        seed_mns_history(history, listings)
        save_mns_history(history)
        print(
            f"MNS initialized with {len(listings)} existing listing(s); "
            "no existing listings alerted."
        )
        return

    history_entries = history.setdefault("listings", {})
    current_by_key = {mns_listing_key(item): item for item in listings}
    current_keys = set(current_by_key)
    now = utc_now_iso()

    removed = []
    for key, entry in history_entries.items():
        if entry.get("active", False) and key not in current_keys:
            entry["active"] = False
            entry["last_removed_at"] = now
            entry["removal_count"] = int(entry.get("removal_count", 0)) + 1
            removed.append(entry)

    alerts = []
    for key, listing in current_by_key.items():
        entry = history_entries.get(key)

        if entry is None:
            details = enrich_mns_listing(listing)
            location = geocode_nyc_address(listing.get("address", ""))
            entry = {
                **listing,
                "first_seen": now,
                "first_seen_source": "observed",
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": location,
                "details": details,
            }
            history_entries[key] = entry
            alerts.append(
                {
                    "listing": listing,
                    "entry": entry,
                    "event_type": "new",
                    "last_removed_at": None,
                }
            )
            continue

        was_active = bool(entry.get("active", False))
        last_removed_at = entry.get("last_removed_at")
        for field, value in listing.items():
            entry[field] = value
        entry["last_seen"] = now
        entry["active"] = True

        if not was_active:
            details = entry.get("details") or enrich_mns_listing(listing)
            location = entry.get("location") or geocode_nyc_address(
                listing.get("address", "")
            )
            entry["details"] = details
            entry["location"] = location
            entry["appearance_count"] = int(entry.get("appearance_count", 1)) + 1
            alerts.append(
                {
                    "listing": listing,
                    "entry": entry,
                    "event_type": "reappeared",
                    "last_removed_at": last_removed_at,
                }
            )

    for event in alerts:
        entry = event["entry"]
        send_telegram(
            build_mns_notification(
                listing=event["listing"],
                details=entry.get("details") or {},
                location=entry.get("location"),
                event_type=event["event_type"],
                first_seen=entry.get("first_seen"),
                last_removed_at=event.get("last_removed_at"),
            ),
            parse_mode="HTML",
        )

    for entry in removed:
        print(
            "MNS removed: "
            f"{entry.get('address')} Apt {entry.get('unit')} "
            f"(removal #{entry.get('removal_count', 1)})"
        )

    save_mns_state(listings)
    save_mns_history(history)
    print(
        f"MNS complete: {len(alerts)} alert event(s), "
        f"{len(removed)} removal(s)."
    )





# ---------------------------------------------------------------------------
# AffordableLivingNYC / Tax Solute HPD Google Sites monitor
# ---------------------------------------------------------------------------

HPD_GOOGLE_URL = "https://sites.google.com/affordablelivingnyc.com/hpd/home"
HPD_GOOGLE_REQUEST_TIMEOUT = (2.5, 6.0)
HPD_GOOGLE_MAX_ATTEMPTS = 3
HPD_GOOGLE_RETRY_DELAYS = (0, 2, 5)
HPD_GOOGLE_STATE_PATH = Path(os.getenv("HPD_GOOGLE_STATE_PATH", "hpd_google_state.json"))
HPD_GOOGLE_HISTORY_PATH = Path(os.getenv("HPD_GOOGLE_HISTORY_PATH", "hpd_google_history.json"))
HPD_GOOGLE_USER_AGENT = "nyc-rerental-monitor/5.15 (personal affordable-housing availability notifier)"


def hpdg_norm(v):
    return re.sub(r"\s+", " ", str(v or "").replace("\u00a0", " ")).strip()


def hpdg_status(v):
    s = hpdg_norm(v).casefold()
    if "initial lease" in s or "initial occupancy" in s: return "initial lease-up"
    if "on hold" in s: return "on hold"
    if "available" in s: return "available"
    return s or "unknown"


def hpdg_active(v):
    """
    Alert all statuses except explicit On Hold.
    Unknown/unrecognized statuses are intentionally surfaced.
    """
    return hpdg_status(v) != "on hold"


def hpdg_address_like(s):
    return bool(re.search(
        r"^\d{1,5}(?:-\d{1,4})?\s+.+?\b(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Place|Pl\.?|Boulevard|Blvd\.?|Drive|Dr\.?|Lane|Ln\.?|Court|Ct\.?)\b",
        hpdg_norm(s), re.I
    ))


def hpdg_property_key(p):
    return re.sub(r"[^a-z0-9]+", " ", hpdg_norm(p.get("address")).casefold()).strip()


def hpdg_tier_key(t):
    return "||".join(hpdg_norm(t.get(k)).casefold() for k in ("unit_size","rent","household_size"))


def hpdg_money(v):
    m=re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", hpdg_norm(v))
    if not m: return hpdg_norm(v) or None
    try: return f"${float(m.group(1).replace(',','')):,.2f}"
    except ValueError: return "$"+m.group(1)


def hpdg_income(row):
    s=hpdg_norm(row)
    # Repair source typos such as $154.440.00 -> $154,440.00.
    s=re.sub(r"\$(\d{2,3})\.(\d{3})\.(\d{2})\b", r"$\1,\2.\3", s)
    amounts=re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", s)
    if len(amounts)<2: return None
    out=[]
    for a in amounts[:2]:
        try: out.append(f"${float(a.replace('$','').replace(',','').strip()):,.2f}")
        except ValueError: out.append(a.strip())
    return f"{out[0]} – {out[1]}"


def hpdg_unit_label(heading, fallback=None):
    s=hpdg_norm(heading).casefold()
    if "studio" in s: return "Studio"
    if re.search(r"\b(?:one|1)\s*(?:bed|bedroom)\b", s): return "1 Bedroom"
    if re.search(r"\b(?:two|2)\s*(?:bed|bedroom)\b", s): return "2 Bedrooms"
    if re.search(r"\b(?:three|3)\s*(?:bed|bedroom)\b", s): return "3 Bedrooms"
    raw=hpdg_norm(fallback)
    return {"0":"Studio","1":"1 Bedroom","2":"2 Bedrooms","3":"3 Bedrooms"}.get(raw, raw or "Affordable Unit")


def hpdg_parse_lines(lines):
    lines=[hpdg_norm(x) for x in lines if hpdg_norm(x)]

    # Address headings can be plain street addresses or a building name that
    # embeds the address, e.g. "The Arcadian Apartment Aka 975 Nostrand Ave."
    idxs=[]
    for i,x in enumerate(lines):
        if hpdg_address_like(x):
            idxs.append(i)
            continue
        if re.search(
            r"\b(?:aka\s+)?\d{1,5}(?:-\d{1,4})?\s+.+?\b"
            r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Place|Pl\.?|"
            r"Boulevard|Blvd\.?|Drive|Dr\.?|Lane|Ln\.?|Court|Ct\.?)\b",
            x,
            re.I,
        ):
            nearby=" ".join(lines[i+1:i+7]).casefold()
            if "unit size" in nearby or "household size" in nearby:
                idxs.append(i)

    props=[]
    for n,idx in enumerate(idxs):
        end=idxs[n+1] if n+1<len(idxs) else len(lines)
        block=lines[idx:end]
        heading=lines[idx]

        # Extract just the street address from a named-building heading.
        address=heading
        embedded=re.search(
            r"(\d{1,5}(?:-\d{1,4})?\s+.+?\b"
            r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Place|Pl\.?|"
            r"Boulevard|Blvd\.?|Drive|Dr\.?|Lane|Ln\.?|Court|Ct\.?))\b",
            heading,
            re.I,
        )
        if embedded:
            address=hpdg_norm(embedded.group(1))

        # append city/borough line when separate
        for x in block[1:6]:
            if re.search(
                r"\b(?:Brooklyn|Bronx|Queens|New York|Jamaica|Staten Island)"
                r"(?:,?\s*NY)?\b",
                x,
                re.I,
            ):
                if x.casefold() not in address.casefold():
                    address=f"{address}, {x}"
                break

        status="unknown"
        for x in reversed(lines[max(0,idx-6):idx+4]):
            low=x.casefold()
            if any(
                k in low
                for k in (
                    "unit available",
                    "units available",
                    "unit on hold",
                    "units on hold",
                    "initial lease-up",
                    "initial occupancy",
                )
            ):
                status=hpdg_status(x)
                break

        joined=" | ".join(block)
        hm=re.search(
            r"Household\s*size\s*:\s*([0-9]+(?:\s*[-–—]\s*[0-9]+)?)",
            joined,
            re.I,
        )
        household=hm.group(1) if hm else None

        um=re.search(r"Unit\s*Size\s*:\s*([^|]+)",joined,re.I)
        fallback_size=hpdg_norm(um.group(1)) if um else None

        rm=re.search(
            r"\bRent\s*:\s*(.+?)(?=\s*\|\s*INCOME|$)",
            joined,
            re.I,
        )
        rent_summary=hpdg_norm(rm.group(1)) if rm else None

        hi=[
            i for i,x in enumerate(block)
            if "income guidelines" in x.casefold()
        ]
        tiers=[]

        for j,hidx in enumerate(hi):
            sec=block[
                hidx+1:(hi[j+1] if j+1<len(hi) else len(block))
            ]
            heading_text=block[hidx]
            size=hpdg_unit_label(
                heading_text,
                fallback_size if len(hi)==1 else None,
            )

            rent=None
            r=re.search(
                r"\$\s*[\d,]+(?:\.\d{1,2})?",
                heading_text,
            )
            if r:
                rent=hpdg_money(r.group(0))
            elif rent_summary and len(hi)==1:
                rent=hpdg_money(rent_summary)
            elif rent_summary and size.startswith("1 Bedroom"):
                r=re.search(
                    r"\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*"
                    r"(?:one|1)\s*bedroom",
                    rent_summary,
                    re.I,
                )
                if r:
                    rent=hpdg_money(r.group(0))
            elif rent_summary and size.startswith("2 Bedroom"):
                r=re.search(
                    r"\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*"
                    r"(?:two|2)\s*bedroom",
                    rent_summary,
                    re.I,
                )
                if r:
                    rent=hpdg_money(r.group(0))

            one=None
            two=False
            for row in sec:
                if re.search(r"\b1\s+PERSON\b",row,re.I):
                    one=hpdg_income(row)
                if re.search(
                    r"\b2\s+(?:PERSON|PEOPLE)\b",
                    row,
                    re.I,
                ):
                    two=True

            tiers.append(
                {
                    "unit_size":size,
                    "rent":rent,
                    "household_size":household,
                    "one_person_eligible": (
                        True if one else False if two else None
                    ),
                    "one_person_income":one,
                }
            )

        if not tiers:
            one=None
            two=False
            for row in block:
                if re.search(r"\b1\s+PERSON\b",row,re.I):
                    one=hpdg_income(row)
                if re.search(
                    r"\b2\s+(?:PERSON|PEOPLE)\b",
                    row,
                    re.I,
                ):
                    two=True

            tiers=[
                {
                    "unit_size":hpdg_unit_label("",fallback_size),
                    "rent":hpdg_money(rent_summary),
                    "household_size":household,
                    "one_person_eligible": (
                        True if one else False if two else None
                    ),
                    "one_person_income":one,
                }
            ]

        tiers=list({hpdg_tier_key(t):t for t in tiers}.values())
        props.append(
            {
                "source":"AffordableLivingNYC / Tax Solute",
                "address":address,
                "status":status,
                "rent_summary":rent_summary,
                "tiers":tiers,
            }
        )

    return list(
        {
            hpdg_property_key(p):p
            for p in props
            if hpdg_property_key(p)
        }.values()
    )


def hpdg_parse_page(response):
    soup=BeautifulSoup(response.text,"html.parser")
    return hpdg_parse_lines(list(soup.stripped_strings))


def hpdg_scrape():
    r=requests.get(
        HPD_GOOGLE_URL,
        headers={"User-Agent":HPD_GOOGLE_USER_AGENT},
        timeout=HPD_GOOGLE_REQUEST_TIMEOUT,
    )
    r.raise_for_status()

    props=hpdg_parse_page(r)
    parser_mode="raw HTML"

    if not props:
        print(
            "AffordableLivingNYC raw HTML produced 0 listings; "
            "falling back to rendered Chrome text."
        )

        with sync_playwright() as p:
            browser=p.chromium.launch(
                channel="chrome",
                headless=True,
            )
            page=browser.new_page(
                user_agent=HPD_GOOGLE_USER_AGENT,
                viewport={"width":1280,"height":1100},
            )

            try:
                page.goto(
                    HPD_GOOGLE_URL,
                    wait_until="domcontentloaded",
                    timeout=25000,
                )

                body_text=""
                deadline=time.time()+10
                while time.time()<deadline:
                    try:
                        body_text=page.locator("body").inner_text(
                            timeout=2000
                        )
                    except Exception:
                        body_text=""

                    low=body_text.casefold()
                    if (
                        "available affordable housing units" in low
                        and "unit size" in low
                        and "income guidelines" in low
                    ):
                        break

                    page.wait_for_timeout(350)

                props=hpdg_parse_lines(body_text.splitlines())
                parser_mode="rendered Chrome text"

                if not props:
                    try:
                        DEBUG_DIR.mkdir(parents=True,exist_ok=True)
                        (
                            DEBUG_DIR/"hpd_google_rendered_text.txt"
                        ).write_text(body_text[:100000])
                        page.screenshot(
                            path=str(
                                DEBUG_DIR/"hpd_google_rendered.png"
                            ),
                            full_page=True,
                        )
                    except Exception as exc:
                        print(
                            "Could not save AffordableLivingNYC "
                            f"debug files: {exc}"
                        )
            finally:
                browser.close()

    if not props:
        soup=BeautifulSoup(r.text,"html.parser")
        title=(
            hpdg_norm(soup.title.get_text(" ",strip=True))
            if soup.title
            else "(no title)"
        )
        raise RuntimeError(
            "AffordableLivingNYC page loaded but no listings were parsed "
            "from raw HTML or rendered Chrome text. "
            f"status={r.status_code}; bytes={len(r.content)}; "
            f"title={title!r}"
        )

    print(
        f"AffordableLivingNYC scrape: {len(props)} property listing(s); "
        f"{sum(hpdg_active(p.get('status')) for p in props)} active; "
        f"parser={parser_mode}."
    )
    return props


def hpdg_load(path, default):
    if not path.exists(): return default
    try: return json.loads(path.read_text())
    except Exception as e: raise RuntimeError(f"Could not read {path}: {e}") from e


def hpdg_save(path,data):
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False,sort_keys=True)+"\n")


def hpdg_fetch(state):
    last=None
    for attempt in range(1,HPD_GOOGLE_MAX_ATTEMPTS+1):
        delay=HPD_GOOGLE_RETRY_DELAYS[min(attempt-1,len(HPD_GOOGLE_RETRY_DELAYS)-1)]
        if delay: time.sleep(delay)
        try:
            props=hpdg_scrape()
            prev=len(state.get("properties",{})); cur=len(props)
            if prev>=8 and prev-cur>=4 and cur/prev<0.55:
                raise RuntimeError(f"Suspicious AffordableLivingNYC listing-count drop: {prev} -> {cur}")
            if attempt>1: print(f"AffordableLivingNYC recovered on attempt {attempt}/{HPD_GOOGLE_MAX_ATTEMPTS}.")
            return props
        except (requests.RequestException,RuntimeError) as e:
            last=e; print(f"AffordableLivingNYC attempt {attempt}/{HPD_GOOGLE_MAX_ATTEMPTS} failed: {type(e).__name__}: {e}",file=sys.stderr)
    print(f"AffordableLivingNYC unavailable after {HPD_GOOGLE_MAX_ATTEMPTS} attempts; preserving prior state/history. Last error: {last}",file=sys.stderr)
    return None


def hpdg_message(prop,tier,location,event,first_seen,previous_status=None,last_removed_at=None):
    priority=priority_for_location(location)
    if event=="became_available": heading="🟢 <b>HPD / TAX SOLUTE LISTING NOW AVAILABLE</b>"
    elif event=="reappeared": heading="🔄 <b>HPD / TAX SOLUTE LISTING REAPPEARED</b>"
    else: heading="🏘️ <b>NEW HPD / TAX SOLUTE LISTING</b>"
    status=hpdg_status(prop.get("status"))
    sdisplay={
        "available":"🟢 AVAILABLE",
        "initial lease-up":"🟢 INITIAL LEASE-UP",
        "on hold":"🟡 ON HOLD",
        "unknown":"⚪ STATUS UNKNOWN",
    }.get(status, f"⚪ {status.upper()}" if status else "⚪ STATUS UNKNOWN")
    lines=[heading,f"<b>{sdisplay}</b>",f"{priority['emoji']} <b>{priority['label']} · {priority['score']}/3</b>",
           f"<i>{html.escape(priority['reason'])}</i>","",f"🏠 <b>{html.escape(prop.get('address') or '')}</b>"]
    if tier.get("rent"): lines.append(f"💰 <b>{html.escape(tier['rent'])}/mo</b>")
    elif prop.get("rent_summary"): lines.append(f"💰 Rent: <b>{html.escape(prop['rent_summary'])}</b>")
    lines.append(f"🛏️ <b>{html.escape(tier.get('unit_size') or 'Affordable Unit')}</b>")
    if tier.get("one_person_eligible") is False: lines.append("👤 1-person household: <b>Not eligible</b>")
    elif tier.get("one_person_eligible") is True:
        lines.append("👤 1-person household: <b>Eligible</b>")
        if tier.get("one_person_income"): lines.append(f"💵 1-person income: <b>{html.escape(tier['one_person_income'])}</b>")
    if location:
        n=normalize(location.get("neighborhood")); b=normalize(location.get("borough")); z=normalize(location.get("postcode"))
        if n and b: lines.append(f"📍 <b>{html.escape(n)}, {html.escape(b)}</b>")
        elif b: lines.append(f"📍 <b>{html.escape(b)}</b>")
        elif n: lines.append(f"📍 <b>{html.escape(n)}</b>")
        if z: lines.append(f"📮 {html.escape(z)}")
    if event=="became_available" and previous_status:
        lines.append(f"🔁 Status: <b>{html.escape(hpdg_status(previous_status).title())} → {html.escape(status.title())}</b>")
    lines.append(f"🕐 First detected: <b>{html.escape(format_et(first_seen))}</b>")
    if event=="reappeared" and last_removed_at:
        d=human_duration(last_removed_at,utc_now_iso())
        if d: lines.append(f"↩️ Reappeared after <b>{html.escape(d)}</b>")
    maps=html.escape(google_maps_url(prop.get("address") or ""),quote=True)
    src=html.escape(HPD_GOOGLE_URL,quote=True)
    lines += [
        "",
        f'📝 <a href="{src}">Open application / listing page</a>',
        f'🗺️ <a href="{maps}">Open in Google Maps</a>',
        "",
        "<i>Source: AffordableLivingNYC / Tax Solute · "
        "Location data: © OpenStreetMap contributors</i>",
    ]
    return "\n".join(lines)


def run_hpd_google_monitor():
    state=hpdg_load(HPD_GOOGLE_STATE_PATH,{"initialized":False,"properties":{}})
    history=hpdg_load(HPD_GOOGLE_HISTORY_PATH,{"version":1,"properties":{}})
    props=hpdg_fetch(state)
    if props is None: return
    now=utc_now_iso(); hist=history.setdefault("properties",{}); current_keys={hpdg_property_key(p) for p in props}
    if not state.get("initialized"):
        for prop in props:
            key=hpdg_property_key(prop); loc=geocode_nyc_address(prop.get("address") or "")
            hist[key]={**prop,"first_seen":now,"last_seen":now,"active":True,"location":loc,"tiers":{
                hpdg_tier_key(t):{**t,"first_seen":now,"last_seen":now,"active":True,"appearance_count":1,"removal_count":0,"last_removed_at":None}
                for t in prop.get("tiers",[])}}
        history["updated_at"]=now
        state={"initialized":True,"updated_at":now,"properties":{k:{"address":v.get("address"),"status":v.get("status"),"tiers":v.get("tiers",{})} for k,v in hist.items() if v.get("active")}}
        hpdg_save(HPD_GOOGLE_HISTORY_PATH,history); hpdg_save(HPD_GOOGLE_STATE_PATH,state)
        print(f"AffordableLivingNYC initialized with {len(props)} existing listing(s); no existing listings alerted.")
        return
    alerts=[]
    for prop in props:
        key=hpdg_property_key(prop); entry=hist.get(key); prev_status=entry.get("status") if entry else None
        if entry is None:
            entry={**prop,"first_seen":now,"last_seen":now,"active":True,"location":geocode_nyc_address(prop.get("address") or ""),"tiers":{}}; hist[key]=entry
        else:
            entry.update({"address":prop.get("address"),"status":prop.get("status"),"rent_summary":prop.get("rent_summary"),"last_seen":now,"active":True})
            if not entry.get("location"): entry["location"]=geocode_nyc_address(prop.get("address") or "")
        tiers=entry.setdefault("tiers",{}); newtiers={hpdg_tier_key(t):t for t in prop.get("tiers",[])}
        for tk,ot in tiers.items():
            if ot.get("active") and tk not in newtiers:
                ot["active"]=False; ot["last_removed_at"]=now; ot["removal_count"]=int(ot.get("removal_count",0))+1
        became=prev_status is not None and not hpdg_active(prev_status) and hpdg_active(prop.get("status"))
        for tk,t in newtiers.items():
            ot=tiers.get(tk)
            if ot is None:
                ot={**t,"first_seen":now,"last_seen":now,"active":True,"appearance_count":1,"removal_count":0,"last_removed_at":None}; tiers[tk]=ot
                if hpdg_active(prop.get("status")): alerts.append((prop,ot,"new",prev_status,None,entry.get("location")))
            else:
                was=bool(ot.get("active")); removed=ot.get("last_removed_at"); ot.update(t); ot["last_seen"]=now; ot["active"]=True
                if not was and hpdg_active(prop.get("status")):
                    ot["appearance_count"]=int(ot.get("appearance_count",1))+1; alerts.append((prop,ot,"reappeared",prev_status,removed,entry.get("location")))
                elif became: alerts.append((prop,ot,"became_available",prev_status,None,entry.get("location")))
    for key,entry in hist.items():
        if entry.get("active") and key not in current_keys:
            entry["active"]=False; entry["last_removed_at"]=now
            for t in entry.get("tiers",{}).values():
                if t.get("active"): t["active"]=False; t["last_removed_at"]=now; t["removal_count"]=int(t.get("removal_count",0))+1
    for prop,t,event,prev,removed,loc in alerts:
        send_telegram(hpdg_message(prop,t,loc,event,t.get("first_seen"),prev,removed),parse_mode="HTML")
    history["updated_at"]=now
    state={"initialized":True,"updated_at":now,"properties":{k:{"address":v.get("address"),"status":v.get("status"),"tiers":v.get("tiers",{})} for k,v in hist.items() if v.get("active")}}
    hpdg_save(HPD_GOOGLE_HISTORY_PATH,history); hpdg_save(HPD_GOOGLE_STATE_PATH,state)
    print(f"AffordableLivingNYC complete: {len(alerts)} alert event(s).")


# ---------------------------------------------------------------------------
# MGNY Consulting monitor (embedded; no separate module required)
# ---------------------------------------------------------------------------

MGNY_URL = "https://mgnyconsulting.com/listings/"
MGNY_REQUEST_TIMEOUT = (2.5, 6.0)
MGNY_MAX_ATTEMPTS = 3
MGNY_RETRY_DELAYS = (0, 2, 5)
MGNY_STATE_PATH = Path(os.getenv("MGNY_STATE_PATH", "mgny_state.json"))
MGNY_HISTORY_PATH = Path(os.getenv("MGNY_HISTORY_PATH", "mgny_history.json"))
MGNY_USER_AGENT = "nyc-rerental-monitor/5.13 (personal affordable-housing availability notifier)"


def mgny_norm(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def mgny_now_iso():
    return datetime.now(timezone.utc).isoformat()


def mgny_building_key(building):
    return mgny_norm(building.get("url")).rstrip("/").casefold() or re.sub(
        r"[^a-z0-9]+", " ", mgny_norm(building.get("address")).casefold()
    ).strip()


def mgny_tier_key(tier):
    return "||".join(
        re.sub(r"\s+", " ", mgny_norm(tier.get(k)).casefold()).strip()
        for k in ("ami", "unit_size", "rent", "household_size")
    )


def mgny_money(value):
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", mgny_norm(value))
    if not m:
        return mgny_norm(value) or None
    try:
        return f"${float(m.group(1).replace(',', '')):,.2f}"
    except ValueError:
        return "$" + m.group(1)


def mgny_index_fingerprint(building):
    return json.dumps(
        {
            "income_range": building.get("income_range"),
            "units_count": building.get("units_count"),
            "url": building.get("url"),
        },
        sort_keys=True,
    )


def mgny_parse_index_page(response):
    soup = BeautifulSoup(response.text, "html.parser")
    pages = {1}
    buildings = []
    total_results = None

    text = mgny_norm(soup.get_text(" ", strip=True))
    m = re.search(r"\b(\d+)\s+results\b", text, re.I)
    if m:
        total_results = int(m.group(1))

    for a in soup.find_all("a", href=True):
        href = urljoin(response.url, a.get("href"))
        m = re.search(r"/listings/page/(\d+)/?", href, re.I)
        if m:
            pages.add(int(m.group(1)))

    for a in soup.find_all("a", href=True):
        href = urljoin(response.url, a.get("href"))
        if not re.search(r"/listing/[^/?#]+/?$", href, re.I):
            continue
        card = mgny_norm(a.get_text(" ", strip=True))
        if not card:
            continue

        income = None
        m = re.search(
            r"(\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*\$\s*[\d,]+(?:\.\d{1,2})?)",
            card,
        )
        if m:
            income = mgny_norm(m.group(1))

        units_count = None
        m = re.search(r"\b(\d+)\s+Units?\b", card, re.I)
        if m:
            units_count = int(m.group(1))

        title = None
        heading = a.find(["h2", "h3", "h4"])
        if heading:
            title = mgny_norm(heading.get_text(" ", strip=True))

        address = None
        # ZIP-bearing address before the income range.
        m = re.search(
            r"(\d[^$]+?,\s*(?:New York|Brooklyn|Bronx|Queens|Staten Island|Briarwood)[^$]*?NY\s+\d{5})",
            card,
            re.I,
        )
        if m:
            address = mgny_norm(m.group(1))
            if not title:
                pos = card.find(address)
                if pos > 0:
                    title = mgny_norm(card[:pos])

        if not title:
            title = address or href.rstrip("/").split("/")[-1].replace("-", " ").title()

        buildings.append(
            {
                "title": title,
                "address": address,
                "income_range": income,
                "units_count": units_count,
                "url": href,
            }
        )

    deduped = {mgny_building_key(b): b for b in buildings}
    return list(deduped.values()), pages, total_results


def mgny_scrape_index():
    """
    MGNY uses client-side/AJAX pagination: clicking page 2/3 does not change
    the URL. Therefore we do not probe /page/N/ or query-string variants.

    Strategy:
      1. Fetch page 1 with requests to get the reported total and a cheap first
         snapshot.
      2. Infer the expected page count from total results / page-1 card count.
      3. Open MGNY in Chrome/Playwright.
      4. Capture page 1 from the rendered DOM.
      5. Click page 2..N in-place and wait for the listing-card set to change.
      6. Merge/de-duplicate all pages and validate against the reported total.
    """
    import math

    headers = {"User-Agent": MGNY_USER_AGENT}
    first = requests.get(
        MGNY_URL,
        headers=headers,
        timeout=MGNY_REQUEST_TIMEOUT,
    )
    first.raise_for_status()

    page1_buildings, _linked_pages, total = mgny_parse_index_page(first)

    if not page1_buildings:
        soup = BeautifulSoup(first.text, "html.parser")
        title = (
            mgny_norm(soup.title.get_text(" ", strip=True))
            if soup.title
            else "(no title)"
        )
        raise RuntimeError(
            "MGNY page 1 loaded but no listings were parsed. "
            f"status={first.status_code}; bytes={len(first.content)}; "
            f"title={title!r}"
        )

    expected_pages = (
        math.ceil(total / len(page1_buildings))
        if total and page1_buildings
        else 1
    )

    print(
        f"MGNY page 1: {len(page1_buildings)} listing(s); "
        f"{total if total is not None else 'unknown'} reported total; "
        f"expecting {expected_pages} page(s)."
    )

    if expected_pages == 1:
        buildings = page1_buildings
    else:
        print(
            "MGNY uses client-side pagination; opening Chrome and clicking "
            "page controls directly."
        )

        buildings = []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome",
                headless=True,
            )
            page = browser.new_page(
                user_agent=MGNY_USER_AGENT,
                viewport={"width": 1280, "height": 1000},
            )

            try:
                page.goto(
                    MGNY_URL,
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                page.wait_for_timeout(700)

                def parse_current_page():
                    html_text = page.content()

                    class BrowserResponse:
                        text = html_text
                        url = page.url

                    parsed, _, _ = mgny_parse_index_page(BrowserResponse())
                    return parsed

                current = parse_current_page()
                if not current:
                    raise RuntimeError(
                        "MGNY Chrome page loaded but no listing cards were parsed."
                    )

                buildings.extend(current)
                previous_keys = {
                    mgny_building_key(b) for b in current
                }
                print(f"MGNY browser page 1: {len(current)} listing(s).")

                for target_page in range(2, expected_pages + 1):
                    clicked = page.evaluate(
                        """pageNum => {
                            const wanted = String(pageNum);

                            function score(el) {
                                let s = 0;
                                let node = el;
                                for (
                                    let depth = 0;
                                    node && depth < 6;
                                    depth++, node = node.parentElement
                                ) {
                                    const meta = (
                                        String(node.className || '') + ' ' +
                                        String(node.id || '') + ' ' +
                                        String(node.getAttribute?.('aria-label') || '')
                                    ).toLowerCase();

                                    if (meta.includes('pagination')) s += 50;
                                    if (meta.includes('paginate')) s += 40;
                                    if (meta.includes('pager')) s += 35;
                                    if (meta.includes('page-number')) s += 30;
                                    if (meta.includes('page')) s += 5;
                                }

                                if (el.tagName === 'BUTTON') s += 10;
                                if (el.tagName === 'A') s += 8;
                                if (el.getAttribute?.('role') === 'button') s += 8;

                                return s;
                            }

                            const candidates = Array.from(
                                document.querySelectorAll(
                                    'button, a, [role="button"], li, span'
                                )
                            ).filter(el => {
                                if ((el.textContent || '').trim() !== wanted) {
                                    return false;
                                }

                                const rect = el.getBoundingClientRect();
                                return rect.width > 0 && rect.height > 0;
                            });

                            if (!candidates.length) return false;

                            candidates.sort((a, b) => score(b) - score(a));

                            const best = candidates[0];
                            const clickable =
                                best.closest('button, a, [role="button"]') || best;

                            clickable.scrollIntoView({
                                block: 'center',
                                inline: 'center'
                            });
                            clickable.click();

                            return true;
                        }""",
                        target_page,
                    )

                    if not clicked:
                        raise RuntimeError(
                            f"Could not find/click MGNY pagination control "
                            f"for page {target_page}."
                        )

                    changed = False
                    parsed = []
                    deadline = time.time() + 10

                    while time.time() < deadline:
                        page.wait_for_timeout(300)
                        parsed = parse_current_page()
                        current_keys = {
                            mgny_building_key(b) for b in parsed
                        }

                        if parsed and current_keys != previous_keys:
                            changed = True
                            previous_keys = current_keys
                            break

                    if not changed:
                        raise RuntimeError(
                            f"MGNY clicked page {target_page}, but the "
                            "listing-card set never changed."
                        )

                    print(
                        f"MGNY browser page {target_page}: "
                        f"{len(parsed)} listing(s)."
                    )
                    buildings.extend(parsed)

            finally:
                browser.close()

    buildings = list(
        {mgny_building_key(b): b for b in buildings}.values()
    )

    if total is not None and len(buildings) + 1 < total:
        raise RuntimeError(
            f"MGNY reported {total} results but only "
            f"{len(buildings)} unique listings were captured."
        )

    print(
        f"MGNY scrape: {len(buildings)} unique building listing(s) "
        f"across {expected_pages} page(s)."
    )
    return buildings


def mgny_enrich_building(building):
    result = {"address": building.get("address"), "neighborhood": None, "tiers": []}
    r = requests.get(building["url"], headers={"User-Agent": MGNY_USER_AGENT}, timeout=MGNY_REQUEST_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = mgny_norm(soup.get_text(" ", strip=True))

    m = re.search(r"\bAddress\s+(.+?)\s+Income Range\b", text, re.I)
    if m:
        result["address"] = mgny_norm(m.group(1))
    m = re.search(r"\bNeighborhood\s+(.+?)\s+Units\b", text, re.I)
    if m:
        result["neighborhood"] = mgny_norm(m.group(1))

    current_ami = None
    # Table-based parse is much more reliable than flattened text.
    for node in soup.find_all(["h2", "h3", "h4", "h5", "p", "div", "tr"]):
        node_text = mgny_norm(node.get_text(" ", strip=True))
        ami = re.search(r"\b(\d{1,3})\s*%\s*AMI\s+Units\b", node_text, re.I)
        if ami and node.name != "tr":
            current_ami = f"{ami.group(1)}%"
            continue
        if node.name != "tr":
            continue
        cells = [mgny_norm(c.get_text(" ", strip=True)) for c in node.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        if cells[0].casefold() == "unit size":
            continue
        if not re.search(r"studio|br|bedroom", cells[0], re.I):
            continue

        # Find closest preceding AMI marker when DOM nesting resets current_ami.
        ami_value = current_ami
        prev = node.find_previous(string=re.compile(r"\b\d{1,3}\s*%\s*AMI\s+Units\b", re.I))
        if prev:
            mm = re.search(r"(\d{1,3})\s*%", str(prev), re.I)
            if mm:
                ami_value = f"{mm.group(1)}%"
        if not ami_value:
            continue

        count_match = re.search(r"\d+", cells[2])
        available = int(count_match.group()) if count_match else 0
        if available <= 0:
            continue
        result["tiers"].append(
            {
                "ami": ami_value,
                "unit_size": cells[0],
                "rent": mgny_money(cells[1]),
                "units_available": available,
                "household_size": cells[3],
                "annual_income": cells[4],
            }
        )

    # Text fallback for pages where the rows aren't semantic TR elements.
    if not result["tiers"]:
        for ami_match in re.finditer(r"\b(\d{1,3})\s*%\s*AMI\s+Units\b", text, re.I):
            section = text[ami_match.end():]
            next_ami = re.search(r"\b\d{1,3}\s*%\s*AMI\s+Units\b", section, re.I)
            if next_ami:
                section = section[:next_ami.start()]
            pattern = re.compile(
                r"\b(Studio|\d+\s*-?\s*BR|\d+\s*Bedroom(?:s)?)\s*\|?\s*"
                r"(\$\s*[\d,]+(?:\.\d{1,2})?)\s*\|?\s*(\d+)\s*\|?\s*"
                r"(\d+\s*-\s*\d+\s*people|\d+\s*people)\s*\|?\s*"
                r"(\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*\$\s*[\d,]+(?:\.\d{1,2})?)",
                re.I,
            )
            for m in pattern.finditer(section):
                if int(m.group(3)) <= 0:
                    continue
                result["tiers"].append(
                    {
                        "ami": f"{ami_match.group(1)}%",
                        "unit_size": mgny_norm(m.group(1)),
                        "rent": mgny_money(m.group(2)),
                        "units_available": int(m.group(3)),
                        "household_size": mgny_norm(m.group(4)),
                        "annual_income": mgny_norm(m.group(5)),
                    }
                )

    result["tiers"] = list({mgny_tier_key(t): t for t in result["tiers"]}.values())
    return result


def mgny_one_person(tier):
    nums = [int(x) for x in re.findall(r"\d+", mgny_norm(tier.get("household_size")))]
    if not nums:
        return None, None
    eligible = nums[0] <= 1
    return eligible, tier.get("annual_income") if eligible else None


def mgny_load_json(path, default):
    if not path.exists():
        return default
    data = json.loads(path.read_text())
    return data


def mgny_save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def mgny_fetch_with_retries(state):
    last = None
    for attempt in range(1, MGNY_MAX_ATTEMPTS + 1):
        delay = MGNY_RETRY_DELAYS[attempt - 1]
        if delay:
            print(f"MGNY retry {attempt}/{MGNY_MAX_ATTEMPTS}: waiting {delay}s...")
            time.sleep(delay)
        try:
            current = mgny_scrape_index()
            previous = len(state.get("buildings", {}))
            if previous >= 8 and len(current) < previous * 0.55 and previous - len(current) >= 5:
                raise RuntimeError(f"Suspicious MGNY count drop: {previous} -> {len(current)}")
            return current
        except (requests.RequestException, RuntimeError) as exc:
            last = exc
            print(f"MGNY attempt {attempt}/{MGNY_MAX_ATTEMPTS} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"MGNY unavailable after {MGNY_MAX_ATTEMPTS} attempts; preserving previous state. Last error: {last}", file=sys.stderr)
    return None


def mgny_notification(building, tier, location, priority_fn, maps_fn, format_et_fn, event, first_seen, previous_units=None):
    priority = priority_fn(location)
    eligible, income = mgny_one_person(tier)
    if event == "reappeared":
        heading = "🔄 <b>MGNY AFFORDABLE TIER REAPPEARED</b>"
    elif event == "availability_increased":
        heading = "📈 <b>MGNY AVAILABILITY INCREASED</b>"
    else:
        heading = "🏗️ <b>NEW MGNY AFFORDABLE LISTING</b>"

    lines = [
        heading,
        f"{priority['emoji']} <b>{priority['label']} · {priority['score']}/3</b>",
        f"<i>{html.escape(priority['reason'])}</i>",
        "",
        f"🏠 <b>{html.escape(mgny_norm(building.get('title')))}</b>",
    ]
    if building.get("address"):
        lines.append(f"📫 {html.escape(building['address'])}")
    if tier.get("rent"):
        lines.append(f"💰 <b>{html.escape(tier['rent'])}/mo</b>")
    if tier.get("unit_size"):
        lines.append(f"🛏️ <b>{html.escape(tier['unit_size'])}</b>")
    if tier.get("ami"):
        lines.append(f"📊 AMI: <b>{html.escape(tier['ami'])}</b>")
    if tier.get("units_available") is not None:
        lines.append(f"🏢 Units available in tier: <b>{tier['units_available']}</b>")
    if event == "availability_increased" and previous_units is not None:
        lines.append(f"↗️ Increased from <b>{previous_units}</b>")
    if eligible is False:
        lines.append("👤 1-person household: <b>Not eligible</b>")
    elif eligible is True:
        lines.append("👤 1-person household: <b>Eligible</b>")
        if income:
            lines.append(f"💵 Published applicable income range: <b>{html.escape(income)}</b>")

    if location:
        n = mgny_norm(location.get("neighborhood"))
        b = mgny_norm(location.get("borough"))
        z = mgny_norm(location.get("postcode"))
        if n and b:
            lines.append(f"📍 <b>{html.escape(n)}, {html.escape(b)}</b>")
        elif b:
            lines.append(f"📍 <b>{html.escape(b)}</b>")
        elif n:
            lines.append(f"📍 <b>{html.escape(n)}</b>")
        if z:
            lines.append(f"📮 {html.escape(z)}")

    lines.append(f"🕐 First detected: <b>{html.escape(format_et_fn(first_seen))}</b>")
    maps = html.escape(maps_fn(building.get("address") or building.get("title") or ""), quote=True)
    detail = html.escape(building.get("url") or MGNY_URL, quote=True)
    index = html.escape(MGNY_URL, quote=True)
    lines += [
        "",
        f'🏗️ <a href="{detail}">View MGNY listing</a>',
        f'🗺️ <a href="{maps}">Open in Google Maps</a>',
        f'📋 <a href="{index}">MGNY listings</a>',
        "",
        "<i>Location data: © OpenStreetMap contributors</i>",
    ]
    return "\n".join(lines)


def run_mgny_monitor(send_telegram, geocode_fn, priority_fn, maps_fn, utc_now_fn, format_et_fn):
    state = mgny_load_json(MGNY_STATE_PATH, {"initialized": False, "buildings": {}})
    history = mgny_load_json(MGNY_HISTORY_PATH, {"version": 1, "buildings": {}})
    history.setdefault("buildings", {})
    buildings = mgny_fetch_with_retries(state)
    if buildings is None:
        return
    now = utc_now_fn()

    # Silent first run, but make it tier-aware once.
    if not state.get("initialized"):
        print(f"MGNY first run: building silent tier baseline for {len(buildings)} building(s).")
        def enrich_one(b):
            try:
                return b, mgny_enrich_building(b)
            except Exception as exc:
                print(f"MGNY baseline detail failed for {b.get('title')}: {exc}", file=sys.stderr)
                return b, {"address": b.get("address"), "neighborhood": None, "tiers": []}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(buildings)))) as ex:
            futures = [ex.submit(enrich_one, b) for b in buildings]
            for fut in as_completed(futures):
                b, detail = fut.result()
                key = mgny_building_key(b)
                address = detail.get("address") or b.get("address") or b.get("title")
                location = geocode_fn(address) if address else None
                tiers = {
                    mgny_tier_key(t): {**t, "first_seen": now, "last_seen": now, "active": True, "appearance_count": 1, "removal_count": 0, "last_removed_at": None}
                    for t in detail.get("tiers", [])
                }
                history["buildings"][key] = {
                    **b,
                    "address": detail.get("address") or b.get("address"),
                    "index_fingerprint": mgny_index_fingerprint(b),
                    "first_seen": now,
                    "last_seen": now,
                    "active": True,
                    "location": location,
                    "current_tiers": tiers,
                }
        state = {
            "initialized": True,
            "updated_at": now,
            "buildings": {
                mgny_building_key(b): {
                    **b,
                    "tiers": history["buildings"].get(mgny_building_key(b), {}).get("current_tiers", {}),
                }
                for b in buildings
            },
        }
        history["updated_at"] = now
        mgny_save_json(MGNY_STATE_PATH, state)
        mgny_save_json(MGNY_HISTORY_PATH, history)
        print(f"MGNY initialized with {len(buildings)} existing building(s); no alerts sent.")
        return

    old_state = state.get("buildings", {})
    h = history["buildings"]
    current_keys = {mgny_building_key(b) for b in buildings}
    alerts = []

    for b in buildings:
        key = mgny_building_key(b)
        entry = h.get(key)
        was_active = bool(entry.get("active")) if entry else False
        fingerprint = mgny_index_fingerprint(b)
        should_enrich = entry is None or not was_active or entry.get("index_fingerprint") != fingerprint

        if not should_enrich:
            entry.update(b)
            entry["active"] = True
            entry["last_seen"] = now
            continue

        try:
            detail = mgny_enrich_building(b)
        except Exception as exc:
            print(f"MGNY detail failed for {b.get('title')}: {exc}; preserving prior tiers.", file=sys.stderr)
            if entry:
                entry.update(b)
                entry["active"] = True
                entry["last_seen"] = now
            continue

        if entry is None:
            address = detail.get("address") or b.get("address") or b.get("title")
            entry = {
                **b,
                "address": detail.get("address") or b.get("address"),
                "index_fingerprint": fingerprint,
                "first_seen": now,
                "last_seen": now,
                "active": True,
                "location": geocode_fn(address) if address else None,
                "current_tiers": {},
            }
            h[key] = entry
        else:
            entry.update(b)
            if detail.get("address"):
                entry["address"] = detail["address"]
            entry["index_fingerprint"] = fingerprint
            entry["active"] = True
            entry["last_seen"] = now
            if not entry.get("location"):
                q = entry.get("address") or entry.get("title")
                entry["location"] = geocode_fn(q) if q else None

        old_tiers = entry.setdefault("current_tiers", {})
        new_tiers = {mgny_tier_key(t): t for t in detail.get("tiers", [])}
        if not new_tiers and old_tiers:
            print(f"MGNY parsed 0 tiers for {b.get('title')}; preserving previous tier state.")
            continue

        for tk, ot in old_tiers.items():
            if ot.get("active") and tk not in new_tiers:
                ot["active"] = False
                ot["last_removed_at"] = now
                ot["removal_count"] = int(ot.get("removal_count", 0)) + 1

        for tk, tier in new_tiers.items():
            old = old_tiers.get(tk)
            if old is None:
                old = {**tier, "first_seen": now, "last_seen": now, "active": True, "appearance_count": 1, "removal_count": 0, "last_removed_at": None}
                old_tiers[tk] = old
                alerts.append((b, old, entry.get("location"), "new", None))
            else:
                prev_active = bool(old.get("active"))
                prev_units = old.get("units_available")
                old.update(tier)
                old["active"] = True
                old["last_seen"] = now
                if not prev_active:
                    old["appearance_count"] = int(old.get("appearance_count", 1)) + 1
                    alerts.append((b, old, entry.get("location"), "reappeared", prev_units))
                elif isinstance(prev_units, int) and isinstance(tier.get("units_available"), int) and tier["units_available"] > prev_units:
                    alerts.append((b, old, entry.get("location"), "availability_increased", prev_units))

    for key, entry in h.items():
        if entry.get("active") and key not in current_keys:
            entry["active"] = False
            entry["last_removed_at"] = now
            for tier in entry.get("current_tiers", {}).values():
                if tier.get("active"):
                    tier["active"] = False
                    tier["last_removed_at"] = now
                    tier["removal_count"] = int(tier.get("removal_count", 0)) + 1

    for b, tier, location, event, previous_units in alerts:
        send_telegram(
            mgny_notification(b, tier, location, priority_fn, maps_fn, format_et_fn, event, tier.get("first_seen"), previous_units),
            parse_mode="HTML",
        )

    state = {
        "initialized": True,
        "updated_at": now,
        "buildings": {
            mgny_building_key(b): {
                **b,
                "tiers": h.get(mgny_building_key(b), {}).get("current_tiers", {}),
            }
            for b in buildings
        },
    }
    history["updated_at"] = now
    mgny_save_json(MGNY_STATE_PATH, state)
    mgny_save_json(MGNY_HISTORY_PATH, history)
    print(f"MGNY complete: {len(alerts)} alert event(s).")


# ---------------------------------------------------------------------------
# Taxace NY Available Units monitor
# ---------------------------------------------------------------------------

TAXACE_URL = "https://www.taxaceny.com/projects-8"
TAXACE_REQUEST_TIMEOUT = (2.5, 6.0)
TAXACE_MAX_ATTEMPTS = 3
TAXACE_RETRY_DELAYS = (0, 2, 5)
TAXACE_STATE_PATH = Path(os.getenv("TAXACE_STATE_PATH", "taxace_state.json"))
TAXACE_HISTORY_PATH = Path(os.getenv("TAXACE_HISTORY_PATH", "taxace_history.json"))
TAXACE_USER_AGENT = "nyc-rerental-monitor/5.18 (personal affordable-housing availability notifier)"


def taxace_norm(v):
    return re.sub(r"\s+", " ", str(v or "").replace("\u00a0", " ")).strip()


def taxace_key(item):
    address = re.sub(r"[^a-z0-9]+", " ", taxace_norm(item.get("address")).casefold()).strip()
    unit = re.sub(r"[^a-z0-9]+", " ", taxace_norm(item.get("unit")).casefold()).strip()
    return f"{address}||{unit}"


def taxace_money(v):
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", taxace_norm(v))
    if not m:
        return taxace_norm(v) or None
    try:
        return f"${float(m.group(1).replace(',', '')):,.2f}"
    except ValueError:
        return "$" + m.group(1)


def taxace_income(v):
    amounts = re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", taxace_norm(v))
    if len(amounts) >= 2:
        return f"{taxace_money(amounts[0])} – {taxace_money(amounts[1])}"
    return taxace_norm(v) or None


def taxace_parse_page(response):
    soup = BeautifulSoup(response.text, "html.parser")
    listings = []

    # Center each card on its unique ClickUp Apply link.
    for apply_link in soup.find_all("a", href=True):
        href = apply_link.get("href") or ""
        if "forms.clickup.com" not in href:
            continue
        if "apply" not in taxace_norm(apply_link.get_text(" ", strip=True)).casefold():
            continue

        chosen = None
        node = apply_link
        for _ in range(10):
            node = node.parent
            if node is None:
                break
            block_text = taxace_norm(node.get_text(" | ", strip=True))
            low = block_text.casefold()
            if "unit size:" in low and "rent:" in low and "min income:" in low:
                chosen = node
                clickup_links = [
                    a for a in node.find_all("a", href=True)
                    if "forms.clickup.com" in (a.get("href") or "")
                ]
                if len(clickup_links) == 1:
                    break

        if chosen is None:
            continue

        block = taxace_norm(chosen.get_text(" | ", strip=True))

        addr_match = re.search(
            r"(\d{1,5}(?:-\d{1,4})?\s+.+?)\.\s*Unit\s+([A-Za-z0-9-]+)\b",
            block,
            re.I,
        )
        if not addr_match:
            continue

        street_address = taxace_norm(addr_match.group(1))
        unit = taxace_norm(addr_match.group(2))

        borough_match = re.search(
            r"\b(Bronx|Brooklyn|Queens|New York|Manhattan|Staten Island),?\s*NY\b",
            block,
            re.I,
        )
        borough = borough_match.group(1).title() if borough_match else None
        full_address = (
            f"{street_address}, {borough}, NY"
            if borough else street_address
        )

        size_match = re.search(r"Unit\s*Size\s*:\s*([^|]+)", block, re.I)
        rent_match = re.search(
            r"\bRent\s*:\s*(\$\s*[\d,]+(?:\.\d{1,2})?)",
            block,
            re.I,
        )
        utilities_match = re.search(r"Utilities\s*:\s*([^|]+)", block, re.I)
        elevator_match = re.search(r"Elevator\s*Building\s*:\s*([^|]+)", block, re.I)
        income_match = re.search(
            r"Min\s*Income\s*:\s*(\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*\$\s*[\d,]+(?:\.\d{1,2})?)",
            block,
            re.I,
        )

        listings.append(
            {
                "source": "Taxace NY",
                "address": full_address,
                "street_address": street_address,
                "unit": unit,
                "borough": borough,
                "unit_size": taxace_norm(size_match.group(1)) if size_match else None,
                "rent": taxace_money(rent_match.group(1)) if rent_match else None,
                "utilities": taxace_norm(utilities_match.group(1)) if utilities_match else None,
                "elevator": taxace_norm(elevator_match.group(1)) if elevator_match else None,
                "income_range": taxace_income(income_match.group(1)) if income_match else None,
                "apply_url": href,
                "source_url": TAXACE_URL,
            }
        )

    listings = list({taxace_key(x): x for x in listings}.values())

    if not listings:
        title = taxace_norm(soup.title.get_text(" ", strip=True)) if soup.title else "(no title)"
        raise RuntimeError(
            "Taxace page loaded but no live units were parsed. "
            f"status={getattr(response, 'status_code', 200)}; "
            f"bytes={len(getattr(response, 'content', b''))}; title={title!r}"
        )

    print(f"Taxace scrape: {len(listings)} live unit(s).")
    return listings


def taxace_scrape():
    r = requests.get(
        TAXACE_URL,
        headers={"User-Agent": TAXACE_USER_AGENT},
        timeout=TAXACE_REQUEST_TIMEOUT,
    )
    r.raise_for_status()

    try:
        return taxace_parse_page(r)
    except RuntimeError:
        print("Taxace raw HTML parser found no units; falling back to rendered Chrome DOM.")

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(
                user_agent=TAXACE_USER_AGENT,
                viewport={"width": 1280, "height": 1100},
            )
            try:
                page.goto(TAXACE_URL, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1000)
                html_text = page.content()

                class RenderedResponse:
                    text = html_text
                    status_code = 200
                    content = html_text.encode("utf-8")

                return taxace_parse_page(RenderedResponse())
            finally:
                browser.close()


def taxace_load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc


def taxace_save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def taxace_validate(listings, state):
    current = len(listings)
    previous = len(state.get("listings", {}))

    if current == 0:
        raise IncompleteScrapeError("Taxace captured 0 units. Previous baseline preserved.")

    if previous:
        ratio = current / previous
        drop = previous - current
        if previous >= 8 and drop >= 5 and ratio < 0.45:
            raise IncompleteScrapeError(
                f"Suspicious Taxace listing-count drop: {previous} -> {current}. "
                "Previous baseline preserved."
            )


def taxace_fetch(state):
    last_error = None

    for attempt in range(1, TAXACE_MAX_ATTEMPTS + 1):
        delay = TAXACE_RETRY_DELAYS[min(attempt - 1, len(TAXACE_RETRY_DELAYS) - 1)]
        if delay:
            print(f"Taxace retry {attempt}/{TAXACE_MAX_ATTEMPTS}: waiting {delay}s...")
            time.sleep(delay)

        try:
            listings = taxace_scrape()
            taxace_validate(listings, state)
            if attempt > 1:
                print(f"Taxace recovered on attempt {attempt}/{TAXACE_MAX_ATTEMPTS}.")
            return listings
        except (requests.RequestException, IncompleteScrapeError, RuntimeError) as exc:
            last_error = exc
            print(
                f"Taxace attempt {attempt}/{TAXACE_MAX_ATTEMPTS} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    print(
        f"Taxace unavailable after {TAXACE_MAX_ATTEMPTS} attempts; "
        "preserving prior state/history. "
        f"Last error: {last_error}",
        file=sys.stderr,
    )
    return None


def taxace_message(listing, location, event, first_seen, last_removed_at=None):
    priority = priority_for_location(location)
    heading = (
        "🔄 <b>TAXACE LISTING REAPPEARED</b>"
        if event == "reappeared"
        else "🏢 <b>NEW TAXACE AFFORDABLE LISTING</b>"
    )

    lines = [
        heading,
        f"{priority['emoji']} <b>{priority['label']} · {priority['score']}/3</b>",
        f"<i>{html.escape(priority['reason'])}</i>",
        "",
        f"🏠 <b>{html.escape(listing.get('street_address') or '')} "
        f"— Unit {html.escape(listing.get('unit') or '')}</b>",
    ]

    if listing.get("rent"):
        lines.append(f"💰 <b>{html.escape(listing['rent'])}/mo</b>")
    if listing.get("unit_size"):
        lines.append(f"🛏️ <b>{html.escape(listing['unit_size'])}</b>")
    if listing.get("income_range"):
        lines.append(
            "💵 Published income range: "
            f"<b>{html.escape(listing['income_range'])}</b>"
        )
    if listing.get("utilities"):
        lines.append(f"⚡ Utilities: <b>{html.escape(listing['utilities'])}</b>")
    if listing.get("elevator"):
        lines.append(f"🛗 Elevator: <b>{html.escape(listing['elevator'])}</b>")

    if location:
        n = normalize(location.get("neighborhood"))
        b = normalize(location.get("borough"))
        z = normalize(location.get("postcode"))
        if n and b:
            lines.append(f"📍 <b>{html.escape(n)}, {html.escape(b)}</b>")
        elif b:
            lines.append(f"📍 <b>{html.escape(b)}</b>")
        elif n:
            lines.append(f"📍 <b>{html.escape(n)}</b>")
        if z:
            lines.append(f"📮 {html.escape(z)}")

    lines.append(f"🕐 First detected: <b>{html.escape(format_et(first_seen))}</b>")

    if event == "reappeared" and last_removed_at:
        duration = human_duration(last_removed_at, utc_now_iso())
        if duration:
            lines.append(f"↩️ Reappeared after <b>{html.escape(duration)}</b>")

    maps = html.escape(google_maps_url(listing.get("address") or ""), quote=True)
    apply_url = html.escape(listing.get("apply_url") or TAXACE_URL, quote=True)
    source_url = html.escape(TAXACE_URL, quote=True)

    lines += [
        "",
        f'📝 <a href="{apply_url}"><b>Apply now</b></a>',
        f'🗺️ <a href="{maps}">Open in Google Maps</a>',
        f'📋 <a href="{source_url}">Taxace available units page</a>',
        "",
        "<i>Location data: © OpenStreetMap contributors</i>",
    ]
    return "\n".join(lines)


def run_taxace_monitor():
    state = taxace_load(TAXACE_STATE_PATH, {"initialized": False, "listings": {}})
    history = taxace_load(
        TAXACE_HISTORY_PATH,
        {"version": 1, "created_at": utc_now_iso(), "listings": {}},
    )

    listings = taxace_fetch(state)
    if listings is None:
        return

    now = utc_now_iso()
    entries = history.setdefault("listings", {})
    current = {taxace_key(x): x for x in listings}
    current_keys = set(current)

    if not state.get("initialized"):
        for key, listing in current.items():
            entries[key] = {
                **listing,
                "first_seen": now,
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": None,
            }

        history["updated_at"] = now
        state = {
            "initialized": True,
            "updated_at": now,
            "listings": {
                key: {
                    "address": listing.get("address"),
                    "unit": listing.get("unit"),
                    "rent": listing.get("rent"),
                    "apply_url": listing.get("apply_url"),
                }
                for key, listing in current.items()
            },
        }
        taxace_save(TAXACE_HISTORY_PATH, history)
        taxace_save(TAXACE_STATE_PATH, state)
        print(
            f"Taxace initialized with {len(listings)} existing unit(s); "
            "no existing units alerted."
        )
        return

    alerts = []
    removed = []

    for key, entry in entries.items():
        if entry.get("active") and key not in current_keys:
            entry["active"] = False
            entry["last_removed_at"] = now
            entry["removal_count"] = int(entry.get("removal_count", 0)) + 1
            removed.append(entry)

    for key, listing in current.items():
        entry = entries.get(key)

        if entry is None:
            location = geocode_nyc_address(listing.get("address") or "")
            entry = {
                **listing,
                "first_seen": now,
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": location,
            }
            entries[key] = entry
            alerts.append((listing, entry, "new", None))
            continue

        was_active = bool(entry.get("active"))
        last_removed_at = entry.get("last_removed_at")
        # Refresh display metadata every run. This also repairs older
        # v5.19 state/history entries whose title was "Read More -->".
        entry.update(listing)
        entry["last_seen"] = now
        entry["active"] = True

        if not was_active:
            if not entry.get("location"):
                entry["location"] = geocode_nyc_address(listing.get("address") or "")
            entry["appearance_count"] = int(entry.get("appearance_count", 1)) + 1
            alerts.append((listing, entry, "reappeared", last_removed_at))

    for listing, entry, event, last_removed_at in alerts:
        send_telegram(
            taxace_message(
                listing,
                entry.get("location"),
                event,
                entry.get("first_seen"),
                last_removed_at,
            ),
            parse_mode="HTML",
        )

    for entry in removed:
        print(
            f"Taxace removed: {entry.get('street_address')} "
            f"Unit {entry.get('unit')} "
            f"(removal #{entry.get('removal_count', 1)})"
        )

    history["updated_at"] = now
    state = {
        "initialized": True,
        "updated_at": now,
        "listings": {
            key: {
                "address": listing.get("address"),
                "unit": listing.get("unit"),
                "rent": listing.get("rent"),
                "apply_url": listing.get("apply_url"),
            }
            for key, listing in current.items()
        },
    }

    taxace_save(TAXACE_HISTORY_PATH, history)
    taxace_save(TAXACE_STATE_PATH, state)

    print(
        f"Taxace complete: {len(alerts)} alert event(s), "
        f"{len(removed)} removal(s)."
    )




# ---------------------------------------------------------------------------
# SJP Tax Consultants affordable re-rentals monitor
# ---------------------------------------------------------------------------

SJP_URL = "https://www.sjpny.com/affordable-rerentals"
SJP_REQUEST_TIMEOUT = (2.5, 6.0)
SJP_MAX_ATTEMPTS = 3
SJP_RETRY_DELAYS = (0, 2, 5)
SJP_STATE_PATH = Path(os.getenv("SJP_STATE_PATH", "sjp_state.json"))
SJP_HISTORY_PATH = Path(os.getenv("SJP_HISTORY_PATH", "sjp_history.json"))
SJP_USER_AGENT = "nyc-rerental-monitor/5.19 (personal affordable-housing availability notifier)"


def sjp_norm(v):
    return re.sub(r"\s+", " ", str(v or "").replace("\u00a0", " ")).strip()


def sjp_key(item):
    # SJP detail URLs are the most stable identity available because some
    # listings use cross streets instead of a full street address.
    url = sjp_norm(item.get("url")).rstrip("/").casefold()
    if url:
        return url
    title = re.sub(r"[^a-z0-9]+", " ", sjp_norm(item.get("title")).casefold()).strip()
    return title


def sjp_money(v):
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", sjp_norm(v))
    if not m:
        return sjp_norm(v) or None
    try:
        return f"${float(m.group(1).replace(',', '')):,.2f}"
    except ValueError:
        return "$" + m.group(1)


def sjp_income(v):
    amounts = re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", sjp_norm(v))
    if len(amounts) >= 2:
        return f"{sjp_money(amounts[0])} – {sjp_money(amounts[1])}"
    return sjp_norm(v) or None


def sjp_status(v):
    s = sjp_norm(v).casefold()
    if "rented" in s or "waitlist met" in s:
        return "rented / waitlist met"
    if "available" in s:
        return "available"
    return s or "unknown"


def sjp_parse_index(response):
    soup = BeautifulSoup(response.text, "html.parser")
    listings = []

    # SJP's card CTA text is typically "Read More -->", so the anchor text is
    # not the listing title. Use each stable /apartment-listings/<slug> URL as
    # the card identity, then derive the human-readable title from nearby card
    # heading/text instead.
    for a in soup.find_all("a", href=True):
        href = urljoin(SJP_URL, a.get("href"))
        if not re.search(r"/apartment-listings/[^/?#]+/?$", href, re.I):
            continue

        anchor_text = sjp_norm(a.get_text(" ", strip=True))
        if anchor_text.casefold().startswith(("previous ", "next ")):
            continue

        title = None

        # Prefer a nearby heading within the same card/container.
        node = a
        for _ in range(7):
            node = node.parent
            if node is None:
                break

            headings = node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
            candidates = []
            for h in headings:
                t = sjp_norm(h.get_text(" ", strip=True))
                if not t:
                    continue
                low = t.casefold()
                if low.startswith(("read more", "apply now", "previous ", "next ")):
                    continue
                candidates.append(t)

            if candidates:
                # Prefer wording that looks like the actual listing title.
                preferred = next(
                    (
                        t for t in candidates
                        if any(
                            k in t.casefold()
                            for k in (
                                "available",
                                "re-rental",
                                "rerental",
                                "apartment",
                                "studio",
                                "bedroom",
                            )
                        )
                    ),
                    None,
                )
                title = preferred or candidates[0]
                break

        # Fallback: inspect nearby text siblings before the CTA.
        if not title:
            nearby = []
            parent = a.parent
            if parent is not None:
                for sibling in list(parent.previous_siblings)[:8]:
                    try:
                        t = sjp_norm(
                            sibling.get_text(" ", strip=True)
                            if hasattr(sibling, "get_text")
                            else str(sibling)
                        )
                    except Exception:
                        t = ""
                    if t:
                        nearby.append(t)

            for t in nearby:
                low = t.casefold()
                if low.startswith(("read more", "apply now")):
                    continue
                if len(t) >= 8:
                    title = t
                    break

        # Last resort: build a readable title from the slug instead of storing
        # the useless CTA text.
        if not title:
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            slug = re.sub(r"-\d+$", "", slug)
            title = " ".join(
                word.capitalize()
                for word in slug.replace("-", " ").split()
            )

        listings.append(
            {
                "source": "SJP Tax Consultants",
                "title": title,
                "url": href,
            }
        )

    listings = list({sjp_key(x): x for x in listings}.values())

    if not listings:
        title = sjp_norm(soup.title.get_text(" ", strip=True)) if soup.title else "(no title)"
        raise RuntimeError(
            "SJP affordable re-rentals page loaded but no listing cards were parsed. "
            f"status={response.status_code}; bytes={len(response.content)}; title={title!r}"
        )

    print(
        f"SJP index scrape: {len(listings)} listing card(s); "
        f"titles={[x.get('title') for x in listings[:3]]}"
    )
    return listings


def sjp_scrape_index():
    r = requests.get(
        SJP_URL,
        headers={"User-Agent": SJP_USER_AGENT},
        timeout=SJP_REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return sjp_parse_index(r)


def sjp_enrich_listing(listing):
    details = {
        "status": "unknown",
        "location_label": None,
        "unit_size": None,
        "household_size": None,
        "rent": None,
        "ami": None,
        "one_person_eligible": None,
        "one_person_income": None,
        "utilities": None,
        "apply_url": None,
    }

    url = listing.get("url")
    if not url:
        return details

    r = requests.get(
        url,
        headers={"User-Agent": SJP_USER_AGENT},
        timeout=SJP_REQUEST_TIMEOUT,
    )
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    page_text = sjp_norm(soup.get_text(" ", strip=True))

    # Status is exposed as a tag/text near the Apply button.
    if re.search(r"Rented\s*/\s*Waitlist\s*Met", page_text, re.I):
        details["status"] = "rented / waitlist met"
    elif re.search(r"\bavailable\b", page_text, re.I):
        details["status"] = "available"

    # Subtitle, e.g. "(Studio - Newtown and 32 Street - 3rd Fl)".
    heading = soup.find(["h2", "h3"], string=re.compile(r"\(.+\)"))
    if heading:
        details["location_label"] = sjp_norm(heading.get_text(" ", strip=True)).strip("()")

    size_match = re.search(
        r"Bedroom\s*Size\s*-\s*(.+?)(?=\s+Household\s*Size|\s+\*?Current\s+Legal\s+Rent|\s+Approximate\s+Income|$)",
        page_text,
        re.I,
    )
    if size_match:
        details["unit_size"] = sjp_norm(size_match.group(1))

    household_match = re.search(
        r"Household\s*Size\s*-\s*([0-9]+\s*[-–—]\s*[0-9]+\s*People|[0-9]+\s*People)",
        page_text,
        re.I,
    )
    if household_match:
        details["household_size"] = sjp_norm(household_match.group(1))

    rent_match = re.search(
        r"Current\s+Legal\s+Rent\s+Amount\s*-\s*\*?\s*(\$\s*[\d,]+(?:\.\d{1,2})?)",
        page_text,
        re.I,
    )
    if rent_match:
        details["rent"] = sjp_money(rent_match.group(1))

    ami_match = re.search(
        r"Approximate\s+Income\s+Limits\s*\(\s*(\d{1,3})\s*%\s*AMI\s*\)",
        page_text,
        re.I,
    )
    if ami_match:
        details["ami"] = f"{ami_match.group(1)}%"

    one_match = re.search(
        r"\b1\s+Person\s*-\s*"
        r"(\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*"
        r"\$\s*[\d,]+(?:\.\d{1,2})?)",
        page_text,
        re.I,
    )
    if one_match:
        details["one_person_eligible"] = True
        details["one_person_income"] = sjp_income(one_match.group(1))
    else:
        # If the published household range explicitly begins at 2+ and there
        # is no 1-person row, mark one-person ineligible.
        nums = [int(x) for x in re.findall(r"\d+", details.get("household_size") or "")]
        if nums and nums[0] >= 2:
            details["one_person_eligible"] = False

    utilities_match = re.search(
        r"Utilities\s*:\s*(.+?)(?=\s+\*?Rent amount subject|\s+Rent amount subject|$)",
        page_text,
        re.I,
    )
    if utilities_match:
        details["utilities"] = sjp_norm(utilities_match.group(1))
    else:
        # Some pages omit "Utilities:" but still state tenant/landlord utility responsibility.
        utility_sentence = re.search(
            r"((?:Tenant|Landlord)\s+pays?.+?(?=\s+\*?Rent amount subject|\s+Rent amount subject|$))",
            page_text,
            re.I,
        )
        if utility_sentence:
            details["utilities"] = sjp_norm(utility_sentence.group(1))

    apply = soup.find(
        "a",
        href=True,
        string=re.compile(r"Apply\s*now", re.I),
    )
    if apply:
        details["apply_url"] = urljoin(url, apply.get("href"))

    return details


def sjp_load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc


def sjp_save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def sjp_validate(listings, state):
    current = len(listings)
    previous = len(state.get("listings", {}))

    if current == 0:
        raise IncompleteScrapeError("SJP captured 0 listing cards. Previous baseline preserved.")

    if previous:
        ratio = current / previous
        drop = previous - current
        if previous >= 6 and drop >= 4 and ratio < 0.45:
            raise IncompleteScrapeError(
                f"Suspicious SJP listing-count drop: {previous} -> {current}. "
                "Previous baseline preserved."
            )


def sjp_fetch(state):
    last_error = None

    for attempt in range(1, SJP_MAX_ATTEMPTS + 1):
        delay = SJP_RETRY_DELAYS[min(attempt - 1, len(SJP_RETRY_DELAYS) - 1)]
        if delay:
            print(f"SJP retry {attempt}/{SJP_MAX_ATTEMPTS}: waiting {delay}s...")
            time.sleep(delay)

        try:
            listings = sjp_scrape_index()
            sjp_validate(listings, state)
            if attempt > 1:
                print(f"SJP recovered on attempt {attempt}/{SJP_MAX_ATTEMPTS}.")
            return listings
        except (requests.RequestException, IncompleteScrapeError, RuntimeError) as exc:
            last_error = exc
            print(
                f"SJP attempt {attempt}/{SJP_MAX_ATTEMPTS} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    print(
        f"SJP unavailable after {SJP_MAX_ATTEMPTS} attempts; "
        "preserving prior state/history. "
        f"Last error: {last_error}",
        file=sys.stderr,
    )
    return None


def sjp_message(listing, details, location, event, first_seen, last_removed_at=None):
    priority = priority_for_location(location)
    heading = (
        "🔄 <b>SJP AFFORDABLE LISTING REAPPEARED</b>"
        if event == "reappeared"
        else "🏙️ <b>NEW SJP AFFORDABLE RE-RENTAL</b>"
    )

    status = sjp_status(details.get("status"))
    status_display = {
        "available": "🟢 AVAILABLE",
        "rented / waitlist met": "⚫ RENTED / WAITLIST MET",
        "unknown": "⚪ STATUS UNKNOWN",
    }.get(status, f"⚪ {status.upper()}")

    lines = [
        heading,
        f"<b>{status_display}</b>",
        f"{priority['emoji']} <b>{priority['label']} · {priority['score']}/3</b>",
        f"<i>{html.escape(priority['reason'])}</i>",
        "",
        f"🏠 <b>{html.escape(listing.get('title') or 'SJP re-rental')}</b>",
    ]

    if details.get("location_label"):
        lines.append(f"📫 {html.escape(details['location_label'])}")
    if details.get("rent"):
        lines.append(f"💰 <b>{html.escape(details['rent'])}/mo</b>")
    if details.get("unit_size"):
        lines.append(f"🛏️ <b>{html.escape(details['unit_size'])}</b>")
    if details.get("ami"):
        lines.append(f"📊 AMI: <b>{html.escape(details['ami'])}</b>")

    if details.get("one_person_eligible") is False:
        lines.append("👤 1-person household: <b>Not eligible</b>")
    elif details.get("one_person_eligible") is True:
        lines.append("👤 1-person household: <b>Eligible</b>")
        if details.get("one_person_income"):
            lines.append(
                f"💵 1-person income: <b>{html.escape(details['one_person_income'])}</b>"
            )

    if details.get("utilities"):
        lines.append(f"⚡ Utilities: {html.escape(details['utilities'])}")

    if location:
        n = normalize(location.get("neighborhood"))
        b = normalize(location.get("borough"))
        z = normalize(location.get("postcode"))
        if n and b:
            lines.append(f"📍 <b>{html.escape(n)}, {html.escape(b)}</b>")
        elif b:
            lines.append(f"📍 <b>{html.escape(b)}</b>")
        elif n:
            lines.append(f"📍 <b>{html.escape(n)}</b>")
        if z:
            lines.append(f"📮 {html.escape(z)}")

    lines.append(f"🕐 First detected: <b>{html.escape(format_et(first_seen))}</b>")

    if event == "reappeared" and last_removed_at:
        d = human_duration(last_removed_at, utc_now_iso())
        if d:
            lines.append(f"↩️ Reappeared after <b>{html.escape(d)}</b>")

    detail_url = html.escape(listing.get("url") or SJP_URL, quote=True)
    apply_url = html.escape(details.get("apply_url") or listing.get("url") or SJP_URL, quote=True)

    # SJP often publishes cross streets rather than a full postal address.
    maps_query = details.get("location_label") or listing.get("title") or ""
    maps = html.escape(google_maps_url(maps_query), quote=True)

    lines += [
        "",
        f'📝 <a href="{apply_url}"><b>Apply now</b></a>',
        f'🔎 <a href="{detail_url}">View SJP listing details</a>',
        f'🗺️ <a href="{maps}">Open in Google Maps</a>',
        "",
        "<i>Source: SJP Tax Consultants · Location data: © OpenStreetMap contributors</i>",
    ]

    return "\n".join(lines)


def run_sjp_monitor():
    state = sjp_load(SJP_STATE_PATH, {"initialized": False, "listings": {}})
    history = sjp_load(
        SJP_HISTORY_PATH,
        {"version": 1, "created_at": utc_now_iso(), "listings": {}},
    )

    listings = sjp_fetch(state)
    if listings is None:
        return

    now = utc_now_iso()
    entries = history.setdefault("listings", {})
    current = {sjp_key(x): x for x in listings}
    current_keys = set(current)

    if not state.get("initialized"):
        # First run enriches current cards once so status and one-person data
        # are already known in history, but sends no Telegram alerts.
        for key, listing in current.items():
            try:
                details = sjp_enrich_listing(listing)
            except Exception as exc:
                print(f"SJP baseline enrichment failed for {listing.get('url')}: {exc}")
                details = {"status": "unknown"}

            entries[key] = {
                **listing,
                "details": details,
                "first_seen": now,
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": None,
            }

        history["updated_at"] = now
        state = {
            "initialized": True,
            "updated_at": now,
            "listings": {
                key: {
                    "title": item.get("title"),
                    "url": item.get("url"),
                }
                for key, item in current.items()
            },
        }
        sjp_save(SJP_HISTORY_PATH, history)
        sjp_save(SJP_STATE_PATH, state)
        print(
            f"SJP initialized with {len(listings)} existing card(s); "
            "no existing listings alerted."
        )
        return

    alerts = []
    removed = []

    for key, entry in entries.items():
        if entry.get("active") and key not in current_keys:
            entry["active"] = False
            entry["last_removed_at"] = now
            entry["removal_count"] = int(entry.get("removal_count", 0)) + 1
            removed.append(entry)

    for key, listing in current.items():
        entry = entries.get(key)

        if entry is None:
            try:
                details = sjp_enrich_listing(listing)
            except Exception as exc:
                print(f"SJP enrichment failed for new listing {listing.get('url')}: {exc}")
                details = {"status": "unknown"}

            # The index is intended to be current inventory, but do not alert a
            # detail page that explicitly says Rented / Waitlist Met.
            status = sjp_status(details.get("status"))

            location_query = details.get("location_label") or listing.get("title") or ""
            location = geocode_nyc_address(location_query)

            entry = {
                **listing,
                "details": details,
                "first_seen": now,
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": location,
            }
            entries[key] = entry

            if status != "rented / waitlist met":
                alerts.append((listing, entry, "new", None))
            continue

        was_active = bool(entry.get("active"))
        last_removed_at = entry.get("last_removed_at")

        entry.update(listing)
        entry["last_seen"] = now
        entry["active"] = True

        if not was_active:
            try:
                details = sjp_enrich_listing(listing)
            except Exception as exc:
                print(f"SJP enrichment failed for reappeared listing {listing.get('url')}: {exc}")
                details = entry.get("details") or {"status": "unknown"}

            entry["details"] = details

            if not entry.get("location"):
                query = details.get("location_label") or listing.get("title") or ""
                entry["location"] = geocode_nyc_address(query)

            entry["appearance_count"] = int(entry.get("appearance_count", 1)) + 1

            if sjp_status(details.get("status")) != "rented / waitlist met":
                alerts.append((listing, entry, "reappeared", last_removed_at))

    for listing, entry, event, last_removed_at in alerts:
        send_telegram(
            sjp_message(
                listing,
                entry.get("details") or {},
                entry.get("location"),
                event,
                entry.get("first_seen"),
                last_removed_at,
            ),
            parse_mode="HTML",
        )

    for entry in removed:
        print(
            f"SJP removed: {entry.get('title')} "
            f"(removal #{entry.get('removal_count', 1)})"
        )

    history["updated_at"] = now
    state = {
        "initialized": True,
        "updated_at": now,
        "listings": {
            key: {
                "title": item.get("title"),
                "url": item.get("url"),
            }
            for key, item in current.items()
        },
    }

    sjp_save(SJP_HISTORY_PATH, history)
    sjp_save(SJP_STATE_PATH, state)

    print(
        f"SJP complete: {len(alerts)} alert event(s), "
        f"{len(removed)} removal(s)."
    )




# ---------------------------------------------------------------------------
# Affordable Housing Group (AHG) monitor
# ---------------------------------------------------------------------------

AHG_URL = "https://ahgleasing.com/"
AHG_REQUEST_TIMEOUT = (2.5, 8.0)
AHG_MAX_ATTEMPTS = 3
AHG_RETRY_DELAYS = (0, 2, 5)
AHG_STATE_PATH = Path(os.getenv("AHG_STATE_PATH", "ahg_state.json"))
AHG_HISTORY_PATH = Path(os.getenv("AHG_HISTORY_PATH", "ahg_history.json"))
AHG_USER_AGENT = "nyc-rerental-monitor/5.21 (personal affordable-housing availability notifier)"


def ahg_norm(v):
    return re.sub(r"\s+", " ", str(v or "").replace("\u00a0", " ")).strip()


def ahg_key(item):
    url = ahg_norm(item.get("url")).rstrip("/").casefold()
    if url:
        return url
    title = re.sub(
        r"[^a-z0-9]+",
        " ",
        ahg_norm(item.get("title")).casefold(),
    ).strip()
    return title


def ahg_money(v):
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", ahg_norm(v))
    if not m:
        return ahg_norm(v) or None
    try:
        return f"${float(m.group(1).replace(',', '')):,.2f}"
    except ValueError:
        return "$" + m.group(1)


def ahg_income(v):
    amounts = re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", ahg_norm(v))
    if len(amounts) >= 2:
        return f"{ahg_money(amounts[0])} – {ahg_money(amounts[1])}"
    return ahg_norm(v) or None


def ahg_parse_index(response):
    soup = BeautifulSoup(response.text, "html.parser")
    listings = []

    # The homepage currently presents active opportunities as building links.
    for a in soup.find_all("a", href=True):
        href = urljoin(AHG_URL, a.get("href"))
        title = ahg_norm(a.get_text(" ", strip=True))
        if not title:
            continue

        low = title.casefold()
        if not any(
            token in low
            for token in (
                "tribeca park",
                "553w30",
                "street",
                "avenue",
                "ave.",
                "road",
                "boulevard",
            )
        ):
            continue

        # Only AHG-owned opportunity/detail/PDF links.
        if "ahgleasing.com" not in href:
            continue

        context = ""
        parent = a.parent
        if parent is not None:
            context = ahg_norm(parent.get_text(" ", strip=True))

        opportunity_type = None
        preceding = []
        node = a
        for _ in range(5):
            try:
                sib = node.find_previous(["h1","h2","h3","h4","p","div"])
            except Exception:
                sib = None
            if sib is None:
                break
            txt = ahg_norm(sib.get_text(" ", strip=True))
            if txt and txt not in preceding:
                preceding.append(txt)
            node = sib

        nearby = " | ".join(preceding[:6] + [context])
        if "accessibility" in nearby.casefold():
            opportunity_type = "Accessibility Units / Waiting List"
        elif "moderate income" in nearby.casefold():
            opportunity_type = "Moderate Income Housing Lottery"
        elif "low income" in nearby.casefold():
            opportunity_type = "Low Income Housing Opportunity"

        listings.append(
            {
                "source": "Affordable Housing Group",
                "title": title,
                "url": href,
                "opportunity_type": opportunity_type,
            }
        )

    listings = list({ahg_key(x): x for x in listings}.values())

    if not listings:
        title = ahg_norm(soup.title.get_text(" ", strip=True)) if soup.title else "(no title)"
        raise RuntimeError(
            "AHG homepage loaded but no active opportunities were parsed. "
            f"status={response.status_code}; bytes={len(response.content)}; title={title!r}"
        )

    print(f"AHG index scrape: {len(listings)} active opportunity link(s).")
    return listings


def ahg_scrape_index():
    r = requests.get(
        AHG_URL,
        headers={"User-Agent": AHG_USER_AGENT},
        timeout=AHG_REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return ahg_parse_index(r)


def ahg_extract_pdf_text(url):
    # Prefer requests; AHG PDFs are text PDFs. pypdf is optional, so fall back
    # to the linked HTML page if PDF parsing is unavailable.
    r = requests.get(
        url,
        headers={"User-Agent": AHG_USER_AGENT},
        timeout=AHG_REQUEST_TIMEOUT,
    )
    r.raise_for_status()

    if "pdf" not in (r.headers.get("content-type") or "").casefold() and not url.casefold().endswith(".pdf"):
        return None, r.text

    try:
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(r.content))
        text_value = "\n".join(page.extract_text() or "" for page in reader.pages)
        return ahg_norm(text_value), None
    except Exception:
        return None, None


def ahg_enrich_listing(listing):
    details = {
        "address": None,
        "deadline": None,
        "lottery_date": None,
        "status": "active",
        "accessibility_only": False,
        "accessibility_note": None,
        "tiers": [],
        "apply_url": None,
        "application_email": None,
        "application_phone": None,
        "utilities": None,
    }

    url = listing.get("url")
    if not url:
        return details

    text_value = None
    html_value = None

    try:
        text_value, html_value = ahg_extract_pdf_text(url)
    except requests.RequestException:
        raise

    # If the destination is HTML (e.g. /accessibility-units/), parse it and
    # follow the AHG-owned PDF flyer link when present.
    if html_value is not None:
        soup = BeautifulSoup(html_value, "html.parser")
        page_text = ahg_norm(soup.get_text(" ", strip=True))

        if "mobility impairments" in page_text.casefold() or "visual or hearing impairments" in page_text.casefold():
            details["accessibility_only"] = True
            details["accessibility_note"] = (
                "Only accepting applications for households requiring a mobility "
                "and/or visual/hearing accessible unit."
            )

        flyer = None
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a.get("href"))
            if href.casefold().endswith(".pdf") and "ahgleasing.com" in href:
                flyer = href
                break

        if flyer:
            try:
                pdf_text, _ = ahg_extract_pdf_text(flyer)
                if pdf_text:
                    text_value = pdf_text
                    details["flyer_url"] = flyer
            except requests.RequestException:
                text_value = page_text
        else:
            text_value = page_text

    text_value = ahg_norm(text_value)

    # Address.
    address_match = re.search(
        r"(\d{1,5}(?:-\d{1,4})?\s+[^|]{2,80}?"
        r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Place|Pl\.?|Boulevard|Blvd\.?|Drive|Dr\.?))"
        r"(?:,\s*New York,\s*NY\s*\d{5})?",
        text_value,
        re.I,
    )
    if address_match:
        address = ahg_norm(address_match.group(0))
        if "NY" not in address.upper():
            address += ", New York, NY"
        details["address"] = address

    deadline_match = re.search(
        r"Application\s+Due(?:\s+Date)?\s*:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        text_value,
        re.I,
    )
    if deadline_match:
        details["deadline"] = ahg_norm(deadline_match.group(1))

    lottery_match = re.search(
        r"Lottery\s+Date\s*:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        text_value,
        re.I,
    )
    if lottery_match:
        details["lottery_date"] = ahg_norm(lottery_match.group(1))

    email_match = re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", text_value)
    if email_match:
        details["application_email"] = email_match.group(0)

    phone_match = re.search(r"\(\d{3}\)\s*\d{3}-\d{4}", text_value)
    if phone_match:
        details["application_phone"] = phone_match.group(0)

    # Online apply link appears in Tribeca flyer.
    online_match = re.search(r"https?://housingsearch\.hcr\.ny\.gov", text_value, re.I)
    if online_match:
        details["apply_url"] = online_match.group(0)

    if "accessible units" in text_value.casefold() or "mobility impairment" in text_value.casefold():
        details["accessibility_only"] = True
        if not details["accessibility_note"]:
            details["accessibility_note"] = (
                "Accessibility-unit waiting list; qualifying mobility and/or "
                "visual/hearing impairment required."
            )

    # Utilities notes.
    util = re.search(
        r"(Rent includes[^.]+(?:\.[^.]+)?|Tenant responsible for electricity)",
        text_value,
        re.I,
    )
    if util:
        details["utilities"] = ahg_norm(util.group(0))

    # Tier extraction for known AHG flyer layouts.
    tiers = []

    # Tribeca-style: 130% Studio $3,765 ... followed by household income rows.
    tier_heads = list(re.finditer(
        r"(\d{2,3})%\s+(Studio|1\s*BR|2\s*BR|One\s+Bedroom|Two\s+Bedroom)\s+"
        r"(\$\s*[\d,]+(?:\.\d{1,2})?)",
        text_value,
        re.I,
    ))

    for idx, m in enumerate(tier_heads):
        section_end = tier_heads[idx + 1].start() if idx + 1 < len(tier_heads) else len(text_value)
        section = text_value[m.end():section_end]
        ami = f"{m.group(1)}%"
        raw_size = m.group(2)
        size_low = raw_size.casefold()
        if "studio" in size_low:
            unit_size = "Studio"
        elif size_low.startswith("1") or "one" in size_low:
            unit_size = "1 Bedroom"
        else:
            unit_size = "2 Bedrooms"

        income_rows = re.findall(
            r"\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*\$\s*[\d,]+(?:\.\d{1,2})?",
            section,
        )

        one_income = ahg_income(income_rows[0]) if income_rows else None

        # Studio and 1BR allow a 1-person household on current AHG Tribeca flyer.
        one_eligible = unit_size in {"Studio", "1 Bedroom"} if income_rows else None

        tiers.append({
            "ami": ami,
            "unit_size": unit_size,
            "rent": ahg_money(m.group(3)),
            "one_person_eligible": one_eligible,
            "one_person_income": one_income if one_eligible else None,
            "published_income_rows": [ahg_income(x) for x in income_rows],
        })

    # 553W30-style table text after PDF extraction.
    if not tiers:
        access_heads = list(re.finditer(
            r"(50|60)%\s+AREA\s+MEDIAN\s+INCOME\s+\(AMI\).*?"
            r"(Studio|One\s+Bedroom|Two\s+Bedroom)\s+"
            r"(\$\s*[\d,]+(?:\.\d{1,2})?)",
            text_value,
            re.I,
        ))

        for idx, m in enumerate(access_heads):
            section_end = access_heads[idx + 1].start() if idx + 1 < len(access_heads) else len(text_value)
            section = text_value[m.end():section_end]
            raw_size = m.group(2).casefold()
            if "studio" in raw_size:
                unit_size = "Studio"
            elif "one" in raw_size:
                unit_size = "1 Bedroom"
            else:
                unit_size = "2 Bedrooms"

            income_rows = re.findall(
                r"\$\s*[\d,]+(?:\.\d{1,2})?\s*[-–—]\s*\$\s*[\d,]+(?:\.\d{1,2})?",
                section,
            )
            one_eligible = unit_size in {"Studio", "1 Bedroom"}
            tiers.append({
                "ami": f"{m.group(1)}%",
                "unit_size": unit_size,
                "rent": ahg_money(m.group(3)),
                "one_person_eligible": one_eligible,
                "one_person_income": ahg_income(income_rows[0]) if (one_eligible and income_rows) else None,
                "published_income_rows": [ahg_income(x) for x in income_rows],
            })

    # If PDF extraction isn't available, keep the opportunity alert useful.
    details["tiers"] = tiers
    return details


def ahg_load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc


def ahg_save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def ahg_validate(listings, state):
    current = len(listings)
    previous = len(state.get("listings", {}))
    if current == 0:
        raise IncompleteScrapeError("AHG captured 0 opportunities. Previous baseline preserved.")
    if previous >= 4 and current < max(1, previous // 2):
        raise IncompleteScrapeError(
            f"Suspicious AHG opportunity-count drop: {previous} -> {current}. "
            "Previous baseline preserved."
        )


def ahg_fetch(state):
    last_error = None
    for attempt in range(1, AHG_MAX_ATTEMPTS + 1):
        delay = AHG_RETRY_DELAYS[min(attempt - 1, len(AHG_RETRY_DELAYS) - 1)]
        if delay:
            print(f"AHG retry {attempt}/{AHG_MAX_ATTEMPTS}: waiting {delay}s...")
            time.sleep(delay)
        try:
            listings = ahg_scrape_index()
            ahg_validate(listings, state)
            if attempt > 1:
                print(f"AHG recovered on attempt {attempt}/{AHG_MAX_ATTEMPTS}.")
            return listings
        except (requests.RequestException, IncompleteScrapeError, RuntimeError) as exc:
            last_error = exc
            print(
                f"AHG attempt {attempt}/{AHG_MAX_ATTEMPTS} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    print(
        f"AHG unavailable after {AHG_MAX_ATTEMPTS} attempts; "
        "preserving prior state/history. "
        f"Last error: {last_error}",
        file=sys.stderr,
    )
    return None


def ahg_message(listing, details, location, event, first_seen, last_removed_at=None):
    priority = priority_for_location(location)
    heading = (
        "🔄 <b>AHG OPPORTUNITY REAPPEARED</b>"
        if event == "reappeared"
        else "🏙️ <b>NEW AHG AFFORDABLE HOUSING OPPORTUNITY</b>"
    )

    lines = [
        heading,
        f"{priority['emoji']} <b>{priority['label']} · {priority['score']}/3</b>",
        f"<i>{html.escape(priority['reason'])}</i>",
        "",
        f"🏠 <b>{html.escape(listing.get('title') or 'AHG opportunity')}</b>",
    ]

    if listing.get("opportunity_type"):
        lines.append(f"📋 {html.escape(listing['opportunity_type'])}")

    if details.get("accessibility_only"):
        lines.append("♿ <b>Accessibility-restricted opportunity</b>")
        if details.get("accessibility_note"):
            lines.append(f"<i>{html.escape(details['accessibility_note'])}</i>")

    if details.get("deadline"):
        lines.append(f"⏰ Application deadline: <b>{html.escape(details['deadline'])}</b>")
    if details.get("lottery_date"):
        lines.append(f"🎟️ Lottery date: <b>{html.escape(details['lottery_date'])}</b>")

    tiers = details.get("tiers") or []
    if tiers:
        lines.append("")
        for tier in tiers:
            parts = []
            if tier.get("ami"):
                parts.append(tier["ami"] + " AMI")
            if tier.get("unit_size"):
                parts.append(tier["unit_size"])
            if tier.get("rent"):
                parts.append(tier["rent"] + "/mo")
            lines.append("• <b>" + html.escape(" · ".join(parts)) + "</b>")
            if tier.get("one_person_eligible") is False:
                lines.append("  👤 1-person household: Not eligible")
            elif tier.get("one_person_eligible") is True:
                lines.append("  👤 1-person household: Eligible")
                if tier.get("one_person_income"):
                    lines.append(
                        "  💵 1-person income: "
                        f"<b>{html.escape(tier['one_person_income'])}</b>"
                    )

    if details.get("utilities"):
        lines.append(f"⚡ {html.escape(details['utilities'])}")

    if location:
        n = normalize(location.get("neighborhood"))
        b = normalize(location.get("borough"))
        z = normalize(location.get("postcode"))
        if n and b:
            lines.append(f"📍 <b>{html.escape(n)}, {html.escape(b)}</b>")
        elif b:
            lines.append(f"📍 <b>{html.escape(b)}</b>")
        elif n:
            lines.append(f"📍 <b>{html.escape(n)}</b>")
        if z:
            lines.append(f"📮 {html.escape(z)}")

    if details.get("application_email"):
        lines.append(f"✉️ {html.escape(details['application_email'])}")
    if details.get("application_phone"):
        lines.append(f"☎️ {html.escape(details['application_phone'])}")

    lines.append(f"🕐 First detected: <b>{html.escape(format_et(first_seen))}</b>")

    if event == "reappeared" and last_removed_at:
        d = human_duration(last_removed_at, utc_now_iso())
        if d:
            lines.append(f"↩️ Reappeared after <b>{html.escape(d)}</b>")

    detail_url = html.escape(
        details.get("flyer_url") or listing.get("url") or AHG_URL,
        quote=True,
    )
    apply_url = html.escape(
        details.get("apply_url") or listing.get("url") or AHG_URL,
        quote=True,
    )
    map_query = details.get("address") or listing.get("title") or ""
    maps = html.escape(google_maps_url(map_query), quote=True)

    lines += [
        "",
        f'📝 <a href="{apply_url}"><b>Apply / application information</b></a>',
        f'📄 <a href="{detail_url}">View AHG flyer/details</a>',
        f'🗺️ <a href="{maps}">Open in Google Maps</a>',
        f'📋 <a href="{html.escape(AHG_URL, quote=True)}">AHG opportunities page</a>',
        "",
        "<i>Source: Affordable Housing Group · Location data: © OpenStreetMap contributors</i>",
    ]

    return "\n".join(lines)


def run_ahg_monitor():
    state = ahg_load(AHG_STATE_PATH, {"initialized": False, "listings": {}})
    history = ahg_load(
        AHG_HISTORY_PATH,
        {"version": 1, "created_at": utc_now_iso(), "listings": {}},
    )

    listings = ahg_fetch(state)
    if listings is None:
        return

    now = utc_now_iso()
    entries = history.setdefault("listings", {})
    current = {ahg_key(x): x for x in listings}
    current_keys = set(current)

    if not state.get("initialized"):
        # Silent baseline but enrich once so all current opportunity details are
        # already present in history for later status/reappearance use.
        for key, listing in current.items():
            try:
                details = ahg_enrich_listing(listing)
            except Exception as exc:
                print(f"AHG baseline enrichment failed for {listing.get('url')}: {exc}")
                details = {"status": "active", "tiers": []}

            entries[key] = {
                **listing,
                "details": details,
                "first_seen": now,
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": None,
            }

        history["updated_at"] = now
        state = {
            "initialized": True,
            "updated_at": now,
            "listings": {
                key: {"title": item.get("title"), "url": item.get("url")}
                for key, item in current.items()
            },
        }
        ahg_save(AHG_HISTORY_PATH, history)
        ahg_save(AHG_STATE_PATH, state)
        print(
            f"AHG initialized with {len(listings)} current opportunity(ies); "
            "no existing opportunities alerted."
        )
        return

    alerts = []
    removed = []

    for key, entry in entries.items():
        if entry.get("active") and key not in current_keys:
            entry["active"] = False
            entry["last_removed_at"] = now
            entry["removal_count"] = int(entry.get("removal_count", 0)) + 1
            removed.append(entry)

    for key, listing in current.items():
        entry = entries.get(key)

        if entry is None:
            try:
                details = ahg_enrich_listing(listing)
            except Exception as exc:
                print(f"AHG enrichment failed for new opportunity {listing.get('url')}: {exc}")
                details = {"status": "active", "tiers": []}

            location = geocode_nyc_address(
                details.get("address") or listing.get("title") or ""
            )
            entry = {
                **listing,
                "details": details,
                "first_seen": now,
                "last_seen": now,
                "active": True,
                "appearance_count": 1,
                "removal_count": 0,
                "last_removed_at": None,
                "location": location,
            }
            entries[key] = entry
            alerts.append((listing, entry, "new", None))
            continue

        was_active = bool(entry.get("active"))
        last_removed_at = entry.get("last_removed_at")
        entry.update(listing)
        entry["last_seen"] = now
        entry["active"] = True

        if not was_active:
            try:
                details = ahg_enrich_listing(listing)
            except Exception as exc:
                print(f"AHG enrichment failed for reappeared opportunity {listing.get('url')}: {exc}")
                details = entry.get("details") or {"status": "active", "tiers": []}

            entry["details"] = details
            if not entry.get("location"):
                entry["location"] = geocode_nyc_address(
                    details.get("address") or listing.get("title") or ""
                )

            entry["appearance_count"] = int(entry.get("appearance_count", 1)) + 1
            alerts.append((listing, entry, "reappeared", last_removed_at))

    for listing, entry, event, last_removed_at in alerts:
        send_telegram(
            ahg_message(
                listing,
                entry.get("details") or {},
                entry.get("location"),
                event,
                entry.get("first_seen"),
                last_removed_at,
            ),
            parse_mode="HTML",
        )

    for entry in removed:
        print(
            f"AHG removed: {entry.get('title')} "
            f"(removal #{entry.get('removal_count', 1)})"
        )

    history["updated_at"] = now
    state = {
        "initialized": True,
        "updated_at": now,
        "listings": {
            key: {"title": item.get("title"), "url": item.get("url")}
            for key, item in current.items()
        },
    }

    ahg_save(AHG_HISTORY_PATH, history)
    ahg_save(AHG_STATE_PATH, state)

    print(
        f"AHG complete: {len(alerts)} alert event(s), "
        f"{len(removed)} removal(s)."
    )



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

        scraped_options, current_record_ids = scrape_options()
        current_options = set(scraped_options)
        validate_scrape(current_options, old_options)

        print(
            f"Validated scrape: {len(current_options)} current option(s); "
            f"{len(old_options)} previous option(s)."
        )

        # Upgrade protection from pre-v3 scraper versions.
        if state.get("scraper_version") != SCRAPER_VERSION:
            save_state(list(current_options), current_record_ids)
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
            save_state(list(current_options), current_record_ids)
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
            current_record_ids,
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
                reside=entry.get("reside"),
            )
            send_telegram(message, parse_mode="HTML")

        for entry in removed_entries:
            print(
                "Removed: "
                f"{entry.get('address')} "
                f"{entry.get('unit') or ''} "
                f"(removal #{entry.get('removal_count', 1)})"
            )

        save_state(list(current_options), current_record_ids)
        save_history(history)
        record_success(health)

    except Exception as exc:
        print(f"Monitor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        record_failure(health, exc)
        raise


def run_test_listing(row: str) -> None:
    history = load_history()
    parsed = parse_listing_row(row)

    # Test mode is Reside-specific. Scrape the picker only to resolve the
    # Airtable linked-record ID for the exact project; do not change baseline,
    # history, or health.
    try:
        test_options, test_record_ids = scrape_options()
        normalized_row = normalize(row)

        rec_id = test_record_ids.get(normalized_row)

        # If the manual test text differs only in harmless whitespace/case,
        # match against the actual picker value.
        if not rec_id:
            wanted = normalized_row.casefold()
            for option in test_options:
                if normalize(option).casefold() == wanted:
                    rec_id = test_record_ids.get(normalize(option))
                    if rec_id:
                        break

        if rec_id:
            parsed["airtable_record_id"] = rec_id
            print("Resolved Airtable linked-record ID for Reside test listing.")
        else:
            print(
                "Warning: Airtable linked-record ID was not resolved for the "
                "test listing; application link will open without project preselection."
            )
    except Exception as exc:
        print(
            "Warning: could not resolve Airtable linked-record ID during test: "
            f"{type(exc).__name__}: {exc}"
        )

    reside = enrich_from_reside(parsed)

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
        reside=reside,
    )
    send_telegram(message, parse_mode="HTML")
    print("Test notification sent. Baseline/history/health were not changed.")
def main() -> int:
    if TEST_LISTING:
        # Existing manual test remains Reside-specific.
        run_test_listing(TEST_LISTING)
        return 0

    # Run all nine sources concurrently so one source does not block another.
    errors = []

    with ThreadPoolExecutor(max_workers=9) as executor:
        futures = {
            executor.submit(run_normal_monitor): "Reside",
            executor.submit(run_fac_monitor): "FAC",
            executor.submit(run_rockrose_monitor): "Rockrose",
            executor.submit(run_mns_monitor): "MNS",
            executor.submit(
                run_mgny_monitor,
                send_telegram,
                geocode_nyc_address,
                priority_for_location,
                google_maps_url,
                utc_now_iso,
                format_et,
            ): "MGNY",
            executor.submit(run_hpd_google_monitor): "HPD / Tax Solute",
            executor.submit(run_taxace_monitor): "Taxace",
            executor.submit(run_sjp_monitor): "SJP",
            executor.submit(run_ahg_monitor): "AHG",
        }

        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
                print(f"{name} monitor finished successfully.")
            except Exception as exc:
                print(
                    f"{name} monitor failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                errors.append((name, exc))

    if errors:
        names = ", ".join(name for name, _ in errors)
        raise RuntimeError(f"One or more monitor sources failed: {names}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlaywrightTimeoutError as exc:
        print(f"Playwright timed out: {exc}", file=sys.stderr)
        raise
