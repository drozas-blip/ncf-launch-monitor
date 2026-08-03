#!/usr/bin/env python3
"""Regenerate the dashboard's embedded data from live sources, in place.

Reads ../index.html, refreshes the data blocks it knows how to rebuild, and
rewrites the file. Designed to run daily in CI (GitHub Actions) and locally.

Coverage (v1): trial cohort (SESS) + "last updated" timestamp — from Metabase.
Extending: add a builder in metrics.py and a corresponding replace_* below.
Each block is delimited so replacement is exact and layout edits stay untouched.
"""
import os
import re
import datetime

import lib
import metrics

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Madrid")
except Exception:  # pragma: no cover - fallback if tzdata missing
    TZ = datetime.timezone.utc

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.normpath(os.path.join(HERE, "..", "index.html"))

SESS_ORDER = ["trials", "seen", "scheduled", "stuck", "gone",
              "unsub", "unsubAfterDr", "stNoshow", "stNever", "stCancel"]
# vs-old baseline / not-yet-scripted fields — preserved from the current file
SESS_KEEP = ["nsRateN", "nsRateO", "toConsultN", "toConsultO"]


def _sub_once(pattern, repl, text, flags=0):
    """re.sub with a literal replacement (no backref interpretation)."""
    return re.subn(pattern, lambda m: repl, text, count=1, flags=flags)


def replace_sess(html, sess):
    m = re.search(r"var SESS=\{(.*?)\};", html, re.S)
    keep = {}
    if m:
        for k in SESS_KEEP:
            mm = re.search(rf"{k}:([0-9.]+)", m.group(1))
            if mm:
                keep[k] = mm.group(1)
    parts = [f"{k}:{sess[k]}" for k in SESS_ORDER]
    parts += [f"{k}:{keep.get(k, 0)}" for k in SESS_KEEP]
    literal = "var SESS={" + ",".join(parts) + "};"
    out, n = _sub_once(r"var SESS=\{.*?\};", literal, html, re.S)
    return out, n


def replace_timestamp(html, now):
    date_s = now.strftime("%-d %b %Y")
    time_s = now.strftime("%H:%M %Z")
    out, n = _sub_once(
        r"Last updated <b>.*?</b> · [^<]*",
        f"Last updated <b>{date_s}</b> · {time_s}",
        html,
    )
    return out, (date_s, time_s), n


def main():
    lib.load_secrets()
    mb = lib.Metabase()

    sess = metrics.trial_cohort(mb)

    with open(HTML_PATH, encoding="utf-8") as fh:
        html = fh.read()
    original = html

    html, n_sess = replace_sess(html, sess)
    now = datetime.datetime.now(TZ)
    html, stamp, n_ts = replace_timestamp(html, now)

    if n_sess != 1:
        raise SystemExit("ERROR: SESS block not found/replaced")
    if n_ts != 1:
        raise SystemExit("ERROR: timestamp not found/replaced")

    if html == original:
        print("No change.")
        return

    with open(HTML_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Regenerated {HTML_PATH}")
    print(f"  trial cohort: {sess}")
    print(f"  last updated: {stamp[0]} · {stamp[1]}")


if __name__ == "__main__":
    main()
