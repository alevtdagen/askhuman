"""Portable function tools for OpenAI, Anthropic, Gemini, and custom runtimes."""

import json
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .client import AskHuman, HumanCancelled, HumanTimeout
from .models import Kind

DESCRIPTION = (
    "Ask a human for missing information, a decision, or explicit approval. "
    "The human controls delivery channels. Returns a saved request and its status. "
    "If pending, retain its ID and call get_human_response later. "
    "Only response.approved=true grants approval for the exact action described."
)


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(description="One clear question or the exact action requiring approval")
    kind: Kind = "ask"
    context: str = ""
    options: list[str] = Field(default_factory=list)
    recipient: str | None = None
    urgency: Literal["low", "normal", "high"] = "normal"
    timeout_seconds: int = 86400
    idempotency_key: str | None = None


async def submit_tool(client: AskHuman, args: ToolInput, wait_seconds: int = 25) -> dict:
    request = await client.create(**args.model_dump())
    if request.status == "pending":
        try:
            await client.wait(request.id, wait_timeout=wait_seconds)
        except (HumanTimeout, HumanCancelled):
            pass
        request = await client.get(request.id)
    return request.model_dump(mode="json")


def tool_definitions(format: Literal["openai", "anthropic", "gemini"] = "openai") -> list[dict]:
    """OpenAI Responses API format by default; no model/provider SDK required."""
    schema = ToolInput.model_json_schema()
    # OpenAI strict schemas require all properties; null expresses an optional recipient/key.
    schema["required"] = list(schema["properties"])
    schema["additionalProperties"] = False
    for prop in schema["properties"].values():
        prop.pop("default", None)
        prop.pop("title", None)
    schema.pop("title", None)
    ids = {
        "type": "object",
        "properties": {"request_id": {"type": "string"}},
        "required": ["request_id"],
        "additionalProperties": False,
    }
    definitions = [
        {"name": "ask_human", "description": DESCRIPTION, "parameters": schema},
        {
            "name": "get_human_response",
            "description": "Read a saved human request by ID.",
            "parameters": ids,
        },
        {
            "name": "cancel_human_request",
            "description": "Cancel a pending human request.",
            "parameters": ids,
        },
    ]
    if format == "openai":
        return [deepcopy(d) | {"type": "function", "strict": True} for d in definitions]
    if format == "anthropic":
        return [
            {
                "name": d["name"],
                "description": d["description"],
                "input_schema": deepcopy(d["parameters"]),
            }
            for d in definitions
        ]
    if format == "gemini":
        return [
            {
                "name": d["name"],
                "description": d["description"],
                "parameters_json_schema": deepcopy(d["parameters"]),
            }
            for d in definitions
        ]
    raise ValueError(f"Unsupported tool format: {format}")


async def handle_tool(
    client: AskHuman,
    name: str,
    arguments: dict | str,
    *,
    call_id: str | None = None,
    wait_seconds: int = 25,
) -> dict:
    """Use a stable provider tool-call ID to deduplicate retried tool execution."""
    args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
    if name == "ask_human":
        if call_id and not args.get("idempotency_key"):
            args["idempotency_key"] = call_id
        return await submit_tool(client, ToolInput.model_validate(args), wait_seconds)
    if name == "get_human_response":
        return (await client.get(args["request_id"])).model_dump(mode="json")
    if name == "cancel_human_request":
        return (await client.cancel(args["request_id"])).model_dump(mode="json")
    raise ValueError(f"Unknown AskHuman tool: {name}")


def as_openai_tool(client: AskHuman):
    """Optional native tool for the OpenAI Agents SDK."""
    from agents import function_tool

    @function_tool(name_override="ask_human", description_override=DESCRIPTION)
    async def tool(
        question: str,
        kind: Kind = "ask",
        context: str = "",
        options: list[str] | None = None,
        recipient: str | None = None,
        urgency: Literal["low", "normal", "high"] = "normal",
        timeout_seconds: int = 86400,
        idempotency_key: str | None = None,
    ) -> str:
        result = await submit_tool(
            client,
            ToolInput(
                question=question,
                kind=kind,
                context=context,
                options=options or [],
                recipient=recipient,
                urgency=urgency,
                timeout_seconds=timeout_seconds,
                idempotency_key=idempotency_key,
            ),
        )
        return json.dumps(result)

    return tool


def as_langchain_tool(client: AskHuman):
    """Optional async StructuredTool, usable in LangChain and LangGraph."""
    from langchain_core.tools import StructuredTool

    async def tool(**kwargs) -> dict:
        return await submit_tool(client, ToolInput.model_validate(kwargs))

    return StructuredTool.from_function(
        coroutine=tool,
        name="ask_human",
        description=DESCRIPTION,
        args_schema=ToolInput,
    )


def as_openai_tools(client: AskHuman):
    """A complete native tool set, including resume and cancellation."""
    from agents import function_tool

    @function_tool
    async def get_human_response(request_id: str) -> str:
        """Read the current state and answer of a saved human request."""
        return (await client.get(request_id)).model_dump_json()

    @function_tool
    async def cancel_human_request(request_id: str) -> str:
        """Cancel a human question that is no longer relevant."""
        return (await client.cancel(request_id)).model_dump_json()

    return [as_openai_tool(client), get_human_response, cancel_human_request]


def as_langchain_tools(client: AskHuman):
    """Async tools for LangChain/LangGraph, including resume and cancellation."""
    from langchain_core.tools import StructuredTool

    async def get_human_response(request_id: str) -> dict:
        """Read the current state and answer of a saved human request."""
        return (await client.get(request_id)).model_dump(mode="json")

    async def cancel_human_request(request_id: str) -> dict:
        """Cancel a human question that is no longer relevant."""
        return (await client.cancel(request_id)).model_dump(mode="json")

    return [
        as_langchain_tool(client),
        *[
            StructuredTool.from_function(
                coroutine=func, name=func.__name__, description=func.__doc__
            )
            for func in (get_human_response, cancel_human_request)
        ],
    ]
