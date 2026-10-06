"""Small async SDK with no dependency on a particular agent framework."""

import asyncio
import math
import os
import uuid

import httpx

from .models import Answer, Question, Request
from .settings import Settings


class AskHumanError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HumanTimeout(AskHumanError):
    def __init__(self, request_id: str, *, expired: bool = False):
        self.request_id = request_id
        self.expired = expired
        super().__init__(f"Request {request_id} {'expired' if expired else 'is still pending'}")


class HumanCancelled(AskHumanError):
    def __init__(self, request_id: str):
        self.request_id = request_id
        super().__init__(f"Request {request_id} was cancelled")


class AskHuman:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        base_url = base_url or os.environ.get("ASKHUMAN_BASE_URL")
        api_key = api_key or os.environ.get("ASKHUMAN_API_KEY")
        if not base_url or not api_key:
            try:
                settings = Settings.load()
            except FileNotFoundError as exc:
                raise AskHumanError(
                    "Set ASKHUMAN_BASE_URL and ASKHUMAN_API_KEY, or run `askhuman init` locally"
                ) from exc
            base_url, api_key = base_url or settings.base_url, api_key or settings.api_key
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=35,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Request:
        # POST creation is retry-safe because create() always supplies an idempotency key.
        for attempt in range(3):
            try:
                response = await self._client.request(method, path, **kwargs)
                if response.status_code >= 500 and attempt < 2:
                    await asyncio.sleep(0.2 * 2**attempt)
                    continue
                if response.is_error:
                    raise AskHumanError(response.text[:1500], status_code=response.status_code)
                return Request.model_validate(response.json())
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise AskHumanError(f"AskHuman is unreachable ({type(exc).__name__})") from exc
                await asyncio.sleep(0.2 * 2**attempt)
        raise AssertionError("Unreachable")

    async def create(
        self, question: str, *, idempotency_key: str | None = None, **kwargs
    ) -> Request:
        body = Question(question=question, **kwargs)
        return await self._request(
            "POST",
            "/v1/requests",
            json=body.model_dump(mode="json"),
            headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())},
        )

    async def get(self, request_id: str) -> Request:
        return await self._request("GET", f"/v1/requests/{request_id}")

    async def cancel(self, request_id: str) -> Request:
        return await self._request("POST", f"/v1/requests/{request_id}/cancel")

    async def wait(self, request_id: str, *, wait_timeout: float | None = None) -> Answer:
        """A local timeout leaves the request pending; save its ID and resume with wait()."""
        if wait_timeout is not None and (not math.isfinite(wait_timeout) or wait_timeout < 0):
            raise ValueError("wait_timeout must be a finite nonnegative number or None")
        loop = asyncio.get_running_loop()
        end = loop.time() + wait_timeout if wait_timeout is not None else math.inf
        while True:
            seconds = max(0, min(25, int(end - loop.time()))) if end != math.inf else 25
            request = await self._request(
                "GET", f"/v1/requests/{request_id}/wait", params={"seconds": seconds}
            )
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
            if seconds == 0:
                await asyncio.sleep(min(0.1, max(0, end - loop.time())))

    async def ask(self, question: str, *, wait_timeout: float | None = None, **kwargs) -> Answer:
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
