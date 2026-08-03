#!/usr/bin/env python3
"""Regenerate every live data block in ../index.html from the real sources.

Sources: Metabase (business DB), Mixpanel (EU), Typeform, Customer.io.
Old/baseline (May–June) values are static and left untouched; only the new-flow
numbers + the "last updated" stamp are rewritten. Safe to run daily in CI.
"""
import os
import re
import json
import datetime

import lib
import metrics

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Madrid")
except Exception:
    TZ = datetime.timezone.utc

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.normpath(os.path.join(HERE, "..", "index.html"))
LAUNCH_MMDD = "07-20"


# ---- surgical helpers --------------------------------------------------------
def _after(html, before_regex, value, label):
    """Replace the number immediately after `before_regex` (kept verbatim)."""
    pat = "(" + before_regex + r")([\d.]+)"
    new, n = re.subn(pat, lambda m: m.group(1) + str(value), html, count=1)
    if n != 1:
        raise SystemExit(f"regen: could not place [{label}]")
    return new


def bf(html, name, value):
    return _after(html, 'n:"' + re.escape(name) + '",v:', value, "BF:" + name)


def stepx(html, label, key, value):
    return _after(html, 's:"' + re.escape(label) + '"[^}]*?' + key + ":", value, f"STEPX:{label}.{key}")


def verdict(html, label, value):
    return _after(html, 'l:"' + re.escape(label) + '",n:', value, "VERDICT:" + label)


def replace_block(html, pattern, literal, label):
    new, n = re.subn(pattern, lambda m: literal, html, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f"regen: block not found [{label}]")
    return new


