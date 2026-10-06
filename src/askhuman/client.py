"""Framework-neutral Python API. Embedded by default, HTTP when explicitly configured."""

import asyncio
import math
import os
import uuid
from pathlib import Path
from typing import Literal

import httpx

from .errors import AskHumanError, HumanCancelled, HumanTimeout
from .models import Answer, Question, Request


def selected_mode(mode=None, base_url=None, api_key=None):
    mode = mode or os.environ.get("ASKHUMAN_MODE")
    if mode is None:
        mode = (
            "remote"
            if (
                base_url
                or api_key
                or os.environ.get("ASKHUMAN_BASE_URL")
                or os.environ.get("ASKHUMAN_API_KEY")
            )
            else "embedded"
        )
    if mode not in ("embedded", "remote"):
        raise ValueError("mode must be embedded or remote")
    return mode


def validate_budget(value):
    if value is not None and (not math.isfinite(value) or value < 0):
        raise ValueError("wait_timeout must be a finite nonnegative number or None")


class AskHuman:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        mode: Literal["embedded", "remote"] | None = None,
        data_dir: str | Path | None = None,
        config: str | Path | None = None,
        interactive: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.mode = selected_mode(mode, base_url, api_key)
        if self.mode == "embedded":
            if base_url or api_key:
                raise ValueError("Embedded mode does not use a base URL or API key")
            from .embedded import EmbeddedEngine

            self._backend = EmbeddedEngine(
                data_dir=data_dir, config=config, interactive=interactive, transport=transport
            )
        else:
            if data_dir is not None or config is not None:
                raise ValueError("data_dir and config configure embedded mode; use mode='embedded'")
            from .remote import HTTPBackend

            self._backend = HTTPBackend(base_url, api_key, transport=transport)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self._backend.close()

    async def create(
        self, question: str, *, idempotency_key: str | None = None, **kwargs
    ) -> Request:
        body = Question(question=question, **kwargs)
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
            raise ValueError("idempotency_key must contain 1–200 characters")
        return await self._backend.create(body, idempotency_key or str(uuid.uuid4()))

    async def get(self, request_id: str) -> Request:
        return await self._backend.get(request_id)

    async def cancel(self, request_id: str) -> Request:
        return await self._backend.cancel(request_id)

    async def wait(self, request_id: str, *, wait_timeout: float | None = None) -> Answer:
        """Local timeout/cancellation preserves the request; save its ID to resume later."""
        validate_budget(wait_timeout)
        loop = asyncio.get_running_loop()
        end = loop.time() + wait_timeout if wait_timeout is not None else math.inf
        while True:
            seconds = max(0, end - loop.time())
            if self.mode == "remote":
                seconds = min(25, seconds)
            if self.mode == "embedded" and seconds > 0:
                budget = None if math.isinf(seconds) else seconds
                try:
                    async with asyncio.timeout(budget):
                        request = await self._backend.wait(request_id, budget)
                except TimeoutError:
                    request = await self._backend.get(request_id)
            else:
                request = await self._backend.wait(request_id, seconds)
            if request.status == "answered":
                assert request.response is not None
                return request.response
            if request.status == "expired":
                raise HumanTimeout(request_id, expired=True)
            if request.status == "cancelled":
                raise HumanCancelled(request_id)
            if request.status == "notified":
                raise AskHumanError("Notifications do not have an answer")
            if loop.time() >= end:
                raise HumanTimeout(request_id)
            if seconds < 1:
                await asyncio.sleep(min(0.1, max(0, end - loop.time())))

    async def ask(self, question: str, *, wait_timeout: float | None = None, **kwargs) -> Answer:
        validate_budget(wait_timeout)
        request = await self.create(question, **kwargs)
        return await self.wait(request.id, wait_timeout=wait_timeout)

    async def decide(self, question: str, *, options: list[str], **kwargs) -> Answer:
        return await self.ask(question, kind="decision", options=options, **kwargs)

    async def approve(self, question: str, **kwargs) -> Answer:
        return await self.ask(question, kind="approval", **kwargs)

    async def clarify(self, question: str, **kwargs) -> Answer:
        return await self.ask(question, kind="clarification", **kwargs)

    async def notify(self, question: str, **kwargs) -> Request:
        return await self.create(question, kind="notification", **kwargs)


async def ask_human(question: str, **kwargs) -> Answer:
    async with AskHuman() as human:
        return await human.ask(question, **kwargs)
