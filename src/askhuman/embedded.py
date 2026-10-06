"""An embedded request engine. No HTTP listener, API keys, or server initialization."""

import asyncio
import contextlib
import os
from pathlib import Path

import httpx

from .config import RoutingConfig
from .direct_channels import SlackSocket, TelegramPolling
from .errors import AskHumanError, ChannelConfigurationError, HumanDeliveryError
from .models import Question, Request
from .settings import data_directory
from .store import Conflict, Store
from .terminal import Terminal


def config_path(directory: Path) -> Path:
    return Path(os.environ.get("ASKHUMAN_CONFIG", str(directory / "config.json"))).expanduser()


class RuntimeLock:
    """OS lock released automatically on process death; only one receiver owns a workspace."""

    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self):
        self.file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.release()
            raise AskHumanError(
                "An embedded runtime already owns this data directory. Share one AskHuman "
                "instance, use a separate directory, or connect to the optional server."
            ) from exc

    def release(self):
        if self.file:
            self.file.close()
            self.file = None


class EmbeddedEngine:
    def __init__(self, *, data_dir=None, config=None, interactive=True, transport=None):
        self.directory = Path(data_dir or data_directory()).expanduser()
        path = Path(config).expanduser() if config is not None else config_path(self.directory)
        supplied = (
            RoutingConfig.model_validate_json(path.read_text()).require_embedded()
            if config is not None or path.exists()
            else None
        )
        self.store = Store(
            self.directory / "embedded.sqlite3", default_config=RoutingConfig.embedded()
        )
        self.config = (supplied or self.store.config()).require_embedded()
        self.terminal = Terminal(interactive)
        self.client = httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False, transport=transport
        )
        self.adapters = {}
        self.lock = RuntimeLock(self.directory / "embedded.lock")
        self.start_lock = asyncio.Lock()
        self.dispatch_lock = asyncio.Lock()
        self.task = None
        self.started = False
        self.closed = False

    async def start(self):
        if self.closed:
            raise AskHumanError("This AskHuman instance is closed")
        async with self.start_lock:
            if self.started:
                return
            self.lock.acquire()
            try:
                # Never change an active runtime's routes from a second client's constructor.
                self.store.configure(self.config)
                receivers = set()
                for channel in self.config.channels:
                    if not channel.enabled or channel.type == "terminal":
                        continue
                    values = channel.resolved()
                    for key in ("bot_token", "app_token"):
                        if key not in values:
                            continue
                        identity = (channel.type, key, values[key])
                        if identity in receivers:
                            raise ChannelConfigurationError(
                                "Use a dedicated bot/app for each direct channel. Multiple routes "
                                "can reuse one channel; duplicate receivers would lose replies."
                            )
                        receivers.add(identity)
                for channel in self.config.channels:
                    if not channel.enabled or channel.type == "terminal":
                        continue
                    adapter = (
                        TelegramPolling(channel, self.store, self.client)
                        if channel.type == "telegram_polling"
                        else SlackSocket(channel, self.store)
                    )
                    self.adapters[channel.id] = adapter
                    await adapter.start()
                self.started = True
                self.task = asyncio.create_task(self.run())
            except BaseException as exc:
                try:
                    await asyncio.gather(
                        *(a.close() for a in self.adapters.values()), return_exceptions=True
                    )
                finally:
                    self.adapters.clear()
                    self.lock.release()
                if not isinstance(exc, Exception) or isinstance(exc, ChannelConfigurationError):
                    raise
                # Provider exceptions can contain access tokens; never propagate their messages.
                raise AskHumanError(
                    f"Cannot start embedded receivers ({type(exc).__name__}). Check channel "
                    "credentials, required extras, and that the bot has no other receiver."
                ) from None

    async def create(self, question: Question, key: str) -> Request:
        await self.start()
        try:
            request = self.store.create(question, key)
        except Conflict as exc:
            raise AskHumanError(str(exc), status_code=409) from exc
        # Send before returning, so a notification still works in a short-lived context.
        await self.dispatch(request.id)
        return self.store.get(request.id)

    async def get(self, request_id: str) -> Request:
        try:
            return self.store.get(request_id)
        except KeyError as exc:
            raise AskHumanError("Request not found", status_code=404) from exc

    async def cancel(self, request_id: str) -> Request:
        try:
            return self.store.cancel(request_id)
        except Conflict as exc:
            raise AskHumanError(str(exc), status_code=409) from exc
        except KeyError as exc:
            raise AskHumanError("Request not found", status_code=404) from exc

    async def dispatch(self, request_id: str | None = None):
        async with self.dispatch_lock:
            while delivery := self.store.claim_delivery(request_id):
                error, permanent = None, False
                try:
                    request = self.store.get(delivery["request_id"])
                    channel = next(c for c in self.config.channels if c.id == delivery["channel"])
                    if not channel.enabled:
                        raise ValueError("Channel disabled")
                    if channel.type == "terminal":
                        if request.kind == "notification":
                            self.terminal.notify(request)
                    else:
                        await self.adapters[channel.id].send(request)
                except Exception as exc:
                    error = (
                        str(exc)
                        if isinstance(exc, ChannelConfigurationError)
                        else (
                            f"Delivery failed ({type(exc).__name__}); "
                            "check the channel configuration"
                        )
                    )
                    permanent = isinstance(
                        exc,
                        (
                            ChannelConfigurationError,
                            ValueError,
                            KeyError,
                            StopIteration,
                        ),
                    )
                self.store.finish_delivery(delivery, error, permanent=permanent)

    async def run(self):
        while True:
            await self.dispatch()
            await asyncio.sleep(0.25)

    async def wait(self, request_id: str, seconds: float | None) -> Request:
        request = await self.get(request_id)
        if request.status != "pending" or (seconds is not None and seconds <= 0):
            return request
        await self.start()

        async def watch():
            while True:
                result = await self.get(request_id)
                if result.status != "pending":
                    return result
                if result.deliveries and all(d.status == "failed" for d in result.deliveries):
                    raise HumanDeliveryError(
                        request_id,
                        "; ".join(
                            f"{d.channel}: {d.error or 'delivery failed'}"
                            for d in result.deliveries
                        ),
                    )
                if self.task.done():
                    self.task.result()
                if terminal is not None and terminal.done():
                    terminal.result()
                await asyncio.sleep(0.1)

        async def prompt():
            async with self.terminal.lock:
                current = await self.get(request_id)
                if current.status == "pending":
                    try:
                        body = await self.terminal.answer(current)
                        self.store.answer(request_id, body, "terminal")
                    except EOFError:
                        self.terminal.interactive = False
                    except Conflict:
                        pass

        watcher = asyncio.create_task(watch())
        terminal = None
        terminal_ids = {c.id for c in self.config.channels if c.type == "terminal"}
        if self.terminal.available and any(d.channel in terminal_ids for d in request.deliveries):
            terminal = asyncio.create_task(prompt())
        try:
            # The watcher sees deadlines, cancellation, and replies arriving on other channels.
            return await asyncio.wait_for(watcher, seconds)
        except TimeoutError:
            return await self.get(request_id)
        finally:
            watcher.cancel()
            if terminal:
                terminal.cancel()
                await asyncio.gather(terminal, return_exceptions=True)
            await asyncio.gather(watcher, return_exceptions=True)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.task:
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
        finally:
            try:
                await asyncio.gather(
                    *(a.close() for a in self.adapters.values()), return_exceptions=True
                )
            finally:
                await self.client.aclose()
                self.lock.release()
