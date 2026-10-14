import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .crypto import decrypt


class MailNotConfigured(Exception):
    pass


def recipients(raw: str) -> list[str]:
    return [r for r in re.split(r"[,;\s]+", raw or "") if r]


def _send_sync(settings: dict[str, str], subject: str, body: str) -> None:
    host = settings.get("smtp_host", "").strip()
    to = recipients(settings.get("smtp_to", ""))
    if not host or not to:
        raise MailNotConfigured("未配置 SMTP 服务器或收件人")
    port = int(settings.get("smtp_port") or 465)
    security = settings.get("smtp_security", "ssl")
    username = settings.get("smtp_username", "")
    password = decrypt(settings.get("smtp_password_enc", ""))
    sender = settings.get("smtp_from", "").strip() or username

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    try:
        if security == "starttls":
            server.starttls(context=ctx)
        if username:
            server.login(username, password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send_mail(settings: dict[str, str], subject: str, body: str) -> None:
    await asyncio.to_thread(_send_sync, settings, subject, body)
