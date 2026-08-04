# NCF Launch Monitor

Self-updating product-analytics dashboard for the **New Onboarding Flow** launch.
A single static `index.html`; a daily GitHub Action pulls fresh numbers from the
live data sources and redeploys it to GitHub Pages.

- **Dashboard:** `index.html` (vanilla HTML/CSS/JS, no build step, no dependencies)
- **Data refresh:** `regen/regen.py` (Python stdlib only) rewrites the embedded
  data blocks in `index.html` and updates the "Last updated" stamp
- **Automation:** `.github/workflows/regen.yml` runs daily (06:00 UTC) + on demand

## One-time setup (GitHub)

1. **Create the repo** and push this folder to `main`. This is currently
   `drozas-blip/ncf-launch-monitor`, and it is **public** — GitHub's free plan
   only serves Pages from public repos. See *Who can see it* below for what that
   means and how to lock it down properly.
2. **Add the data-source secrets** — repo → *Settings → Secrets and variables →
   Actions → New repository secret* — with the same values as `~/.config/secrets.env`:
   - `METABASE_URL`, `METABASE_USERNAME`, `METABASE_PASSWORD`
   - `MIXPANEL_SA_USERNAME`, `MIXPANEL_SA_SECRET`, `MIXPANEL_PROJECT_ID`
   - `TYPEFORM_TOKEN`
   - `CIO_APP_API_KEY`
   (Metabase is required today; the rest are wired for upcoming metrics.)
3. **Enable Pages** — *Settings → Pages → Build and deployment → Source:
   **GitHub Actions***.
4. **Run it once** — *Actions → regen-and-deploy → Run workflow*. When it
   finishes, the Pages URL is shown on the workflow run and under *Settings → Pages*.

After that it refreshes itself every morning; you never touch it.

## Who can see it

⚠️ **The repo and the Pages site are public.** The page carries `noindex` + a
`robots.txt` deny, so it won't show up in search engines, but **anyone with the
link can open it**, and the source (including `regen/`) is world-readable on
GitHub. There is *no* access control today — treat the URL as "unlisted", not
"private".

This is acceptable only because the page shows **aggregate metrics only — no
personal data**, and no secrets live in the repo (they're GitHub Actions secrets,
never committed).

For a real "org-members-only" lock you have two options:
- **Make the repo private** — requires a paid GitHub plan (Team/Enterprise) to
  keep Pages working, then flip *Settings → Pages → Visibility → Private*.
- **Put Cloudflare Access in front of a custom subdomain** (e.g.
  `ncf.balanceapp.ai`): add the domain in Cloudflare, point it at Pages, and add
  an Access policy for the team's email domain. No code changes needed. This also
  hides the source, which Pages-visibility alone does not.

## Editing the dashboard

Edit `index.html` directly (layout, copy, colors). The data blocks are plain
`var NAME = …;` literals near the bottom of the `<script>`; the refresh job only
rewrites the ones it owns, so your layout edits are safe. Push to `main` to
redeploy.

⚠️ **One rule:** the refresh keys on specific label strings (e.g.
`n:"Meeting done"`, `l:"App install"`). Don't rename those *keys* — the display
copy around them is free to change, but the anchor strings are a contract. After
editing, run the offline validator (no secrets, no network):

```
python3 regen/regen.py --check
```

CI runs it too, before every refresh, so a broken anchor fails fast with a clear
message instead of silently skipping a number.

## Data coverage (what auto-refreshes)

`regen.py` is modular — builders live in `regen/metrics.py`, the surgical
in-place replacements in `regen/regen.py`. Everything below auto-refreshes daily:

| Block | Source |
|---|---|
| Trial cohort tree (`SESS`) | Metabase (business DB) |
| Full funnel (`BF`) real rows | Metabase + Mixpanel + Typeform |
| Step completion (`STEPX`) new sides | Mixpanel (unique, flow-filtered) + Typeform |
| Verdict (form / activation / booking / app) | Typeform + Metabase |
| Eligibility breakdown (`QUIZ`) | Typeform response classifier |
| Daily trend + KPIs (`TREND`) | Mixpanel (opens/completes) + Metabase (accounts/trials/bookings) |
| Email campaigns (`EMAIL`) | Customer.io campaign metrics |
| Last-updated stamp | — |

**Old / baseline (May–June) values are static** and never touched — they're the
historical benchmark. A couple of small funnel rows without a clean source
(medical-form-before-consult, qualified-by-doctor) are also left as-is.

To add/adjust a block: edit the builder in `metrics.py` and its replacement in
`regen.py`. Run `python3 regen/regen.py` locally (reads `~/.config/secrets.env`) to
test before pushing — it rewrites `index.html` in place and prints what it changed.

**Data safety.** Headline cumulative counts (leads, eligible, accounts, trials,
bookings, meetings) pass through a sanity gate: if a pull returns `0` or drops by
more than half, the last good value is kept and the run logs a `SANITY …` note
instead of publishing a broken number. A final invariant check asserts the
range-summed daily series still equals the funnel totals (logs `INVARIANT …` if
not). Both surface in the workflow log without failing the deploy — the page
degrades to the last good value rather than showing garbage.

## Note: Metabase is behind Cloudflare

`metabase.balanceapp.ai` sits behind Cloudflare bot protection. `regen.py` sends a
browser `User-Agent` to get through, which works from a normal machine. If a
GitHub-hosted runner ever gets blocked by IP, run the job on a self-hosted runner
or a small cron on an internal box instead — the script is identical either way.
