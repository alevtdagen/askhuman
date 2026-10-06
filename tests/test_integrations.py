import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest
from jsonschema import validate
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from askhuman import AskHuman
from askhuman.tools import (
    ToolInput,
    as_langchain_tools,
    as_openai_tools,
    handle_tool,
    tool_definitions,
)


def test_portable_schema_formats():
    payload = ToolInput(question="Which source?").model_dump()
    for provider in ("openai", "anthropic", "gemini"):
        definitions = tool_definitions(provider)
        assert len(definitions) == 3
        key = {
            "openai": "parameters",
            "anthropic": "input_schema",
            "gemini": "parameters_json_schema",
        }[provider]
        validate(payload, definitions[0][key])
    strict = tool_definitions()[0]
    assert strict["strict"] is True
    assert set(strict["parameters"]["required"]) == set(strict["parameters"]["properties"])


async def test_dispatcher_reuses_tool_call_id(human, app):
    args = {"question": "Which source?"}
    first = await handle_tool(human, "ask_human", args, call_id="call-42", wait_seconds=0)
    second = await handle_tool(human, "ask_human", args, call_id="call-42", wait_seconds=0)
    assert first["id"] == second["id"] and first["status"] == "pending"
    assert len(app.state.store.list()) == 1
    saved = await handle_tool(human, "get_human_response", {"request_id": first["id"]})
    assert saved["id"] == first["id"]
    cancelled = await handle_tool(human, "cancel_human_request", {"request_id": first["id"]})
    assert cancelled["status"] == "cancelled"


async def test_native_openai_tools_without_a_model_call(human):
    pytest.importorskip("agents")
    from agents.tool_context import ToolContext

    tools = as_openai_tools(human)
    assert [t.name for t in tools] == ["ask_human", "get_human_response", "cancel_human_request"]
    context = ToolContext(
        context=None, tool_name="ask_human", tool_call_id="call-1", tool_arguments="{}"
    )
    result = json.loads(
        await tools[0].on_invoke_tool(
            context,
            json.dumps(
                {
                    "question": "Report complete",
                    "kind": "notification",
                    "context": "",
                    "options": None,
                    "recipient": None,
                    "urgency": "high",
                    "timeout_seconds": 3600,
                    "idempotency_key": "native-openai-1",
                }
            ),
        )
    )
    assert result["status"] == "notified"
    assert result["urgency"] == "high" and result["timeout_seconds"] == 3600
    read = json.loads(
        await tools[1].on_invoke_tool(context, json.dumps({"request_id": result["id"]}))
    )
    assert read["id"] == result["id"]


async def test_native_langchain_tools_without_a_model_call(human):
    pytest.importorskip("langchain_core")
    tools = as_langchain_tools(human)
    result = await tools[0].ainvoke({"question": "Report complete", "kind": "notification"})
    assert result["status"] == "notified"
    read = await tools[1].ainvoke({"request_id": result["id"]})
    assert read["id"] == result["id"]


async def test_mcp_stdio_roundtrip_with_human_response(live_server):
    settings, env = live_server
    params = StdioServerParameters(command=sys.executable, args=["-m", "askhuman.mcp"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listing = await session.list_tools()
        assert {tool.name for tool in listing.tools} == {
            "ask_human",
            "get_human_response",
            "wait_for_human",
            "cancel_human_request",
        }
        call = asyncio.create_task(
            session.call_tool(
                "ask_human",
                {
                    "question": "Which data source?",
                    "kind": "decision",
                    "options": ["Finance", "CRM"],
                    "idempotency_key": "mcp-roundtrip",
                },
            )
        )
        async with httpx.AsyncClient(
            base_url=settings.base_url,
            trust_env=False,
            headers={"Authorization": f"Bearer {settings.admin_key}"},
        ) as admin:
            for _ in range(100):
                requests = (await admin.get("/api/admin/requests")).json()
                if requests:
                    break
                await asyncio.sleep(0.05)
            assert requests
            result = await admin.post(
                f"/api/admin/requests/{requests[0]['id']}/answer",
                json={
                    "selected_option": "Finance",
                    "respondent": "MCP human",
                },
            )
            assert result.status_code == 200
        answered = await asyncio.wait_for(call, timeout=10)
        assert not answered.isError
        assert answered.structuredContent["status"] == "answered"
        assert answered.structuredContent["response"]["selected_option"] == "Finance"
        request_id = answered.structuredContent["id"]
        resumed = await session.call_tool(
            "wait_for_human", {"request_id": request_id, "seconds": 0}
        )
        assert resumed.structuredContent["response"]["respondent"] == "MCP human"


async def test_http_sdk_reconnects_with_persisted_id(live_server):
    settings, _ = live_server
    async with AskHuman(settings.base_url, settings.api_key) as client:
        request = await client.create("Proceed?", kind="approval")
    async with httpx.AsyncClient(trust_env=False) as admin:
        result = await admin.post(
            f"{settings.base_url}/api/admin/requests/{request.id}/answer",
            headers={"Authorization": f"Bearer {settings.admin_key}"},
            json={"approved": False, "respondent": "Sam"},
        )
        assert result.status_code == 200
    async with AskHuman(settings.base_url, settings.api_key) as restarted:
        assert (await restarted.wait(request.id)).approved is False


def test_cli_initialization_and_skill_install_are_non_destructive(tmp_path):
    directory = tmp_path / "state"
    env = dict(os.environ, ASKHUMAN_DATA_DIR=str(directory))

    def command(*args):
        return subprocess.run(
            [sys.executable, "-m", "askhuman.cli", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert command("init").returncode == 0
    original = (directory / "settings.json").read_bytes()
    assert command("init").returncode == 1
    assert (directory / "settings.json").read_bytes() == original
    assert command("install-skill", str(tmp_path / "skills")).returncode == 0
    skill = tmp_path / "skills" / "askhuman" / "SKILL.md"
    assert skill.exists() and "name: askhuman" in skill.read_text()
    skill.write_text("local customization")
    assert command("install-skill", str(tmp_path / "skills")).returncode == 1
    assert skill.read_text() == "local customization"
