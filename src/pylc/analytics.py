"""Descriptive analytics over recorded PYLC telemetry samples.

The scan engine already publishes its own health as ordinary tags
(``Watchdog``, ``LastScanUs``, ``MaxScanUs``) and programs count
production into tags like ``PartsCounter``. This module is the honest
first slice of the "predictive analytics" the README promises: a
logger (``sample_tags.py``) records those tags to CSV, and the
functions here summarize the recording — scan-time distribution,
jitter, implied scan rate, throughput — and raise plain-language
warning flags when a least-squares trend or a first/second-half
comparison says something is getting worse.

That is all it is: descriptive statistics and trend flags over
recorded samples. There is no machine learning here and no claim to
predict failures — the flags say "this measured number is moving the
wrong way", with the number attached, so a human can go look.

Pure standard library, like the rest of the runtime core.
"""

import csv
import math
import statistics

SLOW_SCAN_THRESHOLD_US = 1000   # a scan evaluation over 1 ms is "slow"
SCAN_TREND_FLAG_US_PER_MIN = 5.0
THROUGHPUT_DROP_FRACTION = 0.80  # second half < 80% of first half flags

_FIELDS = ("timestamp", "watchdog", "last_scan_us", "max_scan_us", "parts")


# ==========================================
# Loading
# ==========================================
def load_samples(path):
    """Load a samples CSV written by sample_tags.py (or by hand).

    Expected header: ``timestamp,watchdog,last_scan_us,max_scan_us,parts``
    with timestamp as float epoch seconds. Extra columns are ignored;
    rows that fail to parse are skipped. Returns sample dicts sorted
    by timestamp.
    """
    samples = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                sample = {
                    "timestamp": float(row["timestamp"]),
                    "watchdog": int(float(row["watchdog"])),
                    "last_scan_us": int(float(row["last_scan_us"])),
                    "max_scan_us": int(float(row["max_scan_us"])),
                    "parts": int(float(row["parts"])),
                }
            except (KeyError, TypeError, ValueError):
                continue  # malformed row: skip, don't sink the whole log
            samples.append(sample)
    samples.sort(key=lambda s: s["timestamp"])
    return samples


# ==========================================
# Small statistics helpers
# ==========================================
def _percentile(sorted_values, pct):
    """Nearest-rank percentile of an already-sorted sequence."""
    if not sorted_values:
        return None
    rank = math.ceil(pct / 100.0 * len(sorted_values))
    return sorted_values[min(len(sorted_values), max(1, rank)) - 1]


def trend_per_minute(values, timestamps):
    """Least-squares slope of values against time, per minute.

    Returns 0.0 for degenerate input (fewer than 2 points, or all
    timestamps identical) rather than pretending a trend exists.
    """
    if len(values) < 2 or len(values) != len(timestamps):
        return 0.0
    t0 = timestamps[0]
    minutes = [(t - t0) / 60.0 for t in timestamps]
    n = len(values)
    mean_t = sum(minutes) / n
    mean_v = sum(values) / n
    denom = sum((t - mean_t) ** 2 for t in minutes)
    if denom == 0:
        return 0.0
    numer = sum((t - mean_t) * (v - mean_v) for t, v in zip(minutes, values))
    return numer / denom


# ==========================================
# Scan health
# ==========================================
def scan_health(samples):
    """Summarize scan-evaluation health over the recorded samples."""
    health = {
        "sample_count": len(samples),
        "duration_s": 0.0,
        "last_scan_us_mean": None,
        "last_scan_us_p95": None,
        "last_scan_us_max": None,
        "last_scan_us_jitter": None,
        "slow_scan_count": 0,
        "slow_scan_threshold_us": SLOW_SCAN_THRESHOLD_US,
        "watchdog_delta": 0,
        "scan_rate_hz": None,
    }
    if not samples:
        return health

    evals = [s["last_scan_us"] for s in samples]
    health["last_scan_us_mean"] = statistics.fmean(evals)
    health["last_scan_us_p95"] = _percentile(sorted(evals), 95)
    health["last_scan_us_max"] = max(evals)
    health["last_scan_us_jitter"] = statistics.pstdev(evals)
    health["slow_scan_count"] = sum(
        1 for v in evals if v > SLOW_SCAN_THRESHOLD_US
    )

    health["watchdog_delta"] = samples[-1]["watchdog"] - samples[0]["watchdog"]
    duration = samples[-1]["timestamp"] - samples[0]["timestamp"]
    health["duration_s"] = duration
    if duration > 0:
        health["scan_rate_hz"] = health["watchdog_delta"] / duration
    return health


