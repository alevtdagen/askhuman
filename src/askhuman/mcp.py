"""MCP stdio server. Output is reserved for the protocol; human delivery is out of process."""

from contextlib import asynccontextmanager
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP

from .client import AskHuman, HumanCancelled, HumanTimeout
from .models import Kind, Request
from .tools import DESCRIPTION, ToolInput, submit_tool


def create_mcp():
    @asynccontextmanager
    async def lifespan(server):
        async with AskHuman() as client:
            yield client

    server = FastMCP(
        "AskHuman",
        lifespan=lifespan,
        instructions=(
            "Use AskHuman when human judgment or authorization is needed. "
            "Pending requests are durable. Save their ID; poll with get_human_response or "
            "wait_for_human. A timeout is never an answer or an approval. "
            "Do not submit a duplicate question to poll an existing request."
        ),
    )

    @server.tool(description=DESCRIPTION)
    async def ask_human(
        question: str,
        ctx: Context,
        kind: Kind = "ask",
        context: str = "",
        options: list[str] | None = None,
        recipient: str | None = None,
        urgency: Literal["low", "normal", "high"] = "normal",
        timeout_seconds: int = 86400,
        idempotency_key: str | None = None,
    ) -> Request:
        result = await submit_tool(
            ctx.request_context.lifespan_context,
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
        return Request.model_validate(result)

    @server.tool()
    async def get_human_response(request_id: str, ctx: Context) -> Request:
        """Read a saved human request. Pending means no human has answered yet."""
        return await ctx.request_context.lifespan_context.get(request_id)

    @server.tool()
    async def wait_for_human(request_id: str, ctx: Context, seconds: int = 25) -> Request:
        """Wait up to 25 seconds for an existing request, then return its current status."""
        if not 0 <= seconds <= 25:
            raise ValueError("seconds must be between 0 and 25")
        client = ctx.request_context.lifespan_context
        try:
            await client.wait(request_id, wait_timeout=seconds)
        except (HumanTimeout, HumanCancelled):
            pass
        return await client.get(request_id)

    @server.tool()
    async def cancel_human_request(request_id: str, ctx: Context) -> Request:
        """Cancel a pending question that is no longer relevant."""
        return await ctx.request_context.lifespan_context.cancel(request_id)

    return server


def main():
    create_mcp().run(transport="stdio")


if __name__ == "__main__":
    main()
