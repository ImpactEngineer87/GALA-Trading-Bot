import unittest
from datetime import datetime, timedelta

from agents.massive_downloader import _iterar_ventanas


class MassiveDownloaderWindowTests(unittest.TestCase):
    def test_monthly_windows_cover_range_without_gaps(self):
        windows = list(_iterar_ventanas("2024-07-31", "2024-10-03", 30))
        self.assertEqual(windows[0], ("2024-07-31", "2024-08-29"))
        self.assertEqual(windows[-1], ("2024-09-29", "2024-10-03"))

        previous_end = None
        for start_text, end_text in windows:
            start = datetime.strptime(start_text, "%Y-%m-%d").date()
            end = datetime.strptime(end_text, "%Y-%m-%d").date()
            self.assertLessEqual(start, end)
            self.assertLessEqual((end - start).days, 29)
            if previous_end is not None:
                self.assertEqual(start, previous_end + timedelta(days=1))
            previous_end = end

    def test_invalid_window_size_is_rejected(self):
        with self.assertRaises(ValueError):
            list(_iterar_ventanas("2024-01-01", "2024-01-31", 0))

    def test_reversed_dates_are_rejected(self):
        with self.assertRaises(ValueError):
            list(_iterar_ventanas("2024-02-01", "2024-01-01", 30))


if __name__ == "__main__":
    unittest.main()
