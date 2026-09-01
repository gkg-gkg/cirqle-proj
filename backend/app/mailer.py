"""Sending transactional email (verification links, password resets).

Two modes, chosen by CIRQLE_EMAIL_MODE — the same "real service in production,
local fallback in dev" split that `storage.py` uses for images:

  • console (default) -> nothing is sent; the whole message is printed to the
    server log. The links are clickable in the terminal, so the full signup and
    reset flows are testable locally with no AWS setup at all.
  • ses               -> sent for real through Amazon SES in AWS_REGION.

Nothing here ever raises. A mail outage must not turn a working signup into a
500 — the caller gets False back and carries on, and the failure is logged.
"""
import os
import sys

FROM_DEFAULT = "Cirqle <noreply@cirqle.co.uk>"


def _mode() -> str:
    return os.environ.get("CIRQLE_EMAIL_MODE", "console").strip().lower()


def _sender() -> str:
    return os.environ.get("CIRQLE_EMAIL_FROM", FROM_DEFAULT)


def site_base() -> str:
    """Base URL of the *website* (not the API) used to build links in emails.

    The links point at pages like /reset-password.html, which are served by the
    frontend, so this is deliberately separate from the API's own address.
    """
    return os.environ.get("CIRQLE_SITE_BASE", "https://cirqle.co.uk").rstrip("/")


def send_email(to: str, subject: str, text: str, html: str = "") -> bool:
    """Send one message. Returns True if it went out, False if it didn't.

    `text` is the plain-text version and is required — some mail clients show it
    instead of the HTML, and having it improves spam scores.
    """
    if _mode() != "ses":
        print(
            f"\n─── EMAIL (console mode, not sent) ───\n"
            f"To:      {to}\n"
            f"From:    {_sender()}\n"
            f"Subject: {subject}\n\n"
            f"{text}\n"
            f"──────────────────────────────────────\n",
            file=sys.stderr, flush=True,
        )
        return True

    try:
        import boto3  # imported lazily so local dev never needs boto3/AWS
        body = {"Text": {"Data": text, "Charset": "UTF-8"}}
        if html:
            body["Html"] = {"Data": html, "Charset": "UTF-8"}
        region = os.environ.get("AWS_REGION", "eu-west-2")
        boto3.client("ses", region_name=region).send_email(
            Source=_sender(),
            Destination={"ToAddresses": [to]},
            Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body},
        )
        return True
    except Exception as exc:                                  # noqa: BLE001
        # Bad address, SES sandbox rejection, missing IAM permission, network —
        # all handled the same way: log it and let the request succeed.
        print(f"[mailer] failed to send to {to}: {exc}", file=sys.stderr, flush=True)
        return False


# ── The messages we actually send ────────────────────────────────────────────
# Styling is inline because email clients strip <style> blocks. Kept plain and
# text-first: transactional mail that looks like marketing lands in spam.

def _layout(heading: str, intro: str, button: str, url: str, footer: str) -> str:
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:480px;margin:0 auto;padding:32px 24px;color:#1a1a1a">
  <div style="font-size:20px;font-weight:600;letter-spacing:-0.02em">Cirqle</div>
  <h1 style="font-size:22px;font-weight:600;margin:28px 0 12px">{heading}</h1>
  <p style="font-size:15px;line-height:1.6;color:#444;margin:0 0 24px">{intro}</p>
  <a href="{url}" style="display:inline-block;background:#1a1a1a;color:#fff;
     text-decoration:none;padding:13px 24px;border-radius:8px;font-size:15px;
     font-weight:500">{button}</a>
  <p style="font-size:13px;line-height:1.6;color:#888;margin:28px 0 0">
    Or paste this into your browser:<br>
    <span style="color:#555;word-break:break-all">{url}</span>
  </p>
  <p style="font-size:13px;line-height:1.6;color:#888;margin:20px 0 0;
            border-top:1px solid #eee;padding-top:20px">{footer}</p>
