import io
import os
import unittest
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

import amazon_spend as az


def opener(text):
    @contextmanager
    def op():
        yield io.StringIO(text)
    return op


class ToDecimalTest(unittest.TestCase):
    def test_us_style(self):
        self.assertEqual(az.to_decimal("1,234.56"), Decimal("1234.56"))
        self.assertEqual(az.to_decimal("0.0"), Decimal("0"))

    def test_european_style(self):
        self.assertEqual(az.to_decimal("1.234,56"), Decimal("1234.56"))
        self.assertEqual(az.to_decimal("12,34"), Decimal("12.34"))
        self.assertEqual(az.to_decimal("-1,50"), Decimal("-1.50"))

    def test_thousands_groups(self):
        self.assertEqual(az.to_decimal("1,234"), Decimal("1234"))
        self.assertEqual(az.to_decimal("1,234,567"), Decimal("1234567"))

    def test_noise_and_garbage(self):
        self.assertEqual(az.to_decimal("€ 1 234,56"), Decimal("1234.56"))
        self.assertIsNone(az.to_decimal("Not Applicable"))
        self.assertIsNone(az.to_decimal(""))
        self.assertIsNone(az.to_decimal("abc"))


class ToDateTest(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(az.to_date("2024-03-05"), date(2024, 3, 5))
        self.assertEqual(az.to_date("2024-03-05T21:54:00Z"), date(2024, 3, 5))

    def test_garbage(self):
        self.assertIsNone(az.to_date("March 5, 2024"))
        self.assertIsNone(az.to_date(""))


class ClassifyTest(unittest.TestCase):
    def test_new_layout(self):
        self.assertEqual(az.classify("Order History.csv"), "retail")
        self.assertEqual(az.classify("Digital Content Orders.csv"), "digital_money")
        self.assertEqual(az.classify("Refund Details.csv"), "refunds")

    def test_legacy_layout(self):
        self.assertEqual(az.classify("retail.orderhistory.csv"), "retail")
        self.assertEqual(az.classify("retail.ordersreturned.payments.csv"), "refunds")
        self.assertEqual(az.classify("digital orders monetary.csv"), "digital_money")
        self.assertEqual(az.classify("digital orders.csv"), "digital_dates")

    def test_other(self):
        self.assertIsNone(az.classify("Delivery Photos.csv"))
        self.assertIsNone(az.classify("notes.txt"))


class ScanCsvTest(unittest.TestCase):
    def test_retail_parses_and_normalizes_headers(self):
        text = ('order id,order date,currency,total amount,product name\n'
                '111,2020-01-02T10:00:00Z,EUR,"12,34",Widget\n')
        data = az.Data()
        az._scan_csv(data, "retail", opener(text))
        self.assertEqual(data.retail[("111", date(2020, 1, 2), "EUR", Decimal("12.34"), "Widget")], 1)

    def test_retail_dedup_takes_max_across_files(self):
        row = "111,2020-01-02,EUR,12.34,Widget\n"
        header = "Order ID,Order Date,Currency,Total Amount,Product Name\n"
        data = az.Data()
        az._scan_csv(data, "retail", opener(header + row))
        az._scan_csv(data, "retail", opener(header + row * 3))
        self.assertEqual(data.retail[("111", date(2020, 1, 2), "EUR", Decimal("12.34"), "Widget")], 3)

    def test_refunds_require_completed_status(self):
        header = "Order ID,Refund Date,Currency,Refund Amount,Payment Status\n"
        text = header + "222,2021-05-06,EUR,10.00,Pending\n" + "223,2021-05-07,EUR,5.00,Completed\n"
        data = az.Data()
        az._scan_csv(data, "refunds", opener(text))
        self.assertEqual(len(data.refunds), 1)
        self.assertIn(("223", date(2021, 5, 7), "EUR", Decimal("5.00")), data.refunds)

    def test_deferred_digital_uses_packet_info_preferring_dated_records(self):
        dates_undated = "DeliveryPacketId,OrderId\nP1,O1\n"
        dates_dated = "DeliveryPacketId,Order Date\nP1,2024-02-03\n"
        money = "Delivery Packet ID,Transaction Amount,Base Currency Code,Order ID\nP1,5.00,USD,\n"
        data = az.Data()
        az._scan_csv(data, "digital_dates", opener(dates_undated))
        az._scan_csv(data, "digital_dates", opener(dates_dated))
        az._scan_csv(data, "digital_money", opener(money))
        self.assertEqual(data.digital[("O1", date(2024, 2, 3), "USD", Decimal("5.00"))], 1)


class RatesTest(unittest.TestCase):
    def make_rates(self, target="EUR"):
        rates = az.Rates(target)
        rates.series["GBP"] = (
            [date(2020, 1, 1), date(2020, 6, 1)],
            [Decimal("1.20"), Decimal("1.10")],
        )
        return rates

    def test_exact_and_nearest_prior_day(self):
        rates = self.make_rates()
        self.assertEqual(rates.get("GBP", date(2020, 6, 1)), Decimal("1.10"))
        self.assertEqual(rates.get("GBP", date(2020, 3, 1)), Decimal("1.20"))

    def test_cross_rate_to_non_eur_target_tracks_fallback(self):
        rates = self.make_rates("USD")
        expected = Decimal("1.20") / Decimal("0.92")
        self.assertEqual(rates.get("GBP", date(2020, 1, 1)), expected)
        self.assertIn("USD", rates.approx_used)

    def test_unknown_currency_returns_none(self):
        rates = self.make_rates()
        self.assertIsNone(rates.get("JPY", date(2020, 1, 1)))

    def test_target_is_one(self):
        rates = self.make_rates()
        self.assertEqual(rates.get("EUR", date(2020, 1, 1)), Decimal("1"))


class FormatTest(unittest.TestCase):
    def setUp(self):
        az.setup_locale("C")

    def test_fmt_c_locale_groups_thousands(self):
        self.assertEqual(az.fmt(Decimal("1234567.891")), "1,234,567.89")
        self.assertEqual(az.fmt(Decimal("-5")), "-5.00")

    def test_money_prefix_locale(self):
        self.assertEqual(az.money(Decimal("12.345"), "EUR"), "€12.35")
        self.assertEqual(az.money(Decimal("-5"), "USD"), "-$5.00")

    def test_zero_decimal_currencies(self):
        self.assertEqual(az.money(Decimal("3360317.49"), "JPY"), "¥3,360,317")
        self.assertEqual(az.money(Decimal("-3360317.49"), "JPY"), "-¥3,360,317")

    def test_setup_locale_rejects_garbage_and_keeps_fallback(self):
        self.assertTrue(az.setup_locale("C"))
        self.assertFalse(az.LOCALE_READY)
        self.assertFalse(az.setup_locale("definitely_not_a_locale_XYZ"))


class CacheDirTest(unittest.TestCase):
    def test_linux_uses_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/xdg"}, clear=True), \
                mock.patch("sys.platform", "linux"):
            self.assertEqual(az._cache_dir(), Path("/xdg/amzn-orders"))

    def test_windows_uses_localappdata(self):
        env = {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.platform", "win32"):
            self.assertEqual(az._cache_dir(), Path(env["LOCALAPPDATA"]) / "amzn-orders")

    def test_macos_uses_library_caches(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.platform", "darwin"):
            self.assertEqual(az._cache_dir(), Path.home() / "Library" / "Caches" / "amzn-orders")

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"AMZN_ORDERS_CACHE": "/custom"}, clear=True), \
                mock.patch("sys.platform", "win32"):
            self.assertEqual(az._cache_dir(), Path("/custom"))


if __name__ == "__main__":
    unittest.main()
