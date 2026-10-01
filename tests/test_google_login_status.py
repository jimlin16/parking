import unittest
from unittest.mock import MagicMock, patch

from google_login_status import check_google_login


class GoogleLoginStatusTests(unittest.TestCase):
    def check(self, url, status=200, error=None):
        context = MagicMock()
        page = context.new_page.return_value
        page.url = url
        page.goto.return_value.status = status
        page.wait_for_function.side_effect = error
        with patch('google_login_status.get_default_profile_dir', return_value='profile'), \
                patch('google_login_status.file_lock'), \
                patch('playwright.sync_api.sync_playwright') as playwright:
            playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = context
            try:
                return check_google_login()
            finally:
                context.close.assert_called_once()

    def test_signed_in_requires_page_confirmation(self):
        self.assertEqual(self.check('https://myaccount.google.com/'), 'signed_in')

    def test_sign_in_redirect(self):
        self.assertEqual(self.check('https://accounts.google.com/v3/signin/identifier'), 'signed_out')

    def test_server_failure_is_unknown(self):
        self.assertEqual(self.check('https://myaccount.google.com/', 503), 'unknown')

    def test_missing_confirmation_is_not_signed_in(self):
        with self.assertRaises(TimeoutError):
            self.check('https://myaccount.google.com/', error=TimeoutError())
