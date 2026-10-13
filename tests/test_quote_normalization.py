import unittest
from decimal import Decimal

from arb_monitor.core.quote_normalization import (
    QuoteSemantic, compare_to_spot_hkd, normalize_price,
)


class QuoteNormalizationTests(unittest.TestCase):
    def test_absolute_hkd_price_is_compared_directly(self):
        p = normalize_price("binance", "HK0700USDT", Decimal("42"),
                            QuoteSemantic.HKD_ABSOLUTE)
        self.assertEqual(p.hkd_price, Decimal("42"))
        self.assertEqual(compare_to_spot_hkd(p, Decimal("40")), Decimal("2"))

    def test_usd_price_is_converted_using_observed_fx(self):
        p = normalize_price("binance", "TENCENTUSDT", Decimal("5"),
                            QuoteSemantic.USD_CONVERT_TO_HKD,
                            fx_usd_hkd=Decimal("7.8"))
        self.assertEqual(p.hkd_price, Decimal("39.0"))

    def test_usd_conversion_requires_fx(self):
        with self.assertRaises(ValueError):
            normalize_price("okx", "X", Decimal("5"),
                            QuoteSemantic.USD_CONVERT_TO_HKD)


if __name__ == "__main__":
    unittest.main()