# ==========================================
# Production
# ==========================================
def _segment_rate(segment):
    """Parts/hour across one segment of samples, or None if unknowable."""
    if len(segment) < 2:
        return None
    duration = segment[-1]["timestamp"] - segment[0]["timestamp"]
    if duration <= 0:
        return None
    return (segment[-1]["parts"] - segment[0]["parts"]) / duration * 3600.0


def production(samples):
    """Summarize a parts counter over the recorded samples.

    The counter is cumulative, so totals are last-minus-first deltas.
    First/second halves are split at the midpoint in time; the two
    segments share the boundary sample so their deltas add up to the
    total.
    """
    prod = {
        "total_parts": 0,
        "duration_s": 0.0,
        "parts_per_hour": None,
        "first_half_parts_per_hour": None,
        "second_half_parts_per_hour": None,
    }
    if len(samples) < 2:
        return prod

    prod["total_parts"] = samples[-1]["parts"] - samples[0]["parts"]
    prod["duration_s"] = samples[-1]["timestamp"] - samples[0]["timestamp"]
    prod["parts_per_hour"] = _segment_rate(samples)

    mid_time = (samples[0]["timestamp"] + samples[-1]["timestamp"]) / 2.0
    split = 0
    for i, s in enumerate(samples):
        if s["timestamp"] <= mid_time:
            split = i
    prod["first_half_parts_per_hour"] = _segment_rate(samples[:split + 1])
    prod["second_half_parts_per_hour"] = _segment_rate(samples[split:])
    return prod


# ==========================================
# Full report
# ==========================================
def analyze(samples):
    """Run the full analysis: health, production, trend, and flags."""
    health = scan_health(samples)
    prod = production(samples)
    trend = trend_per_minute(
        [s["last_scan_us"] for s in samples],
        [s["timestamp"] for s in samples],
    )

    flags = []
    if trend > SCAN_TREND_FLAG_US_PER_MIN:
        flags.append(
            f"scan evaluation time is trending up ({trend:+.1f} µs/min)"
        )
    first = prod["first_half_parts_per_hour"]
    second = prod["second_half_parts_per_hour"]
    if first is not None and first > 0 and second is not None:
        if second < THROUGHPUT_DROP_FRACTION * first:
            drop_pct = (1.0 - second / first) * 100.0
            flags.append(f"throughput dropped ~{drop_pct:.0f}% in the second half")

    return {
        "scan_health": health,
        "production": prod,
        "scan_trend_us_per_min": trend,
        "flags": flags,
    }


# ==========================================
# Reporting
# ==========================================
def _fmt(value, digits=1, suffix=""):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


def format_report(report):
    """Render an analyze() report as tidy plain text (label: value)."""
    h = report["scan_health"]
    p = report["production"]
    lines = [
        "PYLC analytics report",
        "=" * 21,
        f"{'Samples':<26} {h['sample_count']}",
        f"{'Duration':<26} {_fmt(h['duration_s'], 1, ' s')}",
        f"{'Mean scan eval':<26} {_fmt(h['last_scan_us_mean'], 1, ' µs')}",
        f"{'p95 scan eval':<26} {_fmt(h['last_scan_us_p95'], 0, ' µs')}",
        f"{'Max scan eval':<26} {_fmt(h['last_scan_us_max'], 0, ' µs')}",
        f"{'Scan jitter (pop. stdev)':<26} {_fmt(h['last_scan_us_jitter'], 1, ' µs')}",
        f"{'Slow scans (>1000 µs)':<26} {h['slow_scan_count']}",
        f"{'Implied scan rate':<26} {_fmt(h['scan_rate_hz'], 1, ' Hz')}",
        f"{'Scan eval trend':<26} {report['scan_trend_us_per_min']:+.1f} µs/min",
        f"{'Total parts':<26} {p['total_parts']}",
        f"{'Throughput':<26} {_fmt(p['parts_per_hour'], 1, ' parts/hour')}",
        f"{'  first half':<26} {_fmt(p['first_half_parts_per_hour'], 1, ' parts/hour')}",
        f"{'  second half':<26} {_fmt(p['second_half_parts_per_hour'], 1, ' parts/hour')}",
    ]
    if report["flags"]:
        lines.append(f"{'Flags':<26} {len(report['flags'])} warning(s)")
        lines.extend(f"  - {flag}" for flag in report["flags"])
    else:
        lines.append(f"{'Flags':<26} none")
    return "\n".join(lines)
