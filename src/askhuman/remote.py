"""Optional HTTP transport for a separately running AskHuman service."""

import asyncio
import os

import httpx

from .errors import AskHumanError
from .models import Question, Request
from .settings import Settings


class HTTPBackend:
    def __init__(self, base_url=None, api_key=None, *, transport=None):
        base_url = base_url or os.environ.get("ASKHUMAN_BASE_URL")
        api_key = api_key or os.environ.get("ASKHUMAN_API_KEY")
        if not base_url or not api_key:
            try:
                settings = Settings.load()
            except FileNotFoundError as exc:
                raise AskHumanError(
                    "Remote mode requires ASKHUMAN_BASE_URL and ASKHUMAN_API_KEY, "
                    "or local settings from `askhuman init`"
                ) from exc
            base_url, api_key = base_url or settings.base_url, api_key or settings.api_key
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=35,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def request(self, method: str, path: str, **kwargs) -> Request:
        for attempt in range(3):
            try:
                response = await self.client.request(method, path, **kwargs)
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

    async def create(self, question: Question, key: str) -> Request:
        return await self.request(
            "POST",
            "/v1/requests",
            json=question.model_dump(mode="json"),
            headers={"Idempotency-Key": key},
        )

    async def get(self, request_id: str) -> Request:
        return await self.request("GET", f"/v1/requests/{request_id}")

    async def cancel(self, request_id: str) -> Request:
        return await self.request("POST", f"/v1/requests/{request_id}/cancel")

    async def wait(self, request_id: str, seconds: float) -> Request:
        return await self.request(
            "GET", f"/v1/requests/{request_id}/wait", params={"seconds": int(seconds)}
        )

    async def close(self):
        await self.client.aclose()
