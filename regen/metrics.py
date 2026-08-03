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


if __name__ == "__main__":
    import lib
    lib.load_secrets()
    mb = lib.Metabase()
    sess = trial_cohort(mb)
    import json
    print(json.dumps(sess, indent=2))
    expected = {"trials": 31, "seen": 16, "scheduled": 4, "stuck": 7, "gone": 4,
                "unsub": 7, "unsubAfterDr": 3, "stNoshow": 3, "stNever": 3, "stCancel": 1}
    ok = all(sess.get(k) == v for k, v in expected.items())
    print("MATCHES EXPECTED (as of 3 Aug):", ok)
    if not ok:
        print("expected:", expected)
