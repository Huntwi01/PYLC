#!/usr/bin/env python3
"""Analyze a PYLC telemetry samples CSV and print the report.

Usage:
    python3 analyze_log.py samples.csv

The CSV is written by sample_tags.py (or any logger producing the
same columns). See pylc.analytics for what the numbers mean — and
for what they don't claim to mean.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from pylc.analytics import analyze, format_report, load_samples  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Analyze a PYLC telemetry samples CSV.")
    ap.add_argument("samples_csv", help="path to a samples CSV from sample_tags.py")
    args = ap.parse_args()

    samples = load_samples(args.samples_csv)
    if not samples:
        print(f"no valid samples in {args.samples_csv}", file=sys.stderr)
        return 1
    print(format_report(analyze(samples)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
