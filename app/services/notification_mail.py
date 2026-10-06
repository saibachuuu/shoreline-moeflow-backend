"""Protected synchronous SMTP adapter; no broker dependency and no secret-bearing errors."""

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate
from flask import current_app


def send_notification_mail(
    address, subject, text, html=None, *, message_id, reply_to=None
):
    return send_protected_mail(
        current_app.config,
        address,
        subject,
        text,
        html,
        message_id=message_id,
        reply_to=reply_to,
    )


def send_protected_mail(
    config,
    address,
    subject,
    text,
    html=None,
    *,
    message_id,
    reply_to=None,
    user_mail=True,
):
    if not config.get("ENABLE_USER_EMAIL" if user_mail else "ENABLE_LOG_EMAIL"):
        return "skipped", "channel_disabled"
    if not isinstance(address, str) or not re.fullmatch(
        r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", address
    ):
        return "skipped", "invalid_email"
    address = address.strip().lower()
    allowlist = config.get("NOTIFICATION_EMAIL_ALLOWLIST", [])
    unrestricted = config.get("NOTIFICATION_EMAIL_UNRESTRICTED", False)
    # Development never bypasses its explicit allowlist, even with the production flag.
    if (
        config.get("TESTING") or config.get("DEBUG") or not unrestricted
    ) and address not in allowlist:
        return "skipped", "recipient_not_allowed"
    if not config.get("EMAIL_SMTP_HOST") or not config.get("EMAIL_ADDRESS"):
        return "skipped", "smtp_unconfigured"
    if any(
        "\r" in str(v) or "\n" in str(v)
        for v in (subject, config["EMAIL_ADDRESS"], reply_to or "", message_id)
    ):
        return "failed", "invalid_header"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["EMAIL_ADDRESS"]
    message["To"] = address
    message["Message-ID"] = message_id
    message["Date"] = formatdate(localtime=False)
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    client = None
    sending = False
    accepted = False
    try:
        cls = smtplib.SMTP_SSL if config.get("EMAIL_USE_SSL") else smtplib.SMTP
        options = {"timeout": 10}
        tls_context = ssl.create_default_context()
        if config.get("EMAIL_USE_SSL"):
            options["context"] = tls_context
        client = cls(
            config["EMAIL_SMTP_HOST"],
            int(config.get("EMAIL_SMTP_PORT") or 465),
            **options,
        )
        if not config.get("EMAIL_USE_SSL"):
            client.starttls(context=tls_context)
        if config.get("EMAIL_USERNAME"):
            client.login(config["EMAIL_USERNAME"], config.get("EMAIL_PASSWORD", ""))
        sending = True
        refused = client.send_message(
            message, from_addr=config["EMAIL_ADDRESS"], to_addrs=[address]
        )
        if refused:
            code = next(iter(refused.values()))[0]
            return (
                "retry_wait" if 400 <= code < 500 else "failed"
            ), "recipient_refused"
        accepted = True
        return "accepted", ""
    except smtplib.SMTPRecipientsRefused as exc:
        codes = [v[0] for v in exc.recipients.values()]
        return (
            "retry_wait" if codes and all(400 <= c < 500 for c in codes) else "failed"
        ), "recipient_refused"
    except smtplib.SMTPResponseException as exc:
        return (
            "retry_wait" if 400 <= exc.smtp_code < 500 else "failed"
        ), "smtp_response"
    except (smtplib.SMTPNotSupportedError, ssl.SSLCertVerificationError, ValueError):
        return "failed", "smtp_configuration"
    except Exception:
        return (
            "accepted" if accepted else "unknown" if sending else "retry_wait"
        ), "transport_interrupted"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
