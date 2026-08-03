# NCF Launch Monitor

Self-updating product-analytics dashboard for the **New Onboarding Flow** launch.
A single static `index.html`; a daily GitHub Action pulls fresh numbers from the
live data sources and redeploys it to GitHub Pages.

- **Dashboard:** `index.html` (vanilla HTML/CSS/JS, no build step, no dependencies)
- **Data refresh:** `regen/regen.py` (Python stdlib only) rewrites the embedded
  data blocks in `index.html` and updates the "Last updated" stamp
- **Automation:** `.github/workflows/regen.yml` runs daily (06:00 UTC) + on demand

## One-time setup (GitHub)

1. **Create a private repo** under the org, e.g. `ferbalance/ncf-launch-monitor`,
   and push this folder to `main`.
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

The repo is **private** and the page carries `noindex` + a `robots.txt` deny, so it
won't show up in search. On standard GitHub, a Pages URL is still reachable by
anyone who has the (unguessable) link. For a hard "org-members-only" lock, put
**Cloudflare Access** in front of a custom subdomain (e.g. `ncf.balanceapp.ai`):
add the domain in Cloudflare, point it at Pages, and add an Access policy for the
team's email domain. No code changes needed.

The page shows **only aggregate metrics — no personal data**.

## Editing the dashboard

Edit `index.html` directly (layout, copy, colors). The data blocks are plain
`var NAME = …;` literals near the bottom of the `<script>`; the refresh job only
rewrites the ones it owns, so your layout edits are safe. Push to `main` to
redeploy.

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

## Note: Metabase is behind Cloudflare

`metabase.balanceapp.ai` sits behind Cloudflare bot protection. `regen.py` sends a
browser `User-Agent` to get through, which works from a normal machine. If a
GitHub-hosted runner ever gets blocked by IP, run the job on a self-hosted runner
or a small cron on an internal box instead — the script is identical either way.
