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

At the end, if any sites changed, send ONE summary email via SMTP
covering all changes (rather than one email per site).

Required environment variables (set as GitHub Actions secrets):
  SMTP_HOST         - SMTP server hostname
  SMTP_USERNAME     - SMTP auth username
  SMTP_PASSWORD     - SMTP auth password
  SMTP_FROM_EMAIL   - "from" address (must be allowed by your SMTP server)
  ALERT_TO_EMAIL    - where alerts should be sent (can be comma-separated)

Optional:
  SMTP_PORT         - SMTP port (default: 587, uses STARTTLS; use 465 for implicit SSL)
  SMTP_FROM_NAME    - display name for the from address (default below)

Optional per-site keys in data/sites.json:
  headers               - dict of extra/override request headers for that site
  allow_unverified_tls  - retry without TLS verification if (and only if) the
                          initial request fails with an SSL error. For boards
                          that serve an incomplete certificate chain. The run
                          log marks these fetches [INSECURE].
"""

import os
import re
import sys
import json
import smtplib
import hashlib
import difflib
import pathlib
import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITES_FILE = ROOT / "data" / "sites.json"
STATE_DIR = ROOT / "data" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_TIMEOUT = 25

# Several state boards sit behind WAFs (Akamai, Cloudflare, Imperva) that
# reject self-identifying bot user-agents outright. We fetch one public
# newsroom page per site per day, so presenting as an ordinary browser is
# both accurate about our load and necessary to get a response at all.
# Accept-Encoding is deliberately NOT set here: requests advertises only what
# it can actually decode (brotli included, via the brotli dependency).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# A site that has failed this many runs in a row is reported by email. Without
# this, a blocked site is silently unmonitored forever.
STALE_AFTER_FAILURES = 3

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
        "consecutive_failures": 0,
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")


def record_failure(slug, error):
    """Increment the failure counter, preserving the last good hash/snapshot.

    Returns the new consecutive-failure count. State is written even on
    failure so a site that stops responding becomes visible instead of
    silently dropping out of the report.
    """
    meta_path, _ = state_paths(slug)
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}

    meta["consecutive_failures"] = meta.get("consecutive_failures", 0) + 1
    meta["last_error"] = error
    meta["last_failed"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta["consecutive_failures"]


def build_session():
    """Session that retries transient failures but not deterministic ones.

    403 is deliberately excluded: a WAF denial is a policy decision and
    retrying it just wastes time. Connection resets (Tennessee) and 5xx/429
    are worth a second attempt.
    """
    session = requests.Session()
    # Kept deliberately small: 34 sites x slow backoff turns a stuck run into
    # a very long one. One retry is enough for a transient reset.
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = build_session()


def fetch(url, extra_headers=None, allow_unverified_tls=False):
    """Fetch a page, returning (html, used_unverified_tls)."""
    headers = dict(BROWSER_HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    try:
        resp = SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.SSLError:
        # Some boards (e.g. ncdentalboard.org) serve an incomplete certificate
        # chain -- browsers paper over this by fetching the missing
        # intermediate via AIA, but OpenSSL does not. Only retry unverified
        # for sites explicitly opted in via sites.json, and report that we did.
        if not allow_unverified_tls:
            raise
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = SESSION.get(
            url, headers=headers, timeout=REQUEST_TIMEOUT, verify=False
        )
        resp.raise_for_status()
        return resp.text, True

    resp.raise_for_status()
    return resp.text, False


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


def env(name, default=None):
    """Read an env var, treating empty/whitespace-only values as unset.

    GitHub Actions sets the variable to an empty string when the secret it
    references does not exist, so a plain os.environ.get(name, default)
    never falls back to its default.
    """
    return os.environ.get(name, "").strip() or default


def parse_port(raw, default=587):
    try:
        return int(raw)
    except (TypeError, ValueError):
        if raw is not None:
            print(f"Invalid SMTP_PORT {raw!r}; using {default}.", file=sys.stderr)
        return default


def diff_summary(old_text, new_text, max_lines=15):
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    added = [l[1:].strip() for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:].strip() for l in diff if l.startswith("-") and not l.startswith("---")]
    added = added[:max_lines]
    removed = removed[:max_lines]
    return added, removed


def send_email(smtp_host, smtp_port, smtp_username, smtp_password, from_email, from_name, to_emails, subject, html_body, text_body):
    recipients = [e.strip() for e in to_emails if e.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    smtp_class = smtplib.SMTP_SSL if smtp_port == 465 else smtplib.SMTP
    with smtp_class(smtp_host, smtp_port, timeout=REQUEST_TIMEOUT) as server:
        if smtp_port != 465:
            server.starttls()
        if smtp_username and smtp_password:
            server.login(smtp_username, smtp_password)
        server.sendmail(from_email, recipients, msg.as_string())
    print(f"Sent email via SMTP ({smtp_host}:{smtp_port}) to {len(recipients)} recipient(s).")


def build_email(changes, stale=()):
    today = datetime.date.today().isoformat()
    if changes:
        subject = f"Dental board updates detected ({len(changes)} site{'s' if len(changes) != 1 else ''}) - {today}"
    else:
        subject = f"Dental board monitor: {len(stale)} site(s) unreachable - {today}"

    html_parts = [f"<h2>Dental board newsroom changes - {today}</h2>"]
    text_parts = [f"Dental board newsroom changes - {today}\n"]

    if not changes:
        html_parts.append("<p><em>No content changes detected.</em></p>")
        text_parts.append("No content changes detected.")

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

    if stale:
        html_parts.append(
            f"<hr><h3>&#9888; {len(stale)} site(s) not checked successfully "
            f"for {STALE_AFTER_FAILURES}+ runs</h3>"
            "<p>These boards are <strong>not being monitored</strong> until "
            "the fetch is fixed:</p><ul>"
        )
        text_parts.append(
            f"\n--- WARNING: {len(stale)} site(s) unreachable for "
            f"{STALE_AFTER_FAILURES}+ runs (NOT being monitored) ---"
        )
        for s in stale:
            html_parts.append(
                f"<li><strong>{s['state']}</strong> "
                f"({s['consecutive_failures']} runs): "
                f"<a href='{s['url']}'>{s['url']}</a><br>"
                f"<code>{s['error']}</code></li>"
            )
            text_parts.append(
                f"  ! {s['state']} ({s['consecutive_failures']} runs): "
                f"{s['url']}\n    {s['error']}"
            )
        html_parts.append("</ul>")

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
            html, unverified = fetch(
                url,
                extra_headers=site.get("headers"),
                allow_unverified_tls=site.get("allow_unverified_tls", False),
            )
            text = extract_text(html)
            new_hash = hash_text(text)
            if unverified:
                print(f"[INSECURE] {site['state']}: fetched without TLS verification")
        except Exception as e:
            fails = record_failure(slug, str(e))
            errors.append({
                "state": site["state"],
                "url": url,
                "error": str(e),
                "consecutive_failures": fails,
            })
            print(
                f"[ERROR] {site['state']} ({url}): {e} "
                f"[{fails} run(s) in a row]",
                file=sys.stderr,
            )
            continue

        meta, old_text = load_state(slug)
        # .get(): a meta file written by record_failure() has no hash yet,
        # so a site whose first successful fetch follows failures is a baseline.
        old_hash = meta.get("hash") if meta else None
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

    stale = [e for e in errors if e["consecutive_failures"] >= STALE_AFTER_FAILURES]

    if errors:
        print(f"\n{len(errors)} site(s) failed to fetch:", file=sys.stderr)
        for e in errors:
            print(
                f"  - {e['state']} [{e['consecutive_failures']} in a row]: "
                f"{e['error']}",
                file=sys.stderr,
            )
    if stale:
        print(
            f"{len(stale)} site(s) unreachable for {STALE_AFTER_FAILURES}+ runs "
            "and are NOT being monitored.",
            file=sys.stderr,
        )

    if changes or stale:
        smtp_host = env("SMTP_HOST")
        smtp_username = env("SMTP_USERNAME")
        smtp_password = env("SMTP_PASSWORD")
        from_email = env("SMTP_FROM_EMAIL")
        from_name = env("SMTP_FROM_NAME", "USA Dental Report Board Monitor")
        to_emails = [e.strip() for e in env("ALERT_TO_EMAIL", "").split(",") if e.strip()]

        missing = [
            name
            for name, value in (
                ("SMTP_HOST", smtp_host),
                ("SMTP_FROM_EMAIL", from_email),
                ("ALERT_TO_EMAIL", to_emails),
            )
            if not value
        ]
        if missing:
            print(
                f"Not sending email: {', '.join(missing)} is not set. "
                f"{len(changes)} change(s) and {len(stale)} unreachable site(s) "
                "were recorded but NOT emailed. Add these as repository "
                "secrets to receive alerts.",
                file=sys.stderr,
            )
        else:
            smtp_port = parse_port(env("SMTP_PORT"))
            subject, html_body, text_body = build_email(changes, stale)
            send_email(smtp_host, smtp_port, smtp_username, smtp_password, from_email, from_name, to_emails, subject, html_body, text_body)
            print(
                f"Sent alert email for {len(changes)} changed site(s) "
                f"and {len(stale)} unreachable site(s)."
            )
    else:
        print("\nNo changes detected.")


if __name__ == "__main__":
    main()
