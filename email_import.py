"""
Reads Indeed job-alert emails over IMAP (stdlib imaplib/email, no extra
dependency -- same "zero extra dependency" convention as mailer.py's SMTP
sending) from a dedicated inbox the user set up Indeed's own "job alert"
feature to notify. This is not scraping Indeed's site: Indeed's job search
pages are Cloudflare-protected (confirmed directly: a plain fetch gets a
real bot-detection challenge page, not results) and building a bypass for
that is a line this project doesn't cross -- same principle already
applied elsewhere here (PSNProfiles'/Untappd's image CDNs in the sibling
achievement-vault project hit the identical wall and were left alone
rather than worked around). Reading alert emails Indeed itself chose to
send, to an inbox the user owns, is a first-party channel, not a workaround.

Returns plain dicts, not scraper.Article -- this module has no dependency
on scraper.py (avoids a circular import, since scraper.py is what calls
into this module), and construction of the actual Article happens in
scraper.py's scrape_email(), same as every other kind.

The HTML parsing below (INDEED_LINK_RE) was originally best-effort, built
without a real sample email to test against. Validated against a real
digest email on 2026-09-28 -- extraction itself worked well out of the
box (8/8 real job links, correctly matched to the alert's own search
terms), but needed two fixes:
  - Titles/hrefs contain literal HTML entities (&amp;, &#39;, ...) straight
    from the raw source -- not decoded anywhere before this, so a title
    could read "Un.e Agent d&#39;accueil" verbatim and a link's query
    string would contain "&amp;" instead of "&". Fixed with html.unescape()
    on both.
  - FROM_SEARCH ("indeed" in the From header) missed the real email
    entirely -- it turned out to be *forwarded* by the user into this
    inbox (From: their own address, Subject: "Fwd: ..."), not delivered
    directly by Indeed. Since this inbox is single-purpose (nothing else
    is ever expected to land here), the fix was to drop the sender filter
    and just read every message in it, not to special-case forwards.
"""

import os
import re
import html
import logging
import imaplib
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("news_bot.email_import")

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_EMAIL = os.environ.get("IMAP_EMAIL", "")
IMAP_APP_PASSWORD = os.environ.get("IMAP_APP_PASSWORD", "")
IMAP_TIMEOUT = 20

# A job link in an Indeed alert email, with visible link text
# long enough to plausibly be a job title (short nav-link text like "Voir
# l'offre" would fail this length check and be skipped, which is the
# intent -- we only want title-bearing anchors, not "view/apply" buttons).
INDEED_LINK_RE = re.compile(
    r'<a[^>]+href="([^"]*indeed\.[a-z]+/(?:rc/clk|viewjob|pagead)[^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
TAG_RE = re.compile(r"<[^>]+>")


def is_configured() -> bool:
    return bool(IMAP_EMAIL and IMAP_APP_PASSWORD)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        (part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part)
        for part, enc in parts
    )


def _html_body(msg) -> str | None:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                try:
                    return part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    continue
        return None
    if msg.get_content_type() == "text/html":
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return None


def _clean_title(title_fragment: str) -> str:
    # html.unescape() after stripping tags, not before -- entities can
    # appear inside the visible text itself (an apostrophe as &#39;, e.g.
    # a real title seen verbatim: "Un.e Agent d&#39;accueil"), which
    # would otherwise show up literally instead of as the real character.
    text = html.unescape(TAG_RE.sub("", title_fragment))
    return re.sub(r"\s+", " ", text).strip()


def extract_indeed_jk(href: str) -> str | None:
    """Indeed's `rc/clk` job links carry a stable `jk=<job key>` param
    identifying the actual posting, wrapped in per-email tracking noise
    (`qd=`, `tk=`, `alid=`, `bb=`, `rd=`, ...) that differs on every single
    digest even for the exact same job -- real duplicates confirmed
    directly: three separate emails all linking the same `jk=` value, each
    with a different `qd=`.

    **An earlier version of this function *rewrote* the link down to a bare
    `https://<host>/viewjob?jk=<key>` form, on the assumption that was a
    real, standalone-working Indeed URL (Indeed's own "copy link" gives you
    exactly that shape). That assumption was wrong, confirmed directly
    (`curl` against a real freshly-canonicalized link): Indeed's Cloudflare
    bot-detection returns 401 "Authenticating..." → forced login for a bare
    `/viewjob?jk=` hit with none of the signed tracking context a real
    click carries, while the *original* `rc/clk` tracked link is what
    Indeed's bot-detection actually expects a genuine email click to look
    like. Reported directly as "todays indeed job link not working" --
    the rewritten links were the cause, not a pre-existing Indeed problem.
    This function now only *extracts* the `jk` for duplicate-detection
    purposes (see scraper.scrape_email) -- it never rewrites or returns a
    modified URL. The original tracked href is kept as the article's real,
    clickable `link` so a genuine browser click still goes through
    Indeed's intended (and much more likely to actually work) redirect
    chain. The unrecoverable cost: every link already rewritten by the old
    version before this fix landed has lost its original tracking token
    for good -- those can't be un-broken, only new imports are fixed."""
    parsed = urlparse(href)
    return parse_qs(parsed.query).get("jk", [None])[0]


