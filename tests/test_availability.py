import unittest

from youparking_client import YouParkingClient


class AvailabilityTests(unittest.TestCase):
    def check(self, availability, start="2026-10-21", end="2026-10-24"):
        client = YouParkingClient.__new__(YouParkingClient)
        client.get_space_left = lambda: availability
        return client.check_period_availability(start, end)

    def test_start_available_with_later_dates_missing(self):
        result = self.check({"2026-10-21": 135})
        self.assertTrue(result["all_available"])
        self.assertEqual(len(result["details"]), 4)
        self.assertIsNone(result["details"][-1]["left"])

    def test_later_full_dates_do_not_block_start(self):
        self.assertTrue(self.check({"2026-10-21": 1, "2026-10-24": 0})["all_available"])

    def test_unavailable_start_is_blocked_even_if_later_available(self):
        for left in (None, 0, -1):
            with self.subTest(left=left):
                self.assertFalse(self.check({"2026-10-21": left, "2026-10-24": 135})["all_available"])

    def test_reversed_dates_are_rejected(self):
        with self.assertRaises(ValueError):
            self.check({}, start="2026-10-24", end="2026-10-21")


if __name__ == "__main__":
    unittest.main()
