"""
Reads rainwater tank level, temperature, and header tank level from
tanklevels.co.uk (Black Box Controls / Acculevel portal), and writes them
to data.json in this repo.

The site is a Blazor Server app - the values only appear after a live
connection renders them, so this uses a real headless browser (Playwright)
rather than a plain HTTP scrape.

Two device pages are used:
  - The Acculevel's own device page: rainwater tank level % and air temp
    (the direct, freshest sensor reading).
  - The Rain Director device page: header tank level % (not available
    anywhere else).

Each value's "Last update received" timestamp from its source page is
captured too, so Home Assistant can tell a fresh reading from a stale one.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ACCULEVEL_URL = "https://tanklevels.co.uk/devices/b8QP6j0nVz97"
RAIN_DIRECTOR_URL = "https://tanklevels.co.uk/devices/djn64XAk62AP"

LEVEL_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")
TEMP_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*°\s*C")
HEADER_TANK_PATTERN = re.compile(r"Header Tank:\s*(\d+(?:\.\d+)?)\s*%")
LAST_UPDATE_PATTERN = re.compile(r"Last update received:\s*(.+)")


def try_login(page, username, password):
    """Best-effort login. The login form itself is rendered by the same
    Blazor SignalR circuit as everything else on this site, so it doesn't
    exist in the DOM immediately after page load - wait for it to appear
    rather than checking once. If it genuinely never appears (already
    authenticated, e.g. a reused session), that's a normal outcome too."""
    try:
        page.locator("input[type=password]").first.wait_for(state="visible", timeout=15000)
    except PlaywrightTimeoutError:
        return  # no login form showed up - assume already authenticated

    filled_user = False
    for selector in [
        "input[type=email]",
        "input[name*=mail i]",
        "input[name*=user i]",
        "input[type=text]",
    ]:
        loc = page.locator(selector).first
        if loc.count() > 0:
            loc.fill(username)
            filled_user = True
            break
    if not filled_user:
        raise RuntimeError("Could not find a username/email field on the login page")

    page.locator("input[type=password]").first.fill(password)

    # Give Blazor's two-way data binding a moment to register the typed
    # values server-side before we trigger the submit.
    page.wait_for_timeout(500)

    print(f"[login] about to submit, current url: {page.url}", file=sys.stderr)

    clicked = False
    for role_name in ["Sign in", "Log in", "Login"]:
        btn = page.get_by_role("button", name=re.compile(role_name, re.I))
        if btn.count() > 0:
            btn.first.click()
            clicked = True
            break
    if not clicked:
        page.locator("button[type=submit]").first.click()

    # A Blazor login click may not fire the kind of network activity that
    # "networkidle" tracks, so wait for the actual, observable sign of
    # success: the password field disappearing (we've left the login page).
    try:
        page.locator("input[type=password]").wait_for(state="detached", timeout=20000)
    except PlaywrightTimeoutError:
        try:
            page.screenshot(path="debug_login_failed.png")
        except Exception:
            pass
        raise RuntimeError(
            f"Login did not navigate away from the login page (still on {page.url} "
            "after clicking submit) - see debug_login_failed.png"
        )

    print(f"[login] left login page, current url: {page.url}", file=sys.stderr)
    page.wait_for_load_state("networkidle", timeout=20000)


def wait_for_text(page, deadline_seconds=20):
    """The Blazor app fills the page in after its SignalR circuit connects,
    so poll body text for a few seconds rather than trusting page load."""
    import time

    deadline = time.time() + deadline_seconds
    text = ""
    while time.time() < deadline:
        text = page.inner_text("body")
        if LEVEL_PATTERN.search(text):
            return text
        page.wait_for_timeout(1000)
    return text


def read_acculevel_page(page, username, password):
    page.goto("https://tanklevels.co.uk/", wait_until="networkidle", timeout=30000)
    try_login(page, username, password)

    page.goto(ACCULEVEL_URL, wait_until="networkidle", timeout=30000)
    text = wait_for_text(page)

    level_match = LEVEL_PATTERN.search(text)
    temp_match = TEMP_PATTERN.search(text)
    update_match = LAST_UPDATE_PATTERN.search(text)

    if not level_match:
        raise RuntimeError("Timed out waiting for rainwater tank level to render")

    return {
        "rainwater_tank_level": float(level_match.group(1)),
        "rainwater_tank_temp": float(temp_match.group(1)) if temp_match else None,
        "rainwater_tank_last_update": update_match.group(1).strip() if update_match else None,
    }


def read_rain_director_page(page):
    page.goto(RAIN_DIRECTOR_URL, wait_until="networkidle", timeout=30000)
    text = wait_for_text(page)

    header_match = HEADER_TANK_PATTERN.search(text)
    update_match = LAST_UPDATE_PATTERN.search(text)

    if not header_match:
        raise RuntimeError("Timed out waiting for header tank level to render")

    return {
        "header_tank_level": float(header_match.group(1)),
        "header_tank_last_update": update_match.group(1).strip() if update_match else None,
    }


def main():
    username = os.environ["TANKLEVELS_USERNAME"]
    password = os.environ["TANKLEVELS_PASSWORD"]

    result = {"scraped_at": datetime.now(timezone.utc).isoformat()}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # The site formats "Last update received" client-side using the
        # browser's local timezone. GitHub's runners default to UTC, which
        # doesn't observe British Summer Time, so without this the times
        # come out an hour behind what you see browsing from the UK.
        context = browser.new_context(timezone_id="Europe/London")
        page = context.new_page()
        try:
            result.update(read_acculevel_page(page, username, password))
            result.update(read_rain_director_page(page))
        except Exception:
            try:
                page.screenshot(path="debug_failure.png")
                print("Saved debug_failure.png", file=sys.stderr)
            except Exception:
                pass
            raise
        finally:
            browser.close()

    with open("data.json", "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
