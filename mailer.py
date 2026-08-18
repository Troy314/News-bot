"""
Sends the daily digest by email to registered subscribers, via plain
smtplib (stdlib, no extra dependency) against whatever SMTP server is
configured in .env. If SMTP isn't configured, sending is a no-op that logs
a warning rather than crashing the daily job -- email is an optional
delivery channel alongside Discord, not a required one.
"""

import os
import smtplib
import logging
from email.message import EmailMessage

import db

logger = logging.getLogger("news_bot.mailer")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)


def is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def list_subscribers() -> list[str]:
    conn = db.get_conn()
    try:
        return [row["email"] for row in conn.execute("SELECT email FROM newsletter_subscribers")]
    finally:
        conn.close()


def add_subscriber(email: str) -> None:
    conn = db.get_conn()
    try:
        conn.execute("INSERT OR IGNORE INTO newsletter_subscribers (email) VALUES (?)", (email,))
        conn.commit()
    finally:
        conn.close()


def remove_subscriber(email: str) -> None:
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM newsletter_subscribers WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()


def send_digest_email(subject: str, html_body: str) -> int:
    """Send the digest HTML to every registered subscriber. Returns how many
    sends succeeded. Logs (not raises) on a per-recipient failure, so one
    bad address doesn't stop the rest from getting theirs."""
    subscribers = list_subscribers()
    if not subscribers:
        return 0
    if not is_configured():
        logger.warning(
            f"{len(subscribers)} newsletter subscriber(s) registered, but SMTP_HOST/SMTP_USER/"
            "SMTP_PASSWORD aren't set in .env -- skipping email send."
        )
        return 0

    sent = 0
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            for email in subscribers:
                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = SMTP_FROM
                msg["To"] = email
                msg.set_content("This email requires an HTML-capable client to view.")
                msg.add_alternative(html_body, subtype="html")
                try:
                    server.send_message(msg)
                    sent += 1
                except smtplib.SMTPException as e:
                    logger.error(f"Failed to email digest to {email}: {e}")
    except (smtplib.SMTPException, OSError) as e:
        logger.error(f"Failed to connect to SMTP server: {e}")

    return sent