</div>"""


def send_verification(to: str, first_name: str, token: str) -> bool:
    url = f"{site_base()}/verify-email.html?token={token}"
    name = first_name or "there"
    text = (f"Hi {name},\n\n"
            f"Confirm your email address to finish setting up your Cirqle account:\n\n"
            f"{url}\n\n"
            f"This link expires in 24 hours.\n\n"
            f"If you didn't sign up for Cirqle, you can ignore this email.")
    html = _layout(
        "Confirm your email",
        f"Hi {name}, confirm this address to finish setting up your Cirqle account.",
        "Confirm email", url,
        "This link expires in 24 hours. If you didn't sign up for Cirqle, "
        "you can safely ignore this email.")
    return send_email(to, "Confirm your Cirqle email address", text, html)


def send_password_reset(to: str, first_name: str, token: str) -> bool:
    url = f"{site_base()}/reset-password.html?token={token}"
    name = first_name or "there"
    text = (f"Hi {name},\n\n"
            f"Reset your Cirqle password here:\n\n"
            f"{url}\n\n"
            f"This link expires in 1 hour and can only be used once.\n\n"
            f"If you didn't request this, you can ignore this email — your "
            f"password hasn't changed.")
    html = _layout(
        "Reset your password",
        f"Hi {name}, use the button below to choose a new Cirqle password.",
        "Reset password", url,
        "This link expires in 1 hour and can only be used once. If you didn't "
        "request it, you can ignore this email — your password hasn't changed.")
    return send_email(to, "Reset your Cirqle password", text, html)


def send_email_change(to: str, first_name: str, token: str) -> bool:
    """Sent to the NEW address — clicking it is what proves they own it."""
    url = f"{site_base()}/verify-email.html?token={token}&change=1"
    name = first_name or "there"
    text = (f"Hi {name},\n\n"
            f"Confirm this address as the new email on your Cirqle account:\n\n"
            f"{url}\n\n"
            f"This link expires in 1 hour. Until you confirm, your account keeps "
            f"its current email address.\n\n"
            f"If you didn't request this, you can ignore this email.")
    html = _layout(
        "Confirm your new email",
        f"Hi {name}, confirm this address to make it the new email on your "
        f"Cirqle account.",
        "Confirm new email", url,
        "This link expires in 1 hour. Until you confirm, your account keeps its "
        "current email address. If you didn't request this, ignore this email.")
    return send_email(to, "Confirm your new Cirqle email address", text, html)


def send_merchant_invite(to: str, business_name: str, token: str) -> bool:
    """Sent when an admin creates a merchant login. The link is the ONLY way in
    — no password is generated, so nothing has to be relayed by hand."""
    url = f"{site_base()}/merchant-set-password.html?token={token}"
    name = business_name or "there"
    text = (f"Hi {name},\n\n"
            f"Your Cirqle merchant portal account is ready. Set your password to "
            f"get started:\n\n"
            f"{url}\n\n"
            f"This link expires in 7 days. If it does, ask us for a new one.")
    html = _layout(
        "Your merchant portal is ready",
        f"Hi {name}, set a password to start using your Cirqle merchant portal — "
        f"track your deals, see referrals and manage billing.",
        "Set your password", url,
        "This link expires in 7 days. If it expires, contact us for a new one.")
    return send_email(to, "Set up your Cirqle merchant account", text, html)


def send_merchant_reset(to: str, business_name: str, token: str) -> bool:
    url = f"{site_base()}/merchant-set-password.html?token={token}&reset=1"
    name = business_name or "there"
    text = (f"Hi {name},\n\n"
            f"Reset your Cirqle merchant portal password here:\n\n"
            f"{url}\n\n"
            f"This link expires in 1 hour and can only be used once.\n\n"
            f"If you didn't request this, ignore this email — nothing has changed.")
    html = _layout(
        "Reset your portal password",
        f"Hi {name}, use the button below to choose a new password for your "
        f"Cirqle merchant portal.",
        "Reset password", url,
        "This link expires in 1 hour and can only be used once. If you didn't "
        "request it, ignore this email — nothing has changed.")
    return send_email(to, "Reset your Cirqle merchant password", text, html)
