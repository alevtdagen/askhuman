import httpx
import pytest

from askhuman import AskHuman, AskHumanError


async def test_transport_retry_keeps_idempotency_key(app, settings):
    calls = []
    transport = httpx.ASGITransport(app=app)

    class LostResponse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await transport.handle_async_request(request)
            calls.append(request.headers["Idempotency-Key"])
            if len(calls) == 1:
                await response.aclose()
                raise httpx.ReadError("Response lost after server committed the request")
            return response

    async with AskHuman(settings.base_url, settings.api_key, transport=LostResponse()) as human:
        result = await human.create("Which source?")
    assert result.status == "pending" and calls[0] == calls[1]
    assert len(app.state.store.list()) == 1


async def test_authentication_error_is_not_retried(settings):
    count = 0

    def reject(request):
        nonlocal count
        count += 1
        return httpx.Response(401, json={"detail": "Invalid credential"})

    async with AskHuman(
        settings.base_url, "invalid", transport=httpx.MockTransport(reject)
    ) as human:
        with pytest.raises(AskHumanError) as error:
            await human.create("Test")
    assert error.value.status_code == 401 and count == 1
