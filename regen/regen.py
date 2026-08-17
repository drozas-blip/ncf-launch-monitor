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


# ---- anchor contract ---------------------------------------------------------
# The surgical replacements above key on these literal strings in index.html.
# check_anchors() validates they all still resolve (offline, no API calls) so a
# stray copy edit is caught immediately instead of on the next data run.
BF_NAMES = [
    "Lead form opened", "Lead form completed", "↳ Eligible", "↳ Not eligible",
    "Registration started", "↳ email — reached OTP", "↳ email — OTP verified",
    "↳ Google sign-in (skips OTP)", "Name step started", "Name step completed",
    "Account created", "Trial started", "In trial (active)", "Cancelled during trial",
    "Booked a consult", "Meeting done", "Qualified by doctor",
    "Received first prescription", "Installed the app",
]
STEPX_LABELS = [
    "Quiz — opened → completed", "Registration — started → account",
    "OTP — code sent → verified", "Name — started → completed",
]
VERDICT_LABELS = [
    "Form completion — opened → completed", "Activation — free trial vs paid sub",
    "Consult booking", "App install",
]


def check_anchors(html):
    """Return a list of anchor problems (empty = the HTML contract is intact)."""
    problems = []

    def want(label, pattern, n=1):
        c = len(re.findall(pattern, html, re.S))
        if c != n:
            problems.append(f"{label}: found {c}, expected {n}")

    for name in BF_NAMES:
        want("BF " + name, 'n:"' + re.escape(name) + r'",v:')
    for label in STEPX_LABELS:
        want("STEPX " + label, 's:"' + re.escape(label) + '"')
    for label in VERDICT_LABELS:
        want("VERDICT " + label, 'l:"' + re.escape(label) + r'",n:')
    for label, pat in [("SESS", r"var SESS=\{"), ("QUIZ", r"var QUIZ=\["),
                       ("QTOT", r"var QTOT="), ("TREND", r"var TREND\s*=\s*\["),
                       ("EMAIL", r"var EMAIL=\["), ("stamp", r"Last updated <b>.*?</b> · ")]:
        want(label, pat)
    return problems


# ---- sanity gate -------------------------------------------------------------
# Guards a cumulative metric against a broken-but-parseable pull (the 962-vs-1220
# class): if a number that only ever grows suddenly reads 0 or drops hard, keep
# the last committed value and log it — same "degrade, don't corrupt" philosophy
# as the per-source T() wrappers.
def _embedded_bf(html, name):
    m = re.search('n:"' + re.escape(name) + r'",v:([0-9.]+)', html)
    return float(m.group(1)) if m else None


def _guard(warns, name, new, old, floor=1, max_drop=0.5):
    if old is None or new is None:
        return new
    if new < floor <= old:
        warns.append(f"SANITY {name}: kept {int(old)} (pull returned {new})")
        return int(old)
    if old > 0 and new < old * (1 - max_drop):
        warns.append(f"SANITY {name}: kept {int(old)} (pull returned {new}, >{int(max_drop*100)}% drop)")
        return int(old)
    return new


def _strip_stamp(s):
    return re.sub(r"Last updated <b>.*?</b> · [^<]*", "", s)


