#!/usr/bin/env python3
"""
Dental board newsroom monitor.

For each site in data/sites.json:
  1. Fetch the page.
  2. Strip nav/script/style/footer noise, normalize the remaining text.
  3. Hash it and compare to the stored hash in data/state/<slug>.json.
  4. If changed, compute a simple added/removed line diff against the
     previously stored text snapshot, and queue an email alert.
  5. Update the stored state (hash + text snapshot) either way.

At the end, if any sites changed, send ONE summary email via the
MailerSend API covering all changes (rather than one email per site).

Required environment variables (set as GitHub Actions secrets):
  MAILERSEND_API_KEY   - MailerSend API token
  MAILERSEND_FROM_EMAIL - verified "from" address on your MailerSend domain
  ALERT_TO_EMAIL       - where alerts should be sent (can be comma-separated)

Optional:
  MAILERSEND_FROM_NAME  - display name for the from address (default below)
"""

import os
import re
import sys
import json
import hashlib
import difflib
import pathlib
import datetime
import urllib.request
import urllib.error

import requests
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITES_FILE = ROOT / "data" / "sites.json"
STATE_DIR = ROOT / "data" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (compatible; USADentalReportBoardMonitor/1.0; "
    "+https://usadentalreport.com)"
)

NOISE_TAGS = ["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]


def load_sites():
    with open(SITES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def state_paths(slug):
    return (
        STATE_DIR / f"{slug}.json",
        STATE_DIR / f"{slug}.txt",
    )


def load_state(slug):
    meta_path, text_path = state_paths(slug)
    meta = None
    text = ""
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if text_path.exists():
        text = text_path.read_text(encoding="utf-8")
    return meta, text


def save_state(slug, text_hash, text, changed):
    meta_path, text_path = state_paths(slug)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    prev_meta = meta_path.read_text(encoding="utf-8") if meta_path.exists() else None
    last_changed = now if changed or not prev_meta else json.loads(prev_meta).get("last_changed", now)
    meta = {
        "hash": text_hash,
        "last_checked": now,
        "last_changed": last_changed,
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")


def fetch(url):
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in NOISE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    # Prefer <main> if present, otherwise the whole body
    main = soup.find("main") or soup.body or soup
    text = main.get_text(separator="\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def diff_summary(old_text, new_text, max_lines=15):
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    added = [l[1:].strip() for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:].strip() for l in diff if l.startswith("-") and not l.startswith("---")]
    added = added[:max_lines]
    removed = removed[:max_lines]
    return added, removed


def send_email(api_key, from_email, from_name, to_emails, subject, html_body, text_body):
    url = "https://api.mailersend.com/v1/email"
    payload = {
        "from": {"email": from_email, "name": from_name},
        "to": [{"email": e.strip()} for e in to_emails if e.strip()],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            print(f"MailerSend response: {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"MailerSend error {e.code}: {body}", file=sys.stderr)
        raise


def build_email(changes):
    today = datetime.date.today().isoformat()
    subject = f"Dental board updates detected ({len(changes)} site{'s' if len(changes) != 1 else ''}) - {today}"

    html_parts = [f"<h2>Dental board newsroom changes - {today}</h2>"]
    text_parts = [f"Dental board newsroom changes - {today}\n"]

    for c in changes:
        html_parts.append(
            f"<h3>{c['state']} — {c['board_name']}</h3>"
            f"<p><a href='{c['url']}'>{c['url']}</a></p>"
        )
        text_parts.append(f"\n{c['state']} — {c['board_name']}\n{c['url']}\n")

        if c["added"]:
            html_parts.append("<p><strong>New/changed content:</strong></p><ul>")
            text_parts.append("New/changed content:")
            for line in c["added"]:
                html_parts.append(f"<li>{line}</li>")
                text_parts.append(f"  + {line}")
            html_parts.append("</ul>")
        else:
            html_parts.append("<p><em>Page changed but no clear added text lines were detected (layout/structure change, or content removed only).</em></p>")
            text_parts.append("Page changed but no clear added text lines detected.")

    html_body = "\n".join(html_parts)
    text_body = "\n".join(text_parts)
    return subject, html_body, text_body


def main():
    sites = load_sites()
    changes = []
    errors = []

    for site in sites:
        slug = site["slug"]
        url = site["url"]
        try:
            html = fetch(url)
            text = extract_text(html)
            new_hash = hash_text(text)
        except Exception as e:
            errors.append({"state": site["state"], "url": url, "error": str(e)})
            print(f"[ERROR] {site['state']} ({url}): {e}", file=sys.stderr)
            continue

        meta, old_text = load_state(slug)
        old_hash = meta["hash"] if meta else None
        changed = old_hash is not None and old_hash != new_hash
        is_first_run = old_hash is None

        if changed:
            added, removed = diff_summary(old_text, text)
            changes.append({
                "state": site["state"],
                "board_name": site["board_name"],
                "url": url,
                "added": added,
                "removed": removed,
            })
            print(f"[CHANGED] {site['state']}: {url}")
        elif is_first_run:
            print(f"[BASELINE] {site['state']}: {url}")
        else:
            print(f"[unchanged] {site['state']}")

        save_state(slug, new_hash, text, changed)

    if errors:
        print(f"\n{len(errors)} site(s) failed to fetch:", file=sys.stderr)
        for e in errors:
            print(f"  - {e['state']}: {e['error']}", file=sys.stderr)

    if changes:
        api_key = os.environ.get("MAILERSEND_API_KEY")
        from_email = os.environ.get("MAILERSEND_FROM_EMAIL")
        from_name = os.environ.get("MAILERSEND_FROM_NAME", "USA Dental Report Board Monitor")
        to_emails = os.environ.get("ALERT_TO_EMAIL", "").split(",")

        if not api_key or not from_email or not to_emails or not to_emails[0].strip():
            print(
                "MAILERSEND_API_KEY, MAILERSEND_FROM_EMAIL, and ALERT_TO_EMAIL must be set "
                "to send alerts. Skipping email; changes were still recorded.",
                file=sys.stderr,
            )
        else:
            subject, html_body, text_body = build_email(changes)
            send_email(api_key, from_email, from_name, to_emails, subject, html_body, text_body)
            print(f"Sent alert email for {len(changes)} changed site(s).")
    else:
        print("\nNo changes detected.")


if __name__ == "__main__":
    main()
