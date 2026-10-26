import os
import sys
import tempfile
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from pylc.analytics import (  # noqa: E402
    analyze, format_report, load_samples, production, scan_health,
    trend_per_minute,
)


def sample(ts, watchdog=0, last=100, max_us=100, parts=0):
    return {
        "timestamp": float(ts),
        "watchdog": watchdog,
        "last_scan_us": last,
        "max_scan_us": max_us,
        "parts": parts,
    }


class TestLoadSamples(unittest.TestCase):
    def test_parses_sorts_and_skips_malformed(self):
        csv_text = (
            "timestamp,watchdog,last_scan_us,max_scan_us,parts,extra\n"
            "1001.0,600,43,135,8,y\n"
            "bogus,row,here\n"
            "1002.0,700,44,140,9,z\n"
            "1000.0,500,42,135,7,x\n"
        )
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "samples.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write(csv_text)
            samples = load_samples(path)
        self.assertEqual(len(samples), 3)
        self.assertEqual([s["timestamp"] for s in samples], [1000.0, 1001.0, 1002.0])
        self.assertEqual(samples[0]["watchdog"], 500)
        self.assertEqual(samples[-1]["parts"], 9)
        self.assertIsInstance(samples[0]["last_scan_us"], int)


class TestScanHealth(unittest.TestCase):
    def test_percentile_mean_jitter_on_known_set(self):
        samples = [sample(i, last=i + 1) for i in range(100)]  # evals 1..100 µs
        health = scan_health(samples)
        self.assertEqual(health["sample_count"], 100)
        self.assertAlmostEqual(health["last_scan_us_mean"], 50.5)
        self.assertEqual(health["last_scan_us_p95"], 95)  # nearest-rank
        self.assertEqual(health["last_scan_us_max"], 100)
        # population stdev of 1..100 = sqrt((100^2 - 1) / 12)
        self.assertAlmostEqual(health["last_scan_us_jitter"], 833.25 ** 0.5, places=6)

    def test_slow_scan_count_and_rate(self):
        samples = [
            sample(0.0, watchdog=0, last=100),
            sample(1.0, watchdog=100, last=1500),
            sample(2.0, watchdog=200, last=2000),
            sample(3.0, watchdog=300, last=100),
        ]
        health = scan_health(samples)
        self.assertEqual(health["slow_scan_count"], 2)
        self.assertEqual(health["watchdog_delta"], 300)
        self.assertAlmostEqual(health["scan_rate_hz"], 100.0)


class TestConstantSeries(unittest.TestCase):
    """Flat scan times, steady production: nothing may flag."""

    def setUp(self):
        self.samples = [
            sample(i * 1.0, watchdog=1000 + 100 * i, last=100, max_us=100,
                   parts=10 * i)
            for i in range(20)
        ]

    def test_health(self):
        health = scan_health(self.samples)
        self.assertEqual(health["last_scan_us_mean"], 100.0)
        self.assertEqual(health["last_scan_us_p95"], 100)
        self.assertEqual(health["last_scan_us_jitter"], 0.0)
        self.assertEqual(health["slow_scan_count"], 0)
        self.assertAlmostEqual(health["scan_rate_hz"], 100.0)

    def test_production(self):
        prod = production(self.samples)
        self.assertEqual(prod["total_parts"], 190)
        self.assertAlmostEqual(prod["parts_per_hour"], 36000.0)
        self.assertAlmostEqual(prod["first_half_parts_per_hour"], 36000.0)
        self.assertAlmostEqual(prod["second_half_parts_per_hour"], 36000.0)

    def test_trend_flat_and_no_flags(self):
        report = analyze(self.samples)
        self.assertEqual(report["scan_trend_us_per_min"], 0.0)
        self.assertEqual(report["flags"], [])


class TestRisingScanTimes(unittest.TestCase):
    def test_slope_and_degradation_flag(self):
        # +10 µs every 6 s of samples = +100 µs/min exactly.
        samples = [
            sample(i * 6.0, watchdog=100 * i, last=100 + 10 * i,
                   max_us=100 + 10 * i)
            for i in range(60)
        ]
        trend = trend_per_minute(
            [s["last_scan_us"] for s in samples],
            [s["timestamp"] for s in samples],
        )
        self.assertAlmostEqual(trend, 100.0, places=6)
        report = analyze(samples)
        self.assertEqual(len(report["flags"]), 1)
        self.assertIn("scan evaluation time is trending up (+100.0 µs/min)",
                      report["flags"][0])


class TestThroughputDrop(unittest.TestCase):
    def test_second_half_drop_flag(self):
        # 10 s spacing; +10 parts/sample for 10 samples, then +1/sample.
        parts = [10 * i if i <= 10 else 100 + (i - 10) for i in range(20)]
        samples = [
            sample(i * 10.0, watchdog=100 * i, last=100, parts=parts[i])
            for i in range(20)
        ]
        prod = production(samples)
        self.assertEqual(prod["total_parts"], 109)
        self.assertAlmostEqual(prod["first_half_parts_per_hour"], 3600.0)
        self.assertAlmostEqual(prod["second_half_parts_per_hour"], 684.0)
        report = analyze(samples)
        self.assertEqual(len(report["flags"]), 1)
        self.assertIn("throughput dropped ~81% in the second half",
                      report["flags"][0])


class TestDegenerate(unittest.TestCase):
    def test_single_sample(self):
        samples = [sample(1000.0, watchdog=42, last=77, parts=5)]
        health = scan_health(samples)
        self.assertEqual(health["last_scan_us_mean"], 77.0)
        self.assertEqual(health["last_scan_us_p95"], 77)
        self.assertEqual(health["last_scan_us_jitter"], 0.0)
        self.assertIsNone(health["scan_rate_hz"])
        prod = production(samples)
        self.assertEqual(prod["total_parts"], 0)
        self.assertIsNone(prod["parts_per_hour"])
        self.assertEqual(analyze(samples)["flags"], [])

    def test_identical_timestamps(self):
        samples = [sample(100.0, watchdog=100 * i, last=10 * (i + 1), parts=i)
                   for i in range(5)]
        self.assertEqual(
            trend_per_minute([s["last_scan_us"] for s in samples],
                             [s["timestamp"] for s in samples]),
            0.0,
        )
        report = analyze(samples)
        self.assertIsNone(report["production"]["parts_per_hour"])
        self.assertIsNone(report["scan_health"]["scan_rate_hz"])
        self.assertEqual(report["flags"], [])

    def test_empty(self):
        report = analyze([])
        self.assertEqual(report["flags"], [])
        self.assertEqual(report["scan_health"]["sample_count"], 0)


class TestFormatReport(unittest.TestCase):
    def test_report_lines(self):
        samples = [
            sample(i * 1.0, watchdog=100 * i, last=100, parts=10 * i)
            for i in range(20)
        ]
        text = format_report(analyze(samples))
        self.assertIn("PYLC analytics report", text)
        self.assertIn("Samples", text)
        self.assertIn("Total parts", text)
        self.assertIn("190", text)
        self.assertTrue(text.rstrip().endswith("none"))


if __name__ == "__main__":
    unittest.main()
