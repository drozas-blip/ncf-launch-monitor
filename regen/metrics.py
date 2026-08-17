"""Metric builders — each returns a plain dict/list ready to embed in the HTML.

Every function takes the API clients it needs so they can be tested in isolation.
Business DB = 14, Health DB = 21 (Metabase database ids).
"""

LAUNCH_DATE = "2026-07-20"        # NCF live in prod
TEST_DOMAIN = "@findbalance.app"  # internal test accounts to exclude


# ---------------------------------------------------------- trial cohort ---
_TRIAL_COHORT_SQL = f"""
with cohort as (
  select distinct on (lower(u.email)) lower(u.email) email, s.active
  from subscriptions s join users u on u.id = s.user_id
  where u.landing_source = 'new_onboarding_flow'
    and u.type = 'patient'
    and lower(u.email) not like '%{TEST_DOMAIN}'
    and s.trial_end_at is not null
    and s.created_at >= '{LAUNCH_DATE}'
  order by lower(u.email), s.created_at desc
),
mtg as (
  select lower(user_email) email,
    max(case when state='scheduled' and time < now() and user_no_show_at is null then 1 else 0 end) seen,
    max(case when state='scheduled' and time >= now() then 1 else 0 end) upcoming,
    max(case when user_no_show_at is not null then 1 else 0 end) noshow,
    count(*) any_mtg
  from meetings group by 1
),
j as (
  select c.email, c.active,
    coalesce(m.seen,0) seen, coalesce(m.upcoming,0) upcoming,
    coalesce(m.noshow,0) noshow, coalesce(m.any_mtg,0) any_mtg,
    case when coalesce(m.seen,0)=1 then 'seen'
         when not c.active then 'gone'
         when coalesce(m.upcoming,0)=1 then 'scheduled'
         else 'stuck' end bucket
  from cohort c left join mtg m on m.email = c.email
)
select
  count(*)                                                             trials,
  count(*) filter (where bucket='seen')                               seen,
  count(*) filter (where bucket='scheduled')                          scheduled,
  count(*) filter (where bucket='stuck')                              stuck,
  count(*) filter (where bucket='gone')                               gone,
  count(*) filter (where not active)                                  unsub,
  count(*) filter (where not active and seen=1)                       unsub_after_dr,
  count(*) filter (where bucket='stuck' and noshow=1)                 st_noshow,
  count(*) filter (where bucket='stuck' and noshow=0 and any_mtg=0)   st_never,
  count(*) filter (where bucket='stuck' and noshow=0 and any_mtg>0)   st_cancel
from j
"""


def trial_cohort(mb):
    """Return the SESS.* cohort fields (no-show rate / time-to-doctor kept
    from the previous value by the caller — those compare vs the old baseline)."""
    r = mb.one(14, _TRIAL_COHORT_SQL)
    g = lambda k: int(r.get(k) or 0)
    return {
        "trials": g("trials"),
        "seen": g("seen"),
        "scheduled": g("scheduled"),
        "stuck": g("stuck"),
        "gone": g("gone"),
        "unsub": g("unsub"),
        "unsubAfterDr": g("unsub_after_dr"),
        "stNoshow": g("st_noshow"),
        "stNever": g("st_never"),
        "stCancel": g("st_cancel"),
    }


# -------------------------------------------------- funnel (business DB) ---
_NEWFLOW_U = f"""
  select id, lower(email) email from users
  where landing_source='new_onboarding_flow' and type='patient'
    and lower(email) not like '%{TEST_DOMAIN}' and created_at >= '{LAUNCH_DATE}'
"""

_FUNNEL_SQL = f"""
with u as ({_NEWFLOW_U}),
fb as (select distinct user_id from user_firebase_tokens)
select
  (select count(*) from u)                                                                    accounts,
  (select count(distinct s.user_id) from subscriptions s join u on u.id=s.user_id
     where s.trial_end_at is not null)                                                        trials,
  (select count(distinct s.user_id) from subscriptions s join u on u.id=s.user_id
     where s.trial_end_at is not null and s.active)                                           in_trial,
  (select count(distinct s.user_id) from subscriptions s join u on u.id=s.user_id
     where s.trial_end_at is not null and not s.active)                                       cancelled,
  (select count(distinct lower(m.user_email)) from meetings m join u on u.email=lower(m.user_email)) booked,
  (select count(distinct lower(m.user_email)) from meetings m join u on u.email=lower(m.user_email)
     where m.state='scheduled' and m.time < now() and m.user_no_show_at is null)              meeting_done,
  -- first prescription issued by the doctor (business DB). For the new-flow
  -- cohort issued == downloaded today, i.e. everyone who got one opened it.
  (select count(distinct p.patient_id) from prescriptions p join u on u.id=p.patient_id
     where p.issued_at is not null)                                                           prescribed,
  (select count(*) from u join fb on fb.user_id=u.id)                                         installed
from u limit 1
"""


