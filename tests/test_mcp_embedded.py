import asyncio
import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from askhuman.config import RoutingConfig
from askhuman.store import Store


def embedded_env(directory):
    env = {key: value for key, value in os.environ.items() if not key.startswith("ASKHUMAN_")}
    return env | {"ASKHUMAN_MODE": "embedded", "ASKHUMAN_DATA_DIR": str(directory)}


async def test_mcp_uses_local_engine_and_never_consumes_protocol_stdin(tmp_path):
    env = embedded_env(tmp_path)
    params = StdioServerParameters(command=sys.executable, args=["-m", "askhuman.mcp"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert len((await session.list_tools()).tools) == 4
        call = asyncio.create_task(
            session.call_tool(
                "ask_human",
                {
                    "question": "May I publish the test report?",
                    "kind": "approval",
                    "idempotency_key": "mcp-embedded-test",
                },
            )
        )
        store = Store(tmp_path / "embedded.sqlite3", default_config=RoutingConfig.embedded())
        for _ in range(100):
            requests = store.list()
            if requests:
                break
            await asyncio.sleep(0.02)
        assert requests
        request_id = requests[0].id
        # A distinct human-side process can answer without any HTTP service or API key.
        operator = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "askhuman.cli",
            "answer",
            request_id,
            "--reject",
            "--name",
            "Test human",
            env=env,
            stdout=asyncio.subprocess.PIPE,
        )
        await operator.communicate()
        assert operator.returncode == 0
        answered = await asyncio.wait_for(call, 3)
        assert not answered.isError
        assert answered.structuredContent["response"]["approved"] is False
        assert answered.structuredContent["response"]["source"] == "terminal"
    # A new MCP process reads the result from disk, with no prior process or server remaining.
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        resumed = await session.call_tool("get_human_response", {"request_id": request_id})
        assert resumed.structuredContent["status"] == "answered"
    assert not (tmp_path / "settings.json").exists()


def test_python_core_does_not_load_server_mcp_or_provider_sdks(tmp_path):
    script = """
import asyncio, socket, sys
from askhuman import AskHuman
def forbidden(*args, **kwargs):
    raise AssertionError('Embedded terminal mode must not open a network connection or listener')
socket.socket.connect = forbidden
socket.socket.bind = forbidden
async def main():
    async with AskHuman(interactive=False) as human:
        request = await human.create('Local request')
        assert (await human.get(request.id)).status == 'pending'
asyncio.run(main())
assert not {'fastapi', 'uvicorn', 'mcp', 'slack_sdk', 'aiohttp'} & set(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=embedded_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