def _check_invariants(html, warns):
    """The range-summed funnel must equal the funnel totals — assert it holds."""
    m = re.search(r"var TREND\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        return
    series = json.loads(m.group(1))

    def trend_sum(key):
        return sum(e["n"].get(key, 0) for e in series if e["d"] >= LAUNCH_MMDD)

    for key, name in [("ld", "Lead form completed"), ("el", "↳ Eligible"),
                      ("ac", "Account created"), ("tr", "Trial started"),
                      ("bk", "Booked a consult"), ("md", "Meeting done"),
                      ("rx", "Received first prescription")]:
        total = _embedded_bf(html, name)
        s = trend_sum(key)
        if total is not None and abs(s - total) > max(2, total * 0.02):
            warns.append(f"INVARIANT {name}: daily sum {s} ≠ funnel total {int(total)}")


# ---- main --------------------------------------------------------------------
def main():
    lib.load_secrets()
    lib.require_secrets()
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
    doctor = T(lambda: metrics.doctor_metrics(mb), "doctor/Metabase")
    fn = T(lambda: metrics.funnel_db(mb), "funnel/Metabase")
    quiz = T(lambda: metrics.typeform_quiz(tf), "quiz/Typeform")
    steps = T(lambda: metrics.mixpanel_steps(mp, frm, to), "steps/Mixpanel")
    opens_d = T(lambda: metrics.mixpanel_opens_daily(mp, frm, to), "opens/Mixpanel")
    web_d = T(lambda: metrics.mixpanel_web_daily(mp, frm, to), "web/Mixpanel")
    trend_db = T(lambda: metrics.trend_db_daily(mb), "trend/Metabase")
    emails = T(lambda: metrics.emails_cio(cio), "emails/CIO")
    vd = metrics.verdict_new(fn, quiz) if (fn and quiz) else None

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
        parts = [f"{k}:{cohort[k]}" for k in sess_fields]
        # doctor's verdict split for the reached-doctor leaves (same cohort as seen)
        doc_fields = ["subQual", "subDisq", "subNone", "unsubQual", "unsubDisq", "unsubNone"]
        if doctor:
            parts += [f"{k}:{doctor[k]}" for k in doc_fields]
        else:  # keep whatever's already embedded if the pull failed
            for k in doc_fields:
                mm = m and re.search(rf"{k}:([0-9]+)", m.group(1))
                parts.append(f"{k}:{mm.group(1) if mm else 0}")
        parts += [f"{k}:{keep[k]}" for k in keep]
        sess_lit = "var SESS={" + ",".join(parts) + "};"
        html = replace_block(html, r"var SESS=\{.*?\};", sess_lit, "SESS")

    # 2) full funnel (BF) — new-flow real rows, only the sources that succeeded.
    #    Headline cumulative counts go through _guard (keep last if a pull looks
    #    broken) before they're written.
    if quiz:
        opened = _guard(warns, "Lead form opened", quiz["opened"], _embedded_bf(html, "Lead form opened"))
        completed = _guard(warns, "Lead form completed", quiz["completed"], _embedded_bf(html, "Lead form completed"))
        eligible = _guard(warns, "↳ Eligible", quiz["eligible"], _embedded_bf(html, "↳ Eligible"))
        for name, val in [("Lead form opened", opened),
                          ("Lead form completed", completed),
                          ("↳ Eligible", eligible),
                          ("↳ Not eligible", completed - eligible)]:
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
        acc_g = _guard(warns, "Account created", fn["accounts"], _embedded_bf(html, "Account created"))
        tr_g = _guard(warns, "Trial started", fn["trials"], _embedded_bf(html, "Trial started"))
        bk_g = _guard(warns, "Booked a consult", fn["booked"], _embedded_bf(html, "Booked a consult"))
        md_g = _guard(warns, "Meeting done", fn["meeting_done"], _embedded_bf(html, "Meeting done"))
        for name, val in [("Account created", acc_g),
                          ("Trial started", tr_g),
                          ("In trial (active)", fn["in_trial"]),
                          ("Cancelled during trial", fn["cancelled"]),
                          ("Booked a consult", bk_g),
                          ("Meeting done", md_g),
                          ("Received first prescription", fn["prescribed"]),
                          ("Installed the app", fn["installed"])]:
            html = bf(html, name, val)
    if doctor:  # was hard-coded/stale; now live from the Health DB
        html = bf(html, "Qualified by doctor", doctor["qualified"])

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
    if opens_d or qld or trend_db or web_d:
        od, td, wd = opens_d or {}, trend_db or {}, web_d or {}
        tm = re.search(r"(var TREND\s*=\s*)(\[.*?\])(;)", html, re.S)
        series = json.loads(tm.group(2))
        by_date = {e["d"]: e for e in series}
        for d in sorted(set(od) | set(qld) | set(td) | set(wd)):
            if d < LAUNCH_MMDD:
                continue
            e = by_date.get(d)
            if e is None:
                z = {"op": 0, "ld": 0, "el": 0, "ac": 0, "acR": 0, "tr": 0, "bk": 0,
                     "md": 0, "rx": 0, "web": 0}
                e = {"d": d, "o": dict(z), "n": dict(z)}
                series.append(e)
                by_date[d] = e
            upd = {}
            if od:
                upd["op"] = od.get(d, 0)
            if wd:
                upd["web"] = wd.get(d, 0)   # total site traffic (not new-flow filtered)
            if qld:
                upd["ld"] = qld.get(d, 0)
            if qel:
                upd["el"] = qel.get(d, 0)
            if d in td:
                upd.update(ac=td[d].get("ac", 0), tr=td[d].get("tr", 0),
                           bk=td[d].get("bk", 0), md=td[d].get("md", 0),
                           rx=td[d].get("rx", 0))
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

    # invariant: the range-summed funnel must still equal the funnel totals
    _check_invariants(html, warns)

    # Did any actual data change? (compare ignoring the always-moving stamp)
    data_changed = _strip_stamp(html) != _strip_stamp(original)

    # 8) last-updated stamp — always refreshed so the deployed page shows a live
    #    time; the *commit* is gated on data_changed (see the workflow) so quiet
    #    days don't add stamp-only commits to the history.
    stamp = datetime.datetime.now(TZ)
    html = re.sub(r"Last updated <b>.*?</b> · [^<]*",
                  f"Last updated <b>{stamp.strftime('%-d %b %Y')}</b> · {stamp.strftime('%H:%M %Z')}",
                  html, count=1)
    with open(HTML_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)

    # expose the result to CI (GitHub Actions step output)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"data_changed={'1' if data_changed else '0'}\n")

    print(("Regenerated " if data_changed else "No data change (stamp refreshed) ") +
          HTML_PATH + " · " + stamp.strftime("%-d %b %Y · %H:%M %Z"))
    if cohort:
        print("  cohort ", cohort)
    if fn:
        print("  funnel ", fn)
    if quiz:
        print("  quiz   ", {k: quiz[k] for k in ("opened", "completed", "eligible")})
    if emails:
        print("  emails ", len(emails), "· sent", sum(e["sent"] for e in emails))
    if warns:
        print("  ⚠ sanity/invariant notes:")
        for w in warns:
            print("    -", w)


def _run_check():
    """Offline anchor validation — no secrets, no API calls. Exit non-zero if the
    HTML no longer matches the replacement contract."""
    with open(HTML_PATH, encoding="utf-8") as fh:
        problems = check_anchors(fh.read())
    if problems:
        print("regen --check: FAIL")
        for p in problems:
            print("  -", p)
        raise SystemExit(1)
    print("regen --check: OK — all", len(BF_NAMES) + len(STEPX_LABELS) + len(VERDICT_LABELS) + 6,
          "anchors resolve")


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        _run_check()
    else:
        main()