def funnel_db(mb):
    r = mb.one(14, _FUNNEL_SQL)
    return {k: int(r.get(k) or 0) for k in
            ("accounts", "trials", "in_trial", "cancelled", "booked", "meeting_done",
             "prescribed", "installed")}


# --------------------------------------- doctor qualification (Health DB) ---
# Cross-DB: the reached-doctor cohort + subscription state come from the business
# DB (14, has email → test exclusion); the doctor's verdict lives in the Health
# DB (21, keyed by user_id). We link on user_id (shared id space).
# Same cohort as the trial-cohort tree (SESS): one row per trial user, their
# subscription-active flag, keeping only those who reached the doctor. So the
# qualified/disqualified split sums exactly to the tree's seen/unsubAfterDr.
_REACHED_DOCTOR_SQL = f"""
with cohort as (
  select distinct on (lower(u.email)) u.id, lower(u.email) email, s.active
  from subscriptions s join users u on u.id = s.user_id
  where u.landing_source='new_onboarding_flow' and u.type='patient'
    and lower(u.email) not like '%{TEST_DOMAIN}'
    and s.trial_end_at is not null and s.created_at >= '{LAUNCH_DATE}'
  order by lower(u.email), s.created_at desc
),
mtg as (
  select lower(user_email) email,
    max(case when state='scheduled' and time < now() and user_no_show_at is null then 1 else 0 end) seen
  from meetings group by 1
)
select c.id, c.active
from cohort c join mtg m on m.email = c.email
where coalesce(m.seen, 0) = 1
"""


def doctor_metrics(mb):
    """Doctor's qualified/disqualified verdict for everyone who reached the consult
    (new flow, test-excluded, since launch), split by whether they're still
    subscribed. Returns None if the cohort is empty or Health DB is unreachable."""
    rows = mb.query(14, _REACHED_DOCTOR_SQL)
    if not rows:
        return None
    active_by_id = {r["id"]: r["active"] for r in rows}
    ids = list(active_by_id)
    idlist = ",".join(str(i) for i in ids)
    q = (f"select user_id, qualification_status::text qs from user_health_profiles "
         f"where user_id in ({idlist}) and deleted_at is null")
    qual = {r["user_id"]: (r["qs"] or "none") for r in mb.query(21, q)}

    def verdict(qs):
        return "disq" if qs == "unqualified" else ("qual" if qs == "qualified" else "none")

    def bucket(is_active):
        g = {"qual": 0, "disq": 0, "none": 0}
        for i in ids:
            if active_by_id[i] == is_active:
                g[verdict(qual.get(i, "none"))] += 1
        return g

    sub, uns = bucket(True), bucket(False)
    return {
        "reached": len(ids),
        "qualified": sub["qual"] + uns["qual"],       # → funnel "Qualified by doctor"
        "disqualified": sub["disq"] + uns["disq"],
        "subQual": sub["qual"], "subDisq": sub["disq"], "subNone": sub["none"],
        "unsubQual": uns["qual"], "unsubDisq": uns["disq"], "unsubNone": uns["none"],
    }


# --------------------------------------------- daily trend (business DB) ---
# accounts / trials / bookings per day, keyed by 'MM-DD'
_TREND_DAILY_SQL = f"""
with u as ({_NEWFLOW_U}),
days as (select to_char(gs,'MM-DD') d from generate_series('{LAUNCH_DATE}'::date, current_date, '1 day') gs),
acc as (select to_char(created_at,'MM-DD') d, count(*) c
        from users where landing_source='new_onboarding_flow' and type='patient'
          and lower(email) not like '%{TEST_DOMAIN}' and created_at >= '{LAUNCH_DATE}' group by 1),
tr as (select to_char(s.created_at,'MM-DD') d, count(distinct s.user_id) c
       from subscriptions s join u on u.id=s.user_id
       where s.trial_end_at is not null and s.created_at >= '{LAUNCH_DATE}' group by 1),
-- count each user once, on the day of their FIRST meeting — reschedules create
-- extra rows on later days, so a plain per-day distinct would sum above the total.
bk as (select to_char(fd,'MM-DD') d, count(*) c from (
         select lower(m.user_email) e, min(m.created_at) fd
         from meetings m join u on u.email=lower(m.user_email)
         where m.created_at >= '{LAUNCH_DATE}' group by 1) x group by 1),
md as (select to_char(fd,'MM-DD') d, count(*) c from (
         select lower(m.user_email) e, min(m.time) fd
         from meetings m join u on u.email=lower(m.user_email)
         where m.state='scheduled' and m.time < now() and m.user_no_show_at is null
           and m.time >= '{LAUNCH_DATE}' group by 1) x group by 1),
rx as (select to_char(fd,'MM-DD') d, count(*) c from (
         select p.patient_id, min(p.issued_at) fd
         from prescriptions p join u on u.id=p.patient_id
         where p.issued_at is not null and p.issued_at >= '{LAUNCH_DATE}' group by 1) x group by 1)
select days.d, coalesce(acc.c,0) ac, coalesce(tr.c,0) tr, coalesce(bk.c,0) bk,
       coalesce(md.c,0) md, coalesce(rx.c,0) rx
from days left join acc on acc.d=days.d left join tr on tr.d=days.d
          left join bk on bk.d=days.d left join md on md.d=days.d
          left join rx on rx.d=days.d
order by days.d
"""


