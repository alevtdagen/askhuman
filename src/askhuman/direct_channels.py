"""Direct human replies for embedded runtimes. All connections are outbound."""

import asyncio
import hashlib

import httpx

from .config import Channel
from .errors import ChannelConfigurationError
from .models import Request
from .store import Conflict, Store
from .terminal import parse_reply, prompt_text


class DirectChannel:
    source = ""

    def __init__(self, channel: Channel, store: Store):
        self.channel = channel
        self.store = store
        self.values = channel.resolved()
        self.allowed = {user.strip() for user in self.values["allowed_user_ids"].split(",")}
        identity = self.values["bot_token"] + self.values.get(
            "chat_id", self.values.get("channel_id", "")
        )
        self.key = channel.id + ":" + hashlib.sha256(identity.encode()).hexdigest()[:24]

    def accept(self, external_id: str, text: str, user: str) -> bool:
        if user not in self.allowed:
            return False
        request = self.store.bound_request(self.key, external_id)
        if not request or request.status != "pending":
            return False
        try:
            body = parse_reply(request, text, f"{self.source}:{user}")
            self.store.answer(request.id, body, self.source)
            return True
        except (ValueError, Conflict):
            # An invalid/duplicate reply cannot complete or authorize a request.
            return False


class TelegramPolling(DirectChannel):
    source = "telegram"

    def __init__(self, channel: Channel, store: Store, client: httpx.AsyncClient):
        super().__init__(channel, store)
        self.client = client
        self.task = None

    async def api(self, method: str, data: dict):
        response = await self.client.post(
            f"https://api.telegram.org/bot{self.values['bot_token']}/{method}",
            json=data,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("ok") is not True:
            raise ValueError("Telegram rejected the request; check the bot configuration")
        return payload["result"]

    async def start(self):
        # A configured webhook would make getUpdates fail. Do not delete it on the user's behalf.
        webhook = await self.api("getWebhookInfo", {})
        if webhook.get("url"):
            raise ChannelConfigurationError(
                "Telegram polling requires a dedicated bot without a webhook. "
                "The existing webhook was left unchanged."
            )
        self.task = asyncio.create_task(self.run())

    async def send(self, request: Request):
        text = prompt_text(request)
        if len(text.encode("utf-16-le")) // 2 > 4096:
            raise ChannelConfigurationError(
                "Question, context, and options exceed Telegram's 4096-character limit"
            )
        payload = {
            "chat_id": self.values["chat_id"],
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if request.kind != "notification":
            payload["reply_markup"] = {"force_reply": True, "selective": False}
        sent = await self.api("sendMessage", payload)
        self.store.bind_message(self.key, str(sent["message_id"]), request.id)

    def receive(self, update: dict) -> bool:
        message = update.get("message", {})
        sender = message.get("from", {})
        if (
            str(message.get("chat", {}).get("id")) != self.values["chat_id"]
            or sender.get("is_bot") is not False
            or message.get("forward_origin")
            or message.get("is_automatic_forward")
            or "reply_to_message" not in message
            or not isinstance(message.get("text"), str)
        ):
            return False
        return self.accept(
            str(message["reply_to_message"].get("message_id")),
            message["text"],
            str(sender.get("id")),
        )

    async def poll_once(self):
        offset = self.store.cursor(self.key)
        updates = await self.api(
            "getUpdates",
            {
                "offset": int(offset) if offset else 0,
                "timeout": 20,
                "allowed_updates": ["message"],
            },
        )
        for update in updates:
            self.receive(update)
            # Persist after applying the response. Already-answered messages are harmless on retry.
            self.store.set_cursor(self.key, str(update["update_id"] + 1))
        self.store.receiver_error(self.channel.id, None)

    async def run(self):
        delay = 1
        while True:
            try:
                await self.poll_once()
                delay = 1
                await asyncio.sleep(0.1)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.store.receiver_error(
                    self.channel.id, "Telegram receiver unavailable; retrying"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class SlackSocket(DirectChannel):
    source = "slack"

    def __init__(self, channel: Channel, store: Store):
        super().__init__(channel, store)
        self.socket = None
        self.session = None
        self.web = None

    async def start(self):
        try:
            import aiohttp
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.socket_mode.response import SocketModeResponse
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError as exc:
            raise ChannelConfigurationError(
                "Install the [slack] extra for Slack Socket Mode"
            ) from exc
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        self.web = AsyncWebClient(token=self.values["bot_token"], session=self.session)
        self.socket = SocketModeClient(app_token=self.values["app_token"], web_client=self.web)

        async def listener(client, request):
            if request.type == "events_api":
                self.receive(request.payload.get("event", {}))
            if request.envelope_id:
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=request.envelope_id),
                )

        self.socket.socket_mode_request_listeners.append(listener)
        await self.socket.connect()

    async def send(self, request: Request):
        text = prompt_text(request) + "\n\nReply in this message's thread."
        if len(text) > 40000:
            raise ChannelConfigurationError("Question exceeds Slack's message limit")
        sent = await self.web.chat_postMessage(
            channel=self.values["channel_id"],
            text=text,
            mrkdwn=False,
            parse="none",
            link_names=False,
            unfurl_links=False,
            unfurl_media=False,
        )
        self.store.bind_message(self.key, str(sent["ts"]), request.id)

    def receive(self, event: dict) -> bool:
        if (
            event.get("type") != "message"
            or event.get("subtype")
            or event.get("bot_id")
            or event.get("channel") != self.values["channel_id"]
            or not event.get("thread_ts")
            or not isinstance(event.get("text"), str)
        ):
            return False
        return self.accept(str(event["thread_ts"]), event["text"], str(event.get("user")))

    async def close(self):
        if self.socket:
            await self.socket.close()
        if self.session:
            await self.session.close()
