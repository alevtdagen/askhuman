import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from askhuman import AskHuman, AskHumanError, HumanDeliveryError
from askhuman.config import Channel, RoutingConfig
from askhuman.direct_channels import SlackSocket, TelegramPolling
from askhuman.models import Question
from askhuman.store import Store

TELEGRAM = {"bot_token": "test-bot-token", "chat_id": "123", "allowed_user_ids": "123,456"}
SLACK = {
    "bot_token": "test-bot-token",
    "app_token": "test-app-token",
    "channel_id": "C123",
    "allowed_user_ids": "U123,U456",
}


def configure(tmp_path, kind, values):
    config = RoutingConfig(
        channels=[Channel(id="human", type=kind, settings=values)], default_channels=["human"]
    )
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    return path


def telegram_message(text="Approve", user=123, chat=123, reply_to=42):
    return {
        "update_id": 10,
        "message": {
            "message_id": 99,
            "text": text,
            "from": {"id": user, "is_bot": False},
            "chat": {"id": chat},
            "reply_to_message": {"message_id": reply_to},
        },
    }


async def test_telegram_roundtrip_and_offline_resume_with_persisted_binding(tmp_path):
    path = configure(tmp_path, "telegram_polling", TELEGRAM)
    updates, offsets, sent = [], [], []

    async def provider(request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        if method == "sendMessage":
            sent.append(payload)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
        assert method == "getUpdates"
        offsets.append(payload["offset"])
        await asyncio.sleep(0.01)
        batch = updates[:]
        updates.clear()
        return httpx.Response(200, json={"ok": True, "result": batch})

    async with AskHuman(
        mode="embedded", data_dir=tmp_path, config=path, transport=httpx.MockTransport(provider)
    ) as human:
        request = await human.create("May I deploy?", kind="approval")
        assert request.deliveries[0].status == "delivered"
    updates.append(telegram_message("Reject\nWait for tests"))
    async with AskHuman(
        mode="embedded", data_dir=tmp_path, config=path, transport=httpx.MockTransport(provider)
    ) as restarted:
        answer = await restarted.wait(request.id, wait_timeout=2)
        assert answer.approved is False and answer.answer == "Wait for tests"
        assert answer.respondent == "telegram:123" and answer.source == "telegram"
        adapter = restarted._backend.adapters["human"]
        assert adapter.store.cursor(adapter.key) == "11"
        await adapter.poll_once()
        assert offsets[-1] == 11
    assert len(sent) == 1 and sent[0]["reply_markup"]["force_reply"] is True
    assert "http://" not in sent[0]["text"]


@pytest.mark.parametrize(
    "change",
    [
        {"user": 999},
        {"chat": -123},
        {"reply_to": 999},
        {"text": "yes"},
    ],
)
async def test_telegram_rejects_unapproved_people_chats_threads_and_ambiguous_approval(
    tmp_path, change
):
    config = configure(tmp_path, "telegram_polling", TELEGRAM)
    store = Store(
        tmp_path / "state.sqlite3",
        default_config=RoutingConfig.model_validate_json(config.read_text()),
    )
    request = store.create(Question(question="May I deploy?", kind="approval"), None)
    async with httpx.AsyncClient() as http:
        adapter = TelegramPolling(store.config().channels[0], store, http)
        store.bind_message(adapter.key, "42", request.id)
        assert adapter.receive(telegram_message(**change)) is False
        assert store.get(request.id).status == "pending"
        assert adapter.receive(telegram_message("Approve")) is True
        assert adapter.receive(telegram_message("Reject")) is False
        assert store.get(request.id).response.approved is True


async def test_telegram_ignores_forwarded_edited_and_bot_messages(tmp_path):
    config = configure(tmp_path, "telegram_polling", TELEGRAM)
    store = Store(
        tmp_path / "state.sqlite3",
        default_config=RoutingConfig.model_validate_json(config.read_text()),
    )
    request = store.create(Question(question="Proceed?", kind="approval"), None)
    async with httpx.AsyncClient() as http:
        adapter = TelegramPolling(store.config().channels[0], store, http)
        store.bind_message(adapter.key, "42", request.id)
        forwarded = telegram_message()
        forwarded["message"]["forward_origin"] = {"type": "user"}
        bot = telegram_message()
        bot["message"]["from"]["is_bot"] = True
        for update in [forwarded, bot, {"edited_message": telegram_message()["message"]}]:
            assert adapter.receive(update) is False
    assert store.get(request.id).status == "pending"


async def test_existing_telegram_webhook_is_not_deleted_and_error_hides_token(tmp_path):
    path = configure(tmp_path, "telegram_polling", TELEGRAM)
    calls = []

    def provider(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200, json={"ok": True, "result": {"url": "https://existing.example/hook"}}
        )

    async with AskHuman(
        mode="embedded", data_dir=tmp_path, config=path, transport=httpx.MockTransport(provider)
    ) as human:
        with pytest.raises(AskHumanError) as error:
            await human.create("Test")
        assert TELEGRAM["bot_token"] not in str(error.value)
    assert calls == ["getWebhookInfo"]


async def test_oversized_message_is_reported_as_failed_not_silently_truncated(tmp_path):
    path = configure(tmp_path, "telegram_polling", TELEGRAM)

    async def provider(request):
        method = request.url.path.rsplit("/", 1)[-1]
        await asyncio.sleep(0.01)
        assert method != "sendMessage"
        return httpx.Response(
            200, json={"ok": True, "result": {"url": ""} if method == "getWebhookInfo" else []}
        )

    async with AskHuman(
        mode="embedded", data_dir=tmp_path, config=path, transport=httpx.MockTransport(provider)
    ) as human:
        request = await human.create("A" * 5000)
        with pytest.raises(HumanDeliveryError):
            await human.wait(request.id, wait_timeout=1)
        assert (await human.get(request.id)).response is None


async def test_slack_socket_lifecycle_thread_reply_and_ack_without_a_server(tmp_path, monkeypatch):
    pytest.importorskip("slack_sdk")
    sockets, posted, acknowledgements = [], [], []

    class Socket:
        def __init__(self, app_token, web_client):
            assert app_token == SLACK["app_token"]
            self.socket_mode_request_listeners = []
            self.closed = False
            sockets.append(self)

        async def connect(self):
            pass

        async def close(self):
            self.closed = True

        async def send_socket_mode_response(self, response):
            acknowledgements.append(response.envelope_id)

    async def post(self, **kwargs):
        posted.append(kwargs)
        return {"ok": True, "ts": "123.456"}

    monkeypatch.setattr("slack_sdk.socket_mode.aiohttp.SocketModeClient", Socket)
    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", post)
    path = configure(tmp_path, "slack_socket", SLACK)
    async with AskHuman(mode="embedded", data_dir=tmp_path, config=path) as human:
        request = await human.create("Which definition?", options=["Finance", "CRM"])
        event = {
            "type": "message",
            "channel": "C123",
            "user": "U123",
            "thread_ts": "123.456",
            "text": "2\nUse the CRM definition",
        }
        await sockets[0].socket_mode_request_listeners[0](
            sockets[0],
            SimpleNamespace(type="events_api", payload={"event": event}, envelope_id="envelope-1"),
        )
        answer = await human.wait(request.id, wait_timeout=1)
        assert answer.selected_option == "CRM" and answer.respondent == "slack:U123"
        assert answer.source == "slack" and acknowledgements == ["envelope-1"]
    assert sockets[0].closed and posted[0]["mrkdwn"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"user": "U999"},
        {"channel": "C999"},
        {"thread_ts": "999.1"},
        {"bot_id": "B1"},
        {"subtype": "message_changed"},
        {"text": "yes"},
    ],
)
def test_slack_ignores_unauthorized_or_ambiguous_messages(tmp_path, change):
    path = configure(tmp_path, "slack_socket", SLACK)
    store = Store(
        tmp_path / "state.sqlite3",
        default_config=RoutingConfig.model_validate_json(path.read_text()),
    )
    request = store.create(Question(question="Approve?", kind="approval"), None)
    adapter = SlackSocket(store.config().channels[0], store)
    store.bind_message(adapter.key, "123.456", request.id)
    event = {
        "type": "message",
        "channel": "C123",
        "user": "U123",
        "thread_ts": "123.456",
        "text": "Approve",
    }
    assert adapter.receive(event | change) is False
    assert store.get(request.id).response is None