def _parse_message(raw: bytes) -> tuple[list[dict], str | None]:
    msg = message_from_bytes(raw)
    body_html = _html_body(msg)
    if not body_html:
        return [], None

    pub_date = None
    date_header = msg.get("Date")
    if date_header:
        try:
            pub_date = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            pub_date = None

    # Keyed by jk when the link has one, by the raw href otherwise (a
    # pagead link has no stable id at all -- see extract_indeed_jk) --
    # this is purely about not returning the same job twice from *this one*
    # email; it's not the cross-email dedup (that needs a DB lookup, done
    # in scraper.scrape_email, since a job can recur across separate
    # messages this function never sees together).
    seen = set()
    jobs = []
    for raw_href, title_html in INDEED_LINK_RE.findall(body_html):
        # Same reasoning as _clean_title: a real href was seen verbatim as
        # ".../dl?jk=...&amp;from=ja&amp;qd=..." -- valid-looking but wrong,
        # since &amp; is HTML's escaping of & for display, not a real
        # query-string separator the target server should receive encoded.
        # Kept verbatim (not rewritten) -- see extract_indeed_jk's
        # docstring for why a rewritten link breaks Indeed's own
        # bot-detection on a real click.
        href = html.unescape(raw_href)
        title = _clean_title(title_html)
        dedup_key = extract_indeed_jk(href) or href
        if len(title) < 5 or dedup_key in seen:
            continue
        seen.add(dedup_key)
        jobs.append({"title": title, "link": href, "pub_date": pub_date})

    return jobs, _decode(msg.get("Subject"))


def fetch_indeed_jobs() -> list[dict]:
    """Connects, reads *every* message from Indeed regardless of its
    \\Seen flag, and returns every job link found across all of them.

    Originally searched "UNSEEN" only, on the assumption that was a safe,
    self-contained way to avoid re-parsing the same email every run. Hit a
    real bug on the very first live email: the \\Seen flag isn't ours
    alone to manage -- simply opening the mailbox in Gmail's web UI (to
    confirm the alert had arrived) flipped it to read, and the importer
    then silently found zero messages forever, confirmed directly (`FROM
    "indeed"` matched the message; `UNSEEN FROM "indeed"` didn't). Fixed
    by dropping the UNSEEN filter entirely and searching ALL instead. This
    is safe to re-run indefinitely despite re-parsing old emails every
    time: persist_articles() already dedups by article *link*, so a job
    seen in a previously-processed email just gets silently ignored again
    -- the only cost is some repeated IMAP/parsing work, negligible for a
    personal inbox getting a handful of alert emails a day. Still marks
    each message \\Seen after processing (success or failure -- a
    malformed email shouldn't be retried forever, same "graceful
    degradation over crashing" spirit as scrape_css's per-row fallbacks),
    now purely so the mailbox itself looks read, not because anything
    here depends on that flag anymore. Raises nothing on IMAP/parse
    failure -- logs and
    returns whatever was gathered before the failure, since scraper.py's
    scrape_source_safe() is what needs a clean, non-raising boundary here
    (this mirrors mailer.py's "log a warning, don't crash the daily job"
    contract for an unconfigured/unreachable optional channel)."""
    if not is_configured():
        logger.warning(
            "IMAP_EMAIL/IMAP_APP_PASSWORD aren't set in .env -- skipping Indeed email-alert import."
        )
        return []

    jobs: list[dict] = []
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
            imap.login(IMAP_EMAIL, IMAP_APP_PASSWORD)
            imap.select("INBOX")
            # No sender filter -- see the module docstring for why (a real
            # digest arrived forwarded by the user, not sent directly by
            # Indeed, and a FROM "indeed" search missed it entirely). This
            # inbox is single-purpose, so "every message in it" is already
            # the right scope.
            status, data = imap.search(None, "ALL")
            if status != "OK":
                logger.error(f"IMAP search failed: {status}")
                return jobs

            msg_ids = data[0].split()
            for msg_id in msg_ids:
                try:
                    status, msg_data = imap.fetch(msg_id, "(RFC822)")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    parsed_jobs, subject = _parse_message(raw)
                    jobs.extend(parsed_jobs)
                    if not parsed_jobs:
                        logger.info(
                            f"Indeed alert email {msg_id!r} (subject: {subject!r}) yielded no job "
                            "links -- the parsing pattern likely needs adjusting against this "
                            "email's real markup."
                        )
                except Exception as e:
                    logger.exception(f"Failed to parse Indeed alert email {msg_id!r}: {e}")
                finally:
                    # Mark seen regardless of outcome -- see docstring.
                    imap.store(msg_id, "+FLAGS", "\\Seen")
    except (OSError, imaplib.IMAP4.error) as e:
        logger.error(f"Failed to connect/read Indeed alert inbox: {e}")

    return jobs
