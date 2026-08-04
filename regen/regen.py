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

    # Pull each source independently — a failure (e.g. rate limit) keeps that
    # block's last committed values instead of failing the whole run.
    warns = []

    def T(fn_, label):
        try:
            return fn_()
        except Exception as e:
            warns.append(f"{label}: {str(e)[:140]}")
            return None

    cohort = T(lambda: metrics.trial_cohort(mb), "cohort/Metabase")
    fn = T(lambda: metrics.funnel_db(mb), "funnel/Metabase")
    quiz = T(lambda: metrics.typeform_quiz(tf), "quiz/Typeform")
    steps = T(lambda: metrics.mixpanel_steps(mp, frm, to), "steps/Mixpanel")
    opens_d = T(lambda: metrics.mixpanel_opens_daily(mp, frm, to), "opens/Mixpanel")
    comp_d = T(lambda: metrics.mixpanel_completes_daily(mp, frm, to), "completes/Mixpanel")
    trend_db = T(lambda: metrics.trend_db_daily(mb), "trend/Metabase")
    emails = T(lambda: metrics.emails_cio(cio), "emails/CIO")
    vd = metrics.verdict_new(fn, quiz) if (fn and quiz) else None
    elig_rate = (quiz["eligible"] / quiz["completed"]) if (quiz and quiz["completed"]) else 0

    with open(HTML_PATH, encoding="utf-8") as fh:
        html = original = fh.read()

    # 1) trial cohort (SESS) — preserve the vs-old baseline fields
    if cohort:
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

    # 2) full funnel (BF) — new-flow real rows, only the sources that succeeded
    if quiz:
        for name, val in [("Lead form opened", quiz["opened"]),
                          ("Lead form completed", quiz["completed"]),
                          ("↳ Eligible", quiz["eligible"]),
                          ("↳ Not eligible", quiz["completed"] - quiz["eligible"])]:
            html = bf(html, name, val)
    if steps:
        google = max(steps["reg_completed"] - steps["otp_verified"], 0)
        for name, val in [("Registration started", steps["reg_started"]),
                          ("↳ email — reached OTP", steps["otp_sent"]),
                          ("↳ email — OTP verified", steps["otp_verified"]),
                          ("↳ Google sign-in (skips OTP)", google),
                          ("Name step started", steps["name_started"]),
                          ("Name step completed", steps["name_completed"])]:
            html = bf(html, name, val)
    if fn:
        for name, val in [("Account created", fn["accounts"]),
                          ("Trial started", fn["trials"]),
                          ("In trial (active)", fn["in_trial"]),
                          ("Cancelled during trial", fn["cancelled"]),
                          ("Booked a consult", fn["booked"]),
                          ("Meeting done", fn["meeting_done"]),
                          ("Installed the app", fn["installed"])]:
            html = bf(html, name, val)

    # 3) step completion (STEPX) — new sides
    if quiz:
        html = stepx(html, "Quiz — opened → completed", "nS", quiz["opened"])
        html = stepx(html, "Quiz — opened → completed", "nF", quiz["completed"])
    if steps and fn:
        html = stepx(html, "Registration — started → account", "nS", steps["reg_started"])
        html = stepx(html, "Registration — started → account", "nF", fn["accounts"])
    if steps:
        html = stepx(html, "OTP — code sent → verified", "nS", steps["otp_sent"])
        html = stepx(html, "OTP — code sent → verified", "nF", steps["otp_verified"])
        html = stepx(html, "Name — started → completed", "nS", steps["name_started"])
        html = stepx(html, "Name — started → completed", "nF", steps["name_completed"])

    # 4) verdict — new sides
    if vd:
        html = verdict(html, "Form completion — opened → completed", vd["form"])
        html = verdict(html, "Activation — free trial vs paid sub", vd["activation"])
        html = verdict(html, "Consult booking", vd["booking"])
        html = verdict(html, "App install", vd["app"])

    # 5) eligibility (QUIZ) + MQL denominator label
    if quiz:
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
        html = re.sub(r"(· new )\d+( · old 1379)",
                      lambda mm: mm.group(1) + str(quiz["eligible"]) + mm.group(2), html, count=1)

    # 6) daily trend (TREND) — op (Mixpanel page views), ld/el (Typeform daily),
    #    ac/tr/bk/md (business DB). So the range-summed funnel matches the totals.
    qld = quiz["daily_ld"] if quiz else {}
    qel = quiz["daily_el"] if quiz else {}
    if opens_d or qld or trend_db:
        od, td = opens_d or {}, trend_db or {}
        tm = re.search(r"(var TREND\s*=\s*)(\[.*?\])(;)", html, re.S)
        series = json.loads(tm.group(2))
        by_date = {e["d"]: e for e in series}
        for d in sorted(set(od) | set(qld) | set(td)):
            if d < LAUNCH_MMDD:
                continue
            e = by_date.get(d)
            if e is None:
                z = {"op": 0, "ld": 0, "el": 0, "ac": 0, "acR": 0, "tr": 0, "bk": 0, "md": 0}
                e = {"d": d, "o": dict(z), "n": dict(z)}
                series.append(e)
                by_date[d] = e
            upd = {}
            if od:
                upd["op"] = od.get(d, 0)
            if qld:
                upd["ld"] = qld.get(d, 0)
            if qel:
                upd["el"] = qel.get(d, 0)
            if d in td:
                upd.update(ac=td[d].get("ac", 0), tr=td[d].get("tr", 0),
                           bk=td[d].get("bk", 0), md=td[d].get("md", 0))
            e["n"].update(upd)
        series.sort(key=lambda e: e["d"])
        trend_lit = tm.group(1) + json.dumps(series, separators=(",", ":"), ensure_ascii=False) + tm.group(3)
        html = replace_block(html, r"var TREND\s*=\s*\[.*?\];", trend_lit, "TREND")

    # 7) email campaigns (EMAIL)
    if emails:
        def em_obj(e):
            return ("{id:%d,n:%s,d:%s,sent:%d,dl:%d,o:%d,c:%d,cv:%d,u:%d}" % (
                e["id"], json.dumps(e["n"], ensure_ascii=False), json.dumps(e["d"], ensure_ascii=False),
                e["sent"], e["dl"], e["o"], e["c"], e["cv"], e["u"]))
        email_lit = "var EMAIL=[\n    " + ",\n    ".join(em_obj(e) for e in emails) + "];"
        html = replace_block(html, r"var EMAIL=\[.*?\];", email_lit, "EMAIL")

    # 8) last-updated stamp (always)
    stamp = datetime.datetime.now(TZ)
    html = re.sub(r"Last updated <b>.*?</b> · [^<]*",
                  f"Last updated <b>{stamp.strftime('%-d %b %Y')}</b> · {stamp.strftime('%H:%M %Z')}",
                  html, count=1)

    if html == original:
        print("No change.")
        return
    with open(HTML_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("Regenerated", HTML_PATH, "·", stamp.strftime("%-d %b %Y · %H:%M %Z"))
    if cohort:
        print("  cohort ", cohort)
    if fn:
        print("  funnel ", fn)
    if quiz:
        print("  quiz   ", {k: quiz[k] for k in ("opened", "completed", "eligible")})
    if emails:
        print("  emails ", len(emails), "· sent", sum(e["sent"] for e in emails))
    if warns:
        print("  ⚠ kept last values for:")
        for w in warns:
            print("    -", w)
    print("  updated", stamp.strftime("%-d %b %Y · %H:%M %Z"))


if __name__ == "__main__":
    main()
