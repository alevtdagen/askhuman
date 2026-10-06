"""Outbound notifications carry a scoped link to the shared human response form."""

import asyncio
import hashlib
import hmac
import json
import smtplib
import ssl
import time
from email.message import EmailMessage

import httpx

from .config import Channel
from .models import Request
from .settings import Settings
from .store import Store


def reply_token(settings: Settings, request_id: str) -> str:
    return hmac.new(
        settings.signing_key.encode(), f"reply:{request_id}".encode(), hashlib.sha256
    ).hexdigest()


def reply_url(settings: Settings, request: Request) -> str:
    if request.kind == "notification":
        return settings.base_url
    return f"{settings.base_url}/reply/{request.id}#{reply_token(settings, request.id)}"


def email_send(settings: dict[str, str], message: str):
    mail = EmailMessage()
    mail["Subject"] = "AskHuman: your agent needs you"
    mail["From"] = settings["from"]
    mail["To"] = settings["to"]
    mail.set_content(message)
    use_tls = settings.get("security", "starttls") == "tls"
    port = int(settings.get("port") or (465 if use_tls else 587))
    context = ssl.create_default_context()
    if use_tls:
        connection = smtplib.SMTP_SSL(settings["host"], port, timeout=15, context=context)
    else:
        connection = smtplib.SMTP(settings["host"], port, timeout=15)
    with connection as smtp:
        if not use_tls:
            smtp.starttls(context=context)
        if settings.get("username"):
            smtp.login(settings["username"], settings.get("password", ""))
        smtp.send_message(mail)


async def send(
    channel: Channel, request: Request, url: str, delivery_id: int, client: httpx.AsyncClient
):
    values = channel.resolved()
    text = f"[{request.kind.upper()} · {request.urgency}] {request.question[:1400]}\n\n{url}"
    if channel.type == "web":
        return
    if channel.type == "email":
        await asyncio.to_thread(email_send, values, text)
        return
    if channel.type == "slack":
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        result = await client.post(
            values["url"],
            json={
                "text": "AskHuman: your agent needs you",
                "blocks": [
                    {"type": "section", "text": {"type": "plain_text", "text": escaped}},
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Open in AskHuman"},
                                "url": url,
                            }
                        ],
                    },
                ],
            },
        )
    elif channel.type == "teams":
        result = await client.post(
            values["url"],
            json={
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "contentUrl": None,
                        "content": {
                            "type": "AdaptiveCard",
                            "version": "1.2",
                            "body": [
                                {
                                    "type": "TextBlock",
                                    "text": request.question[:1400],
                                    "wrap": True,
                                },
                            ],
                            "actions": [
                                {"type": "Action.OpenUrl", "title": "Open in AskHuman", "url": url}
                            ],
                        },
                    }
                ],
            },
        )
    elif channel.type == "discord":
        result = await client.post(
            values["url"],
            json={
                "content": text,
                "allowed_mentions": {"parse": []},
            },
        )
    elif channel.type == "telegram":
        token = values["bot_token"]
        result = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": values["chat_id"],
                "text": text,
                "link_preview_options": {"is_disabled": True},
            },
        )
    elif channel.type in ("sms", "whatsapp"):
        prefix = "whatsapp:" if channel.type == "whatsapp" else ""
        result = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{values['account_sid']}/Messages.json",
            auth=(values["account_sid"], values["auth_token"]),
            data={
                "From": prefix + values["from"].removeprefix(prefix) if prefix else values["from"],
                "To": prefix + values["to"].removeprefix(prefix) if prefix else values["to"],
                "Body": text,
            },
        )
    else:
        timestamp = str(int(time.time()))
        payload = json.dumps(
            {
                "event": "human.requested",
                "delivery_id": str(delivery_id),
                "request": request.model_dump(mode="json"),
                "reply_url": url,
            },
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(
            values["secret"].encode(), timestamp.encode() + b"." + payload, hashlib.sha256
        ).hexdigest()
        result = await client.post(
            values["url"],
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-AskHuman-Timestamp": timestamp,
                "X-AskHuman-Signature": "sha256=" + signature,
                "X-AskHuman-Delivery": str(delivery_id),
            },
        )
    result.raise_for_status()
    # Telegram can report application failures inside a successful HTTP response.
    if channel.type == "telegram" and result.json().get("ok") is not True:
        raise ValueError("Telegram rejected the notification")


async def deliver_one(store: Store, settings: Settings, client: httpx.AsyncClient) -> bool:
    delivery = store.claim_delivery()
    if delivery is None:
        return False
    error = None
    try:
        channel = next((c for c in store.config().channels if c.id == delivery["channel"]), None)
        if channel is None or not channel.enabled:
            raise ValueError("Channel is missing or disabled")
        request = store.get(delivery["request_id"])
        await send(channel, request, reply_url(settings, request), delivery["id"], client)
    except httpx.HTTPStatusError as exc:
        error = f"Provider returned HTTP {exc.response.status_code}"
    except Exception as exc:
        # URLs and exception messages may contain tokens, SMTP credentials, or provider secrets.
        error = f"Delivery failed ({type(exc).__name__}); check channel settings and connectivity"
    store.finish_delivery(delivery, error)
    return True


async def worker(store: Store, settings: Settings):
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        while True:
            worked = await deliver_one(store, settings, client)
            await asyncio.sleep(0.05 if worked else 1)
