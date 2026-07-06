# Dental Board Newsroom Monitor

Checks state dental board newsroom/updates pages daily and emails you (via MailerSend) when one changes. Built for USA Dental Report.

## How it works

1. `data/sites.json` lists each state, board name, and its News URL (pulled from your spreadsheet — 35 states currently have one; the rest didn't have a News URL in column G and were left out).
2. Every day at 13:00 UTC, a GitHub Action runs `scripts/check_sites.py`, which:
   - Fetches each page
   - Strips nav/header/footer/script/style noise, keeping just the readable text
   - Hashes that text and compares it to the last stored hash (`data/state/<slug>.json` + `.txt`)
   - If it changed, pulls out the added lines as a lightweight diff
3. If anything changed, **one summary email** goes out via MailerSend listing every site that changed, its URL, and what looks new.
4. The updated hashes/snapshots get committed back to the repo automatically so the next run has something to compare against.

The first run for any site just establishes a baseline (no email) — you'll only get alerted on the second-and-later runs when something actually changes.

## Setup

1. Push this folder to a new (or existing) GitHub repo.
2. In the repo's **Settings → Secrets and variables → Actions**, add:
   - `MAILERSEND_API_KEY` — your MailerSend API token
   - `MAILERSEND_FROM_EMAIL` — a verified sender address on your MailerSend domain
   - `ALERT_TO_EMAIL` — where you want alerts sent (comma-separate for multiple addresses)
   - `MAILERSEND_FROM_NAME` — optional, defaults to "USA Dental Report Board Monitor"
3. That's it — the workflow (`.github/workflows/check.yml`) is already wired to a daily cron (`0 13 * * *`) and can also be triggered manually from the Actions tab (`workflow_dispatch`).

## Adding/editing sites

Edit `data/sites.json` directly — each entry is:

```json
{
  "state": "Alabama",
  "board_name": "Board of Dental Examiners of Alabama",
  "url": "https://dentalboard.org/news-and-announcements/",
  "slug": "alabama"
}
```

`slug` just needs to be unique and filesystem-safe (used as the state filename). Adding a new site means its first run will be a silent baseline, same as above.

## States without a News URL

These 18 didn't have anything in column G of your spreadsheet and aren't monitored yet: Alaska, Idaho, Indiana, Kansas, Kentucky, Louisiana, Maine, Missouri, Montana, Nevada, Ohio, Oklahoma, Rhode Island, South Carolina, South Dakota, Texas, Vermont, Wyoming. Add them to `data/sites.json` once you track down a monitorable URL (some may only have an "Updates URL" in column F, a general agency newsroom, or no online newsroom at all).

## Known limitations

- **JS-rendered pages**: a few state sites (e.g. anything built on heavy client-side rendering) may not expose real content in the raw HTML. If a site's snapshot text looks empty/garbage after the first run, that's the sign — those would need a headless-browser fetch instead of plain `requests`, which this script doesn't do to keep things zero-cost and dependency-light. Flag any you notice and I can add a Playwright-based fallback for just those.
- **Bot-blocking**: some state government sites (Akamai/Cloudflare-protected) may occasionally 403 a plain `requests` fetch. Errors are logged in the Action's run log but won't crash the whole job — every other site still gets checked.
- **Layout-only changes**: if a state redesigns its page without adding new announcement text, you may get a "changed" alert with no clear added lines — treat that as "go look," not necessarily "new announcement."