def trend_db_daily(mb):
    """Return {'MM-DD': {'ac','tr','bk','md','rx'}} for the new flow since launch."""
    rows = mb.query(14, _TREND_DAILY_SQL)
    return {r["d"]: {k: int(r[k]) for k in ("ac", "tr", "bk", "md", "rx")} for r in rows}


# ----------------------------------------------------------- Mixpanel ---
NEWFLOW_WHERE = 'properties["landing_source"]=="new_onboarding_flow"'
# "lead form page view" was renamed (23 Jul); sum both casings to span the boundary.
MP_OPENS = ["Lead form page view", "lead form page view"]
MP_STEPS = {   # single-cased new-flow events
    "reg_started": "web registration started",
    "reg_completed": "web registration completed",
    "otp_sent": "web otp sent",
    "otp_verified": "web otp verified",
    "name_started": "name collection on web started",
    "name_completed": "name collection on web completed",
}


def _mp_daily(mp, event, frm, to, typ="general", where=NEWFLOW_WHERE):
    out = mp.segmentation(event, frm, to, unit="day", where=where, typ=typ)
    vals = (out.get("data", {}).get("values", {}) or {}).get(event, {})
    return {d: int(c) for d, c in vals.items()}


def _mp_daily_sum(mp, events, frm, to, typ="general"):
    total = {}
    for ev in events:
        for d, c in _mp_daily(mp, ev, frm, to, typ=typ).items():
            total[d[5:]] = total.get(d[5:], 0) + c
    return total


def mixpanel_opens_daily(mp, frm, to):
    """Daily form page-views (general) for the new flow → {'MM-DD': opens}."""
    return _mp_daily_sum(mp, MP_OPENS, frm, to)


# Marketing site (findbalance.app + metodobalance.es — same landing, two domains).
# NOTE: this event carries no `landing_source`, so it is TOTAL site traffic and
# cannot be filtered to the new flow, unlike every other step. Labelled as such.
MP_WEB = "Landing view"


def mixpanel_web_daily(mp, frm, to):
    """Daily unique website visitors → {'MM-DD': visitors}."""
    return {d[5:]: c for d, c in
            _mp_daily(mp, MP_WEB, frm, to, typ="unique", where=None).items()}


def mixpanel_steps(mp, frm, to):
    """Unique new-flow users per step over the range."""
    return {k: sum(_mp_daily(mp, ev, frm, to, typ="unique").values())
            for k, ev in MP_STEPS.items()}


# ------------------------------------------------------------- Typeform ---
LEAD_FORM_ID = "ex4N7zee"      # ONBOARD - Patient - Eligibility Form - Balance


def _tf_all_responses(tf, fid=LEAD_FORM_ID):
    items, token = [], None
    while True:
        p = {"since": LAUNCH_DATE + "T00:00:00", "page_size": 1000}
        if token:
            p["before"] = token
        it = tf.get(f"/forms/{fid}/responses", **p).get("items", [])
        items += it
        if len(it) < 1000:
            break
        token = it[-1].get("token")
    return items


def _tf_answers(it):
    d = {}
    for a in it.get("answers", []) or []:
        ref, t = a.get("field", {}).get("ref"), a.get("type")
        d[ref] = (a.get("boolean") if t == "boolean"
                  else a.get("choice", {}).get("label") if t == "choice"
                  else a.get("choices", {}).get("labels") if t == "choices"
                  else a.get("number") if t == "number" else None)
    return d


def _has_contra(v):
    if not v:
        return False
    labs = v if isinstance(v, list) else [v]
    return any("Ninguno" not in str(l) and "Ninguna" not in str(l) for l in labs)


