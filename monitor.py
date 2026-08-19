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
from urllib.parse import quote_plus, urljoin
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
    form = html.escape(FORM_URL, quote=True)

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

        entry["address"] = parsed.get("address")
        entry["unit"] = parsed.get("unit")
        entry["rent"] = parsed.get("rent")
        entry["raw"] = parsed.get("raw")
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
