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
ADD_UNIT_TEXT = os.getenv("AIRTABLE_ADD_UNIT_TEXT", "Add unit")

STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
DEBUG_DIR = Path("debug")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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

    data = json.loads(STATE_PATH.read_text())
    return {
        "initialized": bool(data.get("initialized", False)),
        "options": list(data.get("options", [])),
        "updated_at": data.get("updated_at"),
    }


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
    """Explicitly click the Add unit button for Project Applying For."""
    # Prefer an accessible button match.
    button = page.get_by_role("button", name=re.compile(r"^\s*Add unit\s*$", re.I))
    if button.count() > 0:
        button.first.scroll_into_view_if_needed()
        button.first.click(timeout=10_000)
        return

    # Fallback: locate the field section, then look for a nearby Add unit button.
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


def visible_choice_texts(page) -> set[str]:
    """Collect text from common option/list/menu structures."""
    selectors = [
        '[role="option"]',
        '[role="listbox"] [role="option"]',
        '[role="menuitem"]',
        '[role="menu"] [role="menuitem"]',
        '[role="dialog"] [role="option"]',
        '[role="dialog"] [role="menuitem"]',
        '[role="dialog"] [role="listitem"]',
    ]
    out = set()

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
                out.add(text)

    return out


def click_second_stage_selector_if_needed(page) -> None:
    """
    After Add unit, Airtable may either:
      1) show the choices immediately, or
      2) show another combobox/button that must be opened.
    If choices are already visible, do nothing.
    """
    if visible_choice_texts(page):
        return

    # Prefer controls inside a dialog/popover that appeared after Add unit.
    scopes = []
    dialog = page.locator('[role="dialog"]')
    if dialog.count() > 0:
        scopes.append(dialog.last)
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

            for i in range(min(count, 10)):
                candidate = loc.nth(i)
                try:
                    if not candidate.is_visible():
                        continue
                    text = normalize(candidate.inner_text())
                    if text.casefold() == "add unit":
                        continue
                    candidate.click(timeout=5_000)
                    page.wait_for_timeout(700)
                    if visible_choice_texts(page):
                        return
                except Exception:
                    continue

    # Some Airtable UI may use a generic visible button instead of aria-haspopup.
    # Try likely buttons in the newest dialog, excluding Add unit/Cancel/Close.
    if dialog.count() > 0:
        buttons = dialog.last.locator("button")
        for i in range(min(buttons.count(), 15)):
            candidate = buttons.nth(i)
            try:
                if not candidate.is_visible():
                    continue
                text = normalize(candidate.inner_text()).casefold()
                if text in {"add unit", "cancel", "close", "done", ""}:
                    continue
                candidate.click(timeout=5_000)
                page.wait_for_timeout(700)
                if visible_choice_texts(page):
                    return
            except Exception:
                continue


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

            print('Step 1: clicking "Add unit"...')
            click_add_unit(page)
            page.wait_for_timeout(1_000)

            print("Step 2: opening the unit choices if Airtable requires another click...")
            click_second_stage_selector_if_needed(page)
            page.wait_for_timeout(700)

            collected = set()
            stable_rounds = 0
            previous_count = -1

            for _ in range(50):
                collected.update(visible_choice_texts(page))

                if len(collected) == previous_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    previous_count = len(collected)

                # Scroll the largest visible list/dialog/menu to expose virtualized choices.
                scrolled = page.evaluate(
                    """() => {
                        const els = [
                          ...document.querySelectorAll(
                            '[role="listbox"], [role="menu"], [role="dialog"]'
                          )
                        ].filter(el => {
                          const s = getComputedStyle(el);
                          const r = el.getBoundingClientRect();
                          return r.width > 0 && r.height > 0 &&
                                 s.visibility !== 'hidden' &&
                                 s.display !== 'none' &&
                                 el.scrollHeight > el.clientHeight + 4;
                        });

                        if (!els.length) return false;

                        const el = els.sort(
                          (a, b) => (b.scrollHeight - b.clientHeight) -
                                    (a.scrollHeight - a.clientHeight)
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

                if not scrolled and stable_rounds >= 2:
                    break
                if stable_rounds >= 6:
                    break

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
                raise RuntimeError(
                    'Clicked "Add unit" but captured no unit options. '
                    "Open the workflow's airtable-debug artifact to inspect the screenshot."
                )

            return options

        except Exception:
            save_debug(page, "scrape_failure")
            raise
        finally:
            browser.close()


def main() -> int:
    state = load_state()
    old_options = {
        normalize(x) for x in state.get("options", []) if normalize(x)
    }

    print(f"Opening Airtable form: {FORM_URL}")
    print(f'Watching: "{FIELD_LABEL}" -> "{ADD_UNIT_TEXT}" -> unit choices')

    current_options = set(scrape_options())
    print(f"Found {len(current_options)} current unit option(s).")

    if not state.get("initialized"):
        save_state(list(current_options))
        send_telegram(
            "✅ Reside Airtable monitor initialized.\n"
            f"Tracking {len(current_options)} current unit option(s).\n\n"
            'The monitor clicks "Add unit" before checking the list.\n'
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
            f"Open the application form:\n{FORM_URL}"
        )
        print("New option(s):")
        for item in new_options:
            print(f"  + {item}")
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
