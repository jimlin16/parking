"""Check the shared Google session without reading credential values."""
from urllib.parse import urlparse

from booking_safety import file_lock
from youparking_client import get_default_profile_dir


def check_google_login():
    from playwright.sync_api import sync_playwright

    profile = get_default_profile_dir()
    with file_lock(profile + '.lock'), sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            profile, channel='chrome', headless=True, locale='zh-TW')
        try:
            page = context.new_page()
            response = page.goto('https://myaccount.google.com/',
                                 wait_until='domcontentloaded', timeout=20000)
            if response is None or response.status >= 400:
                return 'unknown'
            page.wait_for_function("location.hostname === 'accounts.google.com' || "
                                   "(location.hostname === 'myaccount.google.com' && "
                                   "document.querySelector('a[href*=\"SignOut\"], a[href*=\"Logout\"]'))",
                                   timeout=10000)
            host = urlparse(page.url).hostname
            if host == 'accounts.google.com':
                return 'signed_out'
            if host == 'myaccount.google.com':
                return 'signed_in'
            return 'unknown'
        finally:
            context.close()
