#!/usr/bin/env python3
"""Record PYLC telemetry samples from a running demo to a CSV log.

Polls the demo's /api/tags endpoint and appends one row per poll with
the engine's telemetry tags (Watchdog, LastScanUs, MaxScanUs) and the
program's parts counter (PartsCounter) — the raw material for
pylc.analytics (see analyze_log.py).

Usage:
    python3 sample_tags.py --url http://127.0.0.1:8085 \
        --out samples.csv --interval 1.0 [--count N]

Without --count it runs until Ctrl-C. The CSV gets its header the
first time the file is created.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

HEADER = ["timestamp", "watchdog", "last_scan_us", "max_scan_us", "parts"]


def poll_tags(url):
    """Fetch one tag snapshot dict from the demo's /api/tags."""
    with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))["tags"]


def main():
    ap = argparse.ArgumentParser(description="Record PYLC telemetry samples to CSV.")
    ap.add_argument("--url", default="http://127.0.0.1:8085",
                    help="base URL of the running demo (default %(default)s)")
    ap.add_argument("--out", default="samples.csv",
                    help="CSV file to append to (default %(default)s)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between polls (default %(default)s)")
    ap.add_argument("--count", type=int, default=None,
                    help="stop after this many samples (default: run until Ctrl-C)")
    args = ap.parse_args()

    need_header = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
    recorded = 0
    print(f"sampling {args.url}/api/tags every {args.interval}s -> {args.out}"
          + (f" ({args.count} samples)" if args.count else " (Ctrl-C to stop)"))
    try:
        with open(args.out, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if need_header:
                writer.writerow(HEADER)
                f.flush()
            while args.count is None or recorded < args.count:
                try:
                    tags = poll_tags(args.url)
                except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                    print(f"  poll failed: {e}", file=sys.stderr)
                    time.sleep(args.interval)
                    continue
                writer.writerow([
                    f"{time.time():.3f}",
                    tags.get("Watchdog", ""),
                    tags.get("LastScanUs", ""),
                    tags.get("MaxScanUs", ""),
                    tags.get("PartsCounter", ""),
                ])
                f.flush()  # a Ctrl-C must never lose recorded samples
                recorded += 1
                print(f"  sample {recorded}: watchdog={tags.get('Watchdog')} "
                      f"last_scan_us={tags.get('LastScanUs')} "
                      f"parts={tags.get('PartsCounter')}")
                if args.count is None or recorded < args.count:
                    time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    print(f"recorded {recorded} sample(s) in {args.out}")


if __name__ == "__main__":
    main()