def typeform_quiz(tf):
    """opened (unique visits) / completed / eligible + reason breakdown + daily completes & eligible."""
    ins = tf.get(f"/insights/{LEAD_FORM_ID}/summary")
    opened = sum(int(p.get("unique_visits", 0)) for p in ins.get("form", {}).get("platforms", []))
    items = _tf_all_responses(tf)
    b = {"eligible": 0, "bmi_lt27": 0, "bmi_2729": 0, "contra": 0, "under18": 0, "pregnant": 0, "other": 0}
    daily_ld, daily_el = {}, {}
    for it in items:
        vs = {v.get("key"): v.get(v.get("type")) for v in it.get("variables", [])}
        a = _tf_answers(it)
        bmi = vs.get("bmi")
        day = (it.get("submitted_at") or "")[5:10]
        if day:
            daily_ld[day] = daily_ld.get(day, 0) + 1
        if vs.get("qualified") == "yes":
            b["eligible"] += 1
            if day:
                daily_el[day] = daily_el.get(day, 0) + 1
        elif a.get("legal_age") is False:
            b["under18"] += 1
        elif _has_contra(a.get("abs_contraindications")):
            b["contra"] += 1
        elif a.get("pregnancy_breastfeeding") is True:
            b["pregnant"] += 1
        elif isinstance(bmi, (int, float)) and bmi < 27:
            b["bmi_lt27"] += 1
        elif isinstance(bmi, (int, float)) and 27 <= bmi < 30:
            b["bmi_2729"] += 1
        else:
            b["other"] += 1
    return {"opened": opened, "completed": len(items), "eligible": b["eligible"],
            "buckets": b, "daily_ld": daily_ld, "daily_el": daily_el}


# -------------------------------------------------------------- verdict ---
def verdict_new(funnel, quiz):
    """New-side percentages (old baseline stays static in the HTML). MQL = eligible leads."""
    mql = quiz["eligible"] or 1
    op = quiz["opened"] or 1
    return {
        "form": round(quiz["completed"] / op * 1000) / 10,
        "activation": round(funnel["trials"] / mql * 1000) / 10,
        "booking": round(funnel["booked"] / mql * 1000) / 10,
        "app": round(funnel["installed"] / mql * 1000) / 10,
    }


# ---------------------------------------------------------- Customer.io ---
# journey order · curated one-line descriptions (source of truth for the table)
EMAIL_CONFIG = [
    (83, "Welcome (eligible)", "Account-created confirmation (subject: \"¡Cuenta creada!\") — welcomes the eligible user and shows their eligibility results."),
    (80, "Not Eligible (nurture)", "Sends results to non-eligible leads, then a nurture sequence."),
    (87, "Profile Completion Recovery", "Nudges users who verified their email but didn't finish the profile."),
    (73, "Appointment & Medical Form", "Confirms the booked consult + reschedule link, and chases the medical form."),
    (81, "Post Medical Form Confirmation", "Confirms the pre-call / medical form was received before the call."),
    (86, "Booking Recovery", "Recovers users who qualified but haven't booked a consult."),
    (79, "No-show", "Follows up when a user misses their doctor video-call."),
    (74, "Reschedule / Cancellation", "Sends a reschedule link after a consult is canceled."),
    (103, "Reschedule Meeting Confirmation", "Confirms the new time + join link after a reschedule (B2C, email only)."),
    (85, "Payment Recovery", "Recovers failed trial / subscription payments."),
    (75, "App Onboarding", "After an eligible consult: invites to download the app + install reminders (day 1–20, email/SMS). Draft — trigger events not wired yet, so 0 so far."),
    (82, "Trial Pre-Renewal Reminder", "Reminds trial users before the trial converts to paid (~day 27)."),
]


def emails_cio(cio):
    out = []
    for cid, name, desc in EMAIL_CONFIG:
        try:
            s = cio.get(f"/campaigns/{cid}/metrics", period="days", steps=45).get("metric", {}).get("series", {})
            tot = lambda k: int(sum(s.get(k, []) or []))
            sent = tot("sent") or tot("attempted")
            out.append({"id": cid, "n": name, "d": desc, "sent": sent, "dl": tot("delivered"),
                        "o": tot("opened"), "c": tot("clicked"), "cv": tot("converted"), "u": tot("unsubscribed")})
        except Exception:
            out.append({"id": cid, "n": name, "d": desc, "sent": 0, "dl": 0, "o": 0, "c": 0, "cv": 0, "u": 0})
    return out


if __name__ == "__main__":
    import lib
    import json
    lib.load_secrets()
    mb, mp, tf, cio = lib.Metabase(), lib.Mixpanel(), lib.Typeform(), lib.CustomerIO()
    frm, to = LAUNCH_DATE, __import__("datetime").date.today().isoformat()
    print("cohort:  ", json.dumps(trial_cohort(mb)))
    fn = funnel_db(mb)
    print("funnel:  ", json.dumps(fn))
    q = typeform_quiz(tf)
    print("quiz:    ", json.dumps({k: q[k] for k in ("opened", "completed", "eligible", "buckets")}))
    print("verdict: ", json.dumps(verdict_new(fn, q)))
    print("steps:   ", json.dumps(mixpanel_steps(mp, frm, to)))
    em = emails_cio(cio)
    print("emails:  ", len(em), "campaigns, total sent", sum(e["sent"] for e in em))
