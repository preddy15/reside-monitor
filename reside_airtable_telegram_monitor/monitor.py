import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

FORM_URL = os.getenv(
    "AIRTABLE_FORM_URL",
    "https://airtable.com/appsseXTOVx59HC0W/pagcVengefPFQvMZC/form",
)
FIELD_LABEL = os.getenv("AIRTABLE_FIELD_LABEL", "Project Applying For")
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
DEBUG_DIR = Path("debug")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

IGNORE_TEXT = {
    "",
    "select an option",
    "select option",
    "choose an option",
    "clear",
    "no options",
    "no results",
    "no results found",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def send_telegram(message: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable."
        )

    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API returned an error: {payload}")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"initialized": False, "options": []}

    try:
        data = json.loads(STATE_PATH.read_text())
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        return {
            "initialized": bool(data.get("initialized", False)),
            "options": list(data.get("options", [])),
            "updated_at": data.get("updated_at"),
        }
    except Exception as exc:
        raise RuntimeError(f"Could not read {STATE_PATH}: {exc}") from exc


def save_state(options: list[str]) -> None:
    STATE_PATH.write_text(
        json.dumps(
            {
                "initialized": True,
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
    safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason)[:40]
    try:
        page.screenshot(
            path=str(DEBUG_DIR / f"{safe_reason}.png"),
            full_page=True,
        )
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{safe_reason}.html").write_text(
            page.content(), encoding="utf-8"
        )
    except Exception:
        pass


def find_field_control(page):
    # Best case: Airtable exposes a useful accessible label.
    for role in ("combobox", "button"):
        try:
            locator = page.get_by_role(role, name=re.compile(re.escape(FIELD_LABEL), re.I))
            if locator.count() > 0:
                return locator.first
        except Exception:
            pass

    # Next, anchor on the visible field label and look in nearby containers.
    label = page.get_by_text(FIELD_LABEL, exact=True)
    if label.count() == 0:
        # Some Airtable views add required markers or extra whitespace.
        label = page.get_by_text(re.compile(re.escape(FIELD_LABEL), re.I))

    if label.count() == 0:
        raise RuntimeError(f'Could not find field label "{FIELD_LABEL}".')

    label = label.first

    candidate_selectors = [
        '[role="combobox"]',
        'button[aria-haspopup="listbox"]',
        'button[aria-haspopup="menu"]',
        'input[role="combobox"]',
        "button",
    ]

    # Airtable's exact wrapper structure can change, so walk up several ancestors.
    for levels_up in range(1, 9):
        ancestor = label.locator("xpath=" + "/.." * levels_up)
        for selector in candidate_selectors:
            candidate = ancestor.locator(selector)
            try:
                if candidate.count() > 0:
                    for i in range(min(candidate.count(), 6)):
                        item = candidate.nth(i)
                        if item.is_visible():
                            return item
            except Exception:
                continue

    raise RuntimeError(
        f'Found "{FIELD_LABEL}" but could not find its dropdown control.'
    )


def collect_visible_options(page) -> list[str]:
    selectors = [
        '[role="option"]',
        '[role="listbox"] [role="option"]',
        '[role="menu"] [role="menuitem"]',
    ]

    texts = []
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
            if text.casefold() not in IGNORE_TEXT:
                texts.append(text)

    return texts


def scrape_options() -> list[str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )

        try:
            page.goto(FORM_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)

            control = find_field_control(page)
            control.scroll_into_view_if_needed()
            control.click(timeout=10_000)
            page.wait_for_timeout(1_000)

            # Some select widgets use a searchable input after opening.
            # We don't type into it; we simply harvest the full option list.
            collected = set()
            stable_rounds = 0
            previous_count = -1

            for _ in range(40):
                for text in collect_visible_options(page):
                    collected.add(text)

                if len(collected) == previous_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    previous_count = len(collected)

                # Find the most likely scrollable menu/listbox and move downward.
                scrolled = page.evaluate(
                    """() => {
                        const candidates = [
                          ...document.querySelectorAll('[role="listbox"], [role="menu"]')
                        ].filter(el => {
                          const s = getComputedStyle(el);
                          return el.scrollHeight > el.clientHeight + 4 &&
                                 s.visibility !== 'hidden' &&
                                 s.display !== 'none';
                        });

                        if (!candidates.length) return false;

                        const el = candidates.sort(
                          (a, b) => b.scrollHeight - a.scrollHeight
                        )[0];

                        const before = el.scrollTop;
                        el.scrollTop = Math.min(
                          el.scrollHeight,
                          el.scrollTop + Math.max(250, el.clientHeight * 0.8)
                        );
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                        return el.scrollTop !== before;
                    }"""
                )

                page.wait_for_timeout(250)

                # Once scrolling can no longer reveal anything new, we're done.
                if not scrolled and stable_rounds >= 2:
                    break
                if stable_rounds >= 5:
                    break

            options = sorted(collected, key=str.casefold)

            # Fallback: native <option> elements, if Airtable changes the widget.
            if not options:
                native = page.locator("select option")
                for i in range(native.count()):
                    text = normalize(native.nth(i).inner_text())
                    if text.casefold() not in IGNORE_TEXT:
                        options.append(text)
                options = sorted(set(options), key=str.casefold)

            if not options:
                save_debug(page, "no_options_found")
                raise RuntimeError(
                    "The dropdown opened, but no options were captured. "
                    "Check the uploaded debug screenshot/HTML from this workflow run."
                )

            return options

        except Exception:
            save_debug(page, "scrape_failure")
            raise
        finally:
            browser.close()


def main() -> int:
    state = load_state()
    old_options = set(normalize(x) for x in state.get("options", []) if normalize(x))

    print(f"Opening Airtable form: {FORM_URL}")
    print(f'Watching dropdown: "{FIELD_LABEL}"')

    current_options = set(scrape_options())
    print(f"Found {len(current_options)} current option(s).")

    if not state.get("initialized"):
        save_state(list(current_options))
        send_telegram(
            "✅ Reside Airtable monitor initialized.\n"
            f"Tracking {len(current_options)} current option(s) in "
            f'"{FIELD_LABEL}".\n\n'
            "I will alert you only when a new option appears."
        )
        print("Initialized baseline; existing options were not sent as alerts.")
        return 0

    new_options = sorted(current_options - old_options, key=str.casefold)
    removed_options = sorted(old_options - current_options, key=str.casefold)

    if new_options:
        lines = "\n".join(f"• {item}" for item in new_options)
        send_telegram(
            "🚨 NEW RESIDE RE-RENTAL OPTION\n\n"
            f"{lines}\n\n"
            f"Apply/check the form:\n{FORM_URL}"
        )
        print("New option(s):")
        for item in new_options:
            print(f"  + {item}")
    else:
        print("No new options.")

    if removed_options:
        print("Removed option(s) since the previous run:")
        for item in removed_options:
            print(f"  - {item}")

    # Save the current snapshot so reappearing listings can alert again.
    if current_options != old_options:
        save_state(list(current_options))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlaywrightTimeoutError as exc:
        print(f"Playwright timed out: {exc}", file=sys.stderr)
        raise