async def test_duplicate_pollers_are_rejected_before_contacting_provider(tmp_path):
    config = RoutingConfig(
        channels=[
            Channel(id="a", type="telegram_polling", settings=TELEGRAM),
            Channel(id="b", type="telegram_polling", settings=TELEGRAM | {"chat_id": "456"}),
        ],
        default_channels=["a", "b"],
    )
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    calls = []

    def network(request):
        calls.append(request)
        raise AssertionError("Duplicate polling must be rejected before contacting Telegram")

    async with AskHuman(
        mode="embedded", data_dir=tmp_path, config=path, transport=httpx.MockTransport(network)
    ) as human:
        with pytest.raises(AskHumanError, match="dedicated bot/app"):
            await human.create("Question")
    assert calls == []


async def test_resume_timeout_includes_receiver_startup_and_releases_lock(tmp_path):
    path = configure(tmp_path, "telegram_polling", TELEGRAM)
    store = Store(
        tmp_path / "embedded.sqlite3",
        default_config=RoutingConfig.model_validate_json(path.read_text()),
    )
    request = store.create(Question(question="Question"), None)

    async def network(request):
        await asyncio.sleep(10)
        raise AssertionError("The receiver startup should have been cancelled")

    from askhuman import HumanTimeout

    async with AskHuman(
        mode="embedded", data_dir=tmp_path, config=path, transport=httpx.MockTransport(network)
    ) as human:
        with pytest.raises(HumanTimeout):
            await asyncio.wait_for(human.wait(request.id, wait_timeout=0.05), timeout=0.5)
        assert human._backend.lock.file is None
        assert (await human.get(request.id)).status == "pending"