# ---- main --------------------------------------------------------------------
def main():
    lib.load_secrets()
    mb, mp, tf, cio = lib.Metabase(), lib.Mixpanel(), lib.Typeform(), lib.CustomerIO()
    today = datetime.datetime.now(TZ).date()
    frm, to = metrics.LAUNCH_DATE, today.isoformat()

    cohort = metrics.trial_cohort(mb)
    fn = metrics.funnel_db(mb)
    quiz = metrics.typeform_quiz(tf)
    steps = metrics.mixpanel_steps(mp, frm, to)
    vd = metrics.verdict_new(fn, quiz)
    opens_d = metrics.mixpanel_opens_daily(mp, frm, to)
    comp_d = metrics.mixpanel_completes_daily(mp, frm, to)
    trend_db = metrics.trend_db_daily(mb)
    emails = metrics.emails_cio(cio)
    elig_rate = (quiz["eligible"] / quiz["completed"]) if quiz["completed"] else 0

    with open(HTML_PATH, encoding="utf-8") as fh:
        html = original = fh.read()

    # 1) trial cohort (SESS) — preserve the vs-old baseline fields
    keep = {}
    m = re.search(r"var SESS=\{(.*?)\};", html, re.S)
    for k in ("nsRateN", "nsRateO", "toConsultN", "toConsultO"):
        mm = m and re.search(rf"{k}:([0-9.]+)", m.group(1))
        keep[k] = mm.group(1) if mm else 0
    sess_fields = ["trials", "seen", "scheduled", "stuck", "gone", "unsub",
                   "unsubAfterDr", "stNoshow", "stNever", "stCancel"]
    sess_lit = "var SESS={" + ",".join(f"{k}:{cohort[k]}" for k in sess_fields) + \
        "," + ",".join(f"{k}:{keep[k]}" for k in keep) + "};"
    html = replace_block(html, r"var SESS=\{.*?\};", sess_lit, "SESS")

    # 2) full funnel (BF) — new-flow real rows
    google = max(steps["reg_completed"] - steps["otp_verified"], 0)
    for name, val in [
        ("Lead form opened", quiz["opened"]),
        ("Lead form completed", quiz["completed"]),
        ("↳ Eligible", quiz["eligible"]),
        ("↳ Not eligible", quiz["completed"] - quiz["eligible"]),
        ("Registration started", steps["reg_started"]),
        ("↳ email — reached OTP", steps["otp_sent"]),
        ("↳ email — OTP verified", steps["otp_verified"]),
        ("↳ Google sign-in (skips OTP)", google),
        ("Account created", fn["accounts"]),
        ("Name step started", steps["name_started"]),
        ("Name step completed", steps["name_completed"]),
        ("Trial started", fn["trials"]),
        ("In trial (active)", fn["in_trial"]),
        ("Cancelled during trial", fn["cancelled"]),
        ("Booked a consult", fn["booked"]),
        ("Meeting done", fn["meeting_done"]),
        ("Installed the app", fn["installed"]),
    ]:
        html = bf(html, name, val)

    # 3) step completion (STEPX) — new sides
    for label, ns, nf in [
        ("Quiz — opened → completed", quiz["opened"], quiz["completed"]),
        ("Registration — started → account", steps["reg_started"], fn["accounts"]),
        ("OTP — code sent → verified", steps["otp_sent"], steps["otp_verified"]),
        ("Name — started → completed", steps["name_started"], steps["name_completed"]),
    ]:
        html = stepx(html, label, "nS", ns)
        html = stepx(html, label, "nF", nf)

    # 4) verdict — new sides
    html = verdict(html, "Form completion — opened → completed", vd["form"])
    html = verdict(html, "Activation — free trial vs paid sub", vd["activation"])
    html = verdict(html, "Consult booking", vd["booking"])
    html = verdict(html, "App install", vd["app"])

    # 5) eligibility (QUIZ)
    b = quiz["buckets"]
    quiz_lit = ("var QUIZ=[{n:\"Eligible ✓\",v:%d,c:\"g\"},"
                "{n:\"BMI < 27\",v:%d,c:\"c\",k:1},"
                "{n:\"BMI 27–29, no comorbidity\",v:%d,c:\"c\",k:1},"
                "{n:\"Contraindication\",v:%d,c:\"w\"},"
                "{n:\"Under 18\",v:%d,c:\"w\"},"
                "{n:\"Pregnant / breastfeeding\",v:%d,c:\"w\"}];") % (
        b["eligible"], b["bmi_lt27"], b["bmi_2729"], b["contra"], b["under18"], b["pregnant"])
    html = replace_block(html, r"var QUIZ=\[.*?\];", quiz_lit, "QUIZ")
    html = _after(html, r"var QTOT=", quiz["completed"], "QTOT")

    # 6) daily trend (TREND) — merge new-flow side, append new days
    tm = re.search(r"(var TREND\s*=\s*)(\[.*?\])(;)", html, re.S)
    series = json.loads(tm.group(2))
    by_date = {e["d"]: e for e in series}
    all_days = set(opens_d) | set(comp_d) | set(trend_db)
    for d in sorted(all_days):
        if d < LAUNCH_MMDD:
            continue
        e = by_date.get(d)
        if e is None:
            e = {"d": d, "o": {"op": 0, "ld": 0, "el": 0, "ac": 0, "acR": 0, "tr": 0, "bk": 0},
                 "n": {"op": 0, "ld": 0, "el": 0, "ac": 0, "acR": 0, "tr": 0, "bk": 0}}
            series.append(e)
            by_date[d] = e
        db = trend_db.get(d, {})
        ld = comp_d.get(d, 0)
        e["n"].update({
            "op": opens_d.get(d, 0),
            "ld": ld,
            "el": round(ld * elig_rate),
            "ac": db.get("ac", 0),
            "tr": db.get("tr", 0),
            "bk": db.get("bk", 0),
        })
    series.sort(key=lambda e: e["d"])
    trend_lit = tm.group(1) + json.dumps(series, separators=(",", ":"), ensure_ascii=False) + tm.group(3)
    html = replace_block(html, r"var TREND\s*=\s*\[.*?\];", trend_lit, "TREND")

    # 7) email campaigns (EMAIL)
    def em_obj(e):
        return ("{id:%d,n:%s,d:%s,sent:%d,dl:%d,o:%d,c:%d,cv:%d,u:%d}" % (
            e["id"], json.dumps(e["n"], ensure_ascii=False), json.dumps(e["d"], ensure_ascii=False),
            e["sent"], e["dl"], e["o"], e["c"], e["cv"], e["u"]))
    email_lit = "var EMAIL=[\n    " + ",\n    ".join(em_obj(e) for e in emails) + "];"
    html = replace_block(html, r"var EMAIL=\[.*?\];", email_lit, "EMAIL")

    # 8) MQL denominator label (new-side eligible leads)
    html = re.sub(r"(· new )\d+( · old 1379)",
                  lambda m: m.group(1) + str(quiz["eligible"]) + m.group(2), html, count=1)

    # 9) last-updated stamp
    stamp = datetime.datetime.now(TZ)
    html = re.sub(r"Last updated <b>.*?</b> · [^<]*",
                  f"Last updated <b>{stamp.strftime('%-d %b %Y')}</b> · {stamp.strftime('%H:%M %Z')}",
                  html, count=1)

    if html == original:
        print("No change.")
        return
    with open(HTML_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("Regenerated", HTML_PATH)
    print("  cohort ", cohort)
    print("  funnel ", fn)
    print("  quiz   ", {k: quiz[k] for k in ("opened", "completed", "eligible")})
    print("  verdict", vd, "| steps", steps)
    print("  emails ", len(emails), "campaigns · sent", sum(e["sent"] for e in emails))
    print("  updated", stamp.strftime("%-d %b %Y · %H:%M %Z"))


if __name__ == "__main__":
    main()
