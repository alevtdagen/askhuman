import hashlib
import hmac
import json
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from askhuman.channels import deliver_one, reply_url, send
from askhuman.config import Channel, RoutingConfig
from askhuman.models import Question
from askhuman.store import Store

SETTINGS = {
    "web": {},
    "slack": {"url": "https://hooks.example.com/slack"},
    "teams": {"url": "https://hooks.example.com/teams"},
    "discord": {"url": "https://hooks.example.com/discord"},
    "telegram": {"bot_token": "bot-secret", "chat_id": "123"},
    "sms": {
        "account_sid": "AC123",
        "auth_token": "twilio-secret",
        "from": "+15550001",
        "to": "+15550002",
    },
    "whatsapp": {
        "account_sid": "AC123",
        "auth_token": "twilio-secret",
        "from": "+15550001",
        "to": "+15550002",
    },
    "webhook": {"url": "https://hooks.example.com/custom", "secret": "signing-secret"},
    "email": {"host": "smtp.example.com", "from": "agent@example.com", "to": "human@example.com"},
}


@pytest.mark.parametrize("kind", list(SETTINGS))
async def test_channel_delivery_contracts(kind, app, settings, monkeypatch):
    question = app.state.store.create(Question(question="Which source? <!channel> @everyone"), None)
    url = reply_url(settings, question)
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    mail = []
    monkeypatch.setattr(
        "askhuman.channels.email_send", lambda values, message: mail.append(message)
    )
    channel = Channel(id="test", type=kind, settings=SETTINGS[kind])
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        await send(channel, question, url, 42, client)
    if kind == "web":
        assert calls == []
    elif kind == "email":
        assert url in mail[0]
    elif kind in ("sms", "whatsapp"):
        body = parse_qs(calls[0].content.decode())
        assert url in body["Body"][0]
        assert body["To"][0] == ("whatsapp:" if kind == "whatsapp" else "") + "+15550002"
        assert calls[0].headers["Authorization"].startswith("Basic ")
    else:
        body = json.loads(calls[0].content)
        assert url in calls[0].content.decode()
        if kind == "discord":
            assert body["allowed_mentions"]["parse"] == []
        if kind == "slack":
            assert body["blocks"][0]["text"]["type"] == "plain_text"
        if kind == "teams":
            assert body["attachments"][0]["content"]["type"] == "AdaptiveCard"
        if kind == "webhook":
            stamp = calls[0].headers["X-AskHuman-Timestamp"]
            expected = hmac.new(
                b"signing-secret", stamp.encode() + b"." + calls[0].content, hashlib.sha256
            ).hexdigest()
            assert calls[0].headers["X-AskHuman-Signature"] == "sha256=" + expected
            assert body["delivery_id"] == "42"
            assert body["request"]["id"] == question.id


async def test_durable_retry_redacts_secrets_and_recovers_after_restart(app, settings):
    store = app.state.store
    store.configure(
        RoutingConfig(
            channels=[
                Channel(
                    id="slack",
                    type="slack",
                    settings={
                        "url": "https://hooks.example.com/secret-value",
                    },
                )
            ],
            default_channels=["slack"],
        )
    )
    request = store.create(Question(question="Which source?"), None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="secret-value"))
    ) as client:
        assert await deliver_one(store, settings, client)
    failed = store.get(request.id).deliveries[0]
    assert failed.status == "pending" and failed.attempts == 1
    assert "secret-value" not in failed.error
    with store.connection() as db:
        db.execute("UPDATE deliveries SET due=0")
    reopened = Store(store.path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200))
    ) as client:
        assert await deliver_one(reopened, settings, client)
    assert reopened.get(request.id).deliveries[0].status == "delivered"


def test_lease_and_retry_limit(app):
    store = app.state.store
    store.configure(
        RoutingConfig(
            channels=[Channel(id="custom", type="webhook", settings=SETTINGS["webhook"])],
            default_channels=["custom"],
        )
    )
    request = store.create(Question(question="Which source?"), None)
    first = store.claim_delivery()
    assert first and store.claim_delivery() is None
    for _ in range(4):
        with store.connection() as db:
            db.execute("UPDATE deliveries SET due=0")
        assert store.claim_delivery() is not None
    with store.connection() as db:
        db.execute("UPDATE deliveries SET due=0")
    assert store.claim_delivery() is None
    assert store.get(request.id).deliveries[0].status == "failed"


def test_cancelled_requests_skip_queued_notifications(app):
    store = app.state.store
    store.configure(
        RoutingConfig(
            channels=[Channel(id="custom", type="webhook", settings=SETTINGS["webhook"])],
            default_channels=["custom"],
        )
    )
    request = store.create(Question(question="Which source?"), None)
    store.cancel(request.id)
    assert store.claim_delivery() is None
    assert store.get(request.id).deliveries[0].status == "skipped"


def test_environment_settings_and_destination_validation(monkeypatch):
    channel = Channel(id="slack", type="slack", settings={"url": "env:TEST_SLACK_URL"})
    with pytest.raises(ValueError, match="Missing environment"):
        channel.resolved()
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.example.com/secret")
    assert channel.resolved()["url"].startswith("https://")
    monkeypatch.setenv("TEST_SLACK_URL", "http://insecure.example.com")
    with pytest.raises(ValidationError):
        channel.resolved()
    monkeypatch.setenv("TEST_SMTP_SECURITY", "tls")
    mail = Channel(
        id="mail",
        type="email",
        settings=SETTINGS["email"]
        | {
            "security": "env:TEST_SMTP_SECURITY",
        },
    )
    assert mail.resolved()["security"] == "tls"
    with pytest.raises(ValidationError):
        Channel(id="email", type="email", settings=SETTINGS["email"] | {"from": "a\nBcc: b"})


@pytest.mark.parametrize("security,expected_port", [("starttls", 587), ("tls", 465)])
def test_email_uses_validated_tls(monkeypatch, security, expected_port):
    from askhuman.channels import email_send

    events = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            events.append(("connect", host, port))
            assert kwargs["timeout"] == 15
            if security == "tls":
                assert kwargs["context"].check_hostname

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, context):
            assert context.check_hostname
            events.append(("starttls",))

        def login(self, user, password):
            events.append(("login", user))

        def send_message(self, message):
            assert "Reply here" in message.get_content()
            events.append(("send",))

    monkeypatch.setattr("askhuman.channels.smtplib.SMTP", SMTP)
    monkeypatch.setattr("askhuman.channels.smtplib.SMTP_SSL", SMTP)
    email_send(
        SETTINGS["email"] | {"security": security, "username": "sam", "password": "secret"},
        "Reply here",
    )
    assert events[0][2] == expected_port
    assert (("starttls",) in events) == (security == "starttls")
