"""SMTP delivery. Every setting comes from environment variables."""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

REQUIRED = {
    "NEWSTRIAGE_SMTP_HOST": "SMTP server, e.g. smtp.example.com",
    "NEWSTRIAGE_SMTP_PORT": "port: 587 (STARTTLS) or 465 (SSL)",
    "NEWSTRIAGE_SMTP_USER": "SMTP login",
    "NEWSTRIAGE_SMTP_PASSWORD": "SMTP password or app password",
    "NEWSTRIAGE_MAIL_FROM": "sender address",
    "NEWSTRIAGE_MAIL_TO": "recipients, comma-separated",
}


def missing_settings(env=None) -> dict:
    env = env if env is not None else os.environ
    return {k: v for k, v in REQUIRED.items() if not env.get(k)}


def recipients(env) -> list[str]:
    return [a.strip() for a in env["NEWSTRIAGE_MAIL_TO"].split(",") if a.strip()]


def build_message(subject: str, html: str, text: str, issue_key: str, env) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env["NEWSTRIAGE_MAIL_FROM"]
    msg["To"] = ", ".join(recipients(env))
    msg["Message-ID"] = make_msgid(idstring=f"newstriage-{issue_key}")
    msg["X-Newstriage-Issue"] = issue_key
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send(subject: str, html: str, text: str, issue_key: str, env=None) -> dict:
    env = env if env is not None else os.environ
    miss = missing_settings(env)
    if miss:
        raise RuntimeError("mail settings not set: " + ", ".join(miss))
    msg = build_message(subject, html, text, issue_key, env)
    to = recipients(env)
    refused = _deliver(msg, to, env)
    accepted = [a for a in to if a not in refused]
    return {"message_id": msg["Message-ID"], "recipients": accepted,
            "refused": {a: f"{c} {m.decode(errors='replace') if isinstance(m, bytes) else m}"
                        for a, (c, m) in refused.items()}}


def _deliver(msg: EmailMessage, to: list[str], env) -> dict:
    """Sends; returns addresses the SMTP server refused immediately (smtplib is
    silent about partial refusal). Later bounces arrive at the sender mailbox."""
    port = int(env["NEWSTRIAGE_SMTP_PORT"])
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(env["NEWSTRIAGE_SMTP_HOST"], port, context=ctx, timeout=60) as s:
            s.login(env["NEWSTRIAGE_SMTP_USER"], env["NEWSTRIAGE_SMTP_PASSWORD"])
            return s.send_message(msg, to_addrs=to)
    with smtplib.SMTP(env["NEWSTRIAGE_SMTP_HOST"], port, timeout=60) as s:
        s.starttls(context=ctx)
        s.login(env["NEWSTRIAGE_SMTP_USER"], env["NEWSTRIAGE_SMTP_PASSWORD"])
        return s.send_message(msg, to_addrs=to)


def send_test(env=None) -> list[dict]:
    """A short test message, sent to each recipient separately so the server's
    answer for every address is visible."""
    env = env if env is not None else os.environ
    miss = missing_settings(env)
    if miss:
        raise RuntimeError("mail settings not set: " + ", ".join(miss))
    out = []
    for addr in recipients(env):
        msg = EmailMessage()
        msg["Subject"] = "[newstriage] test message"
        msg["From"] = env["NEWSTRIAGE_MAIL_FROM"]
        msg["To"] = addr
        msg["Message-ID"] = make_msgid(idstring="newstriage-test")
        msg.set_content(f"Test message from newstriage. If you can read this, {addr} receives digests.")
        try:
            refused = _deliver(msg, [addr], env)
            out.append({"to": addr, "ok": not refused, "detail": str(refused) if refused else "accepted by server"})
        except smtplib.SMTPException as e:
            out.append({"to": addr, "ok": False, "detail": f"{type(e).__name__}: {e}"})
    return out
