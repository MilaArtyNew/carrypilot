import unittest

from exchanges.variational import normalize_funding_interval_seconds


class VariationalFundingIntervalTests(unittest.TestCase):
    def test_zero_interval_falls_back_to_eight_hours(self):
        self.assertEqual(normalize_funding_interval_seconds(0), 28800)

    def test_positive_interval_is_preserved(self):
        self.assertEqual(normalize_funding_interval_seconds(3600), 3600)


if __name__ == "__main__":
    unittest.main()
