# Integrate once; let the human choose delivery

The shared boundary is a typed question and answer. No framework is required by the
Python library. `AskHuman()` runs locally, with terminal or direct messaging replies.
Explicit remote configuration selects the optional HTTP service. Both modes persist
requests so their IDs can survive an agent worker, MCP call, or model turn.

## Any Python framework

```python
from askhuman import AskHuman

human = AskHuman()

async def ask_for_direction(question: str, context: str = "") -> dict:
    result = await human.ask(question, context=context)
    return result.model_dump(mode="json")

# Register ask_for_direction using your framework's async function-tool API.
# Keep human alive for the agent lifetime and `await human.close()` on shutdown.
```

For durable workers, register a tool around `human.create()` and save the returned ID.
Resume with `human.get()` / `human.wait()`. Embedded receiving stops when its process
stops; keep a runtime alive for live messaging replies, or use the optional service for
independent agents. HTTP routes in remote mode are `POST /v1/requests`, `GET /v1/requests/{id}`,
`GET /v1/requests/{id}/wait?seconds=25`, and `POST /v1/requests/{id}/cancel`.

## OpenAI Responses, Anthropic, and Gemini

```python
from askhuman.tools import tool_definitions, handle_tool

openai_tools = tool_definitions("openai")
anthropic_tools = tool_definitions("anthropic")
gemini_declarations = tool_definitions("gemini")

# Inside your existing model's tool-call loop:
result = await handle_tool(human, call_name, arguments, call_id=tool_call_id)
```

`openai` produces Responses API `type: function` definitions with strict JSON schemas.
For Chat Completions, wrap each definition's name, description, parameters, and strict
fields under `function`. `anthropic` uses `input_schema`. `gemini` uses
`parameters_json_schema` declarations, placed inside `function_declarations` in your
Gemini SDK tool configuration. Pass the returned result through your provider's normal
tool-result mechanism. Do not describe a pending request as an answer.

The Python dispatcher waits up to 25 seconds before returning the request. Set
`wait_seconds=0` for immediate return. Provider call IDs serve as default idempotency keys;
your workflow should supply a stable key if the provider changes call IDs on retries.

OpenAI function shapes follow the [official function-calling documentation](https://developers.openai.com/api/docs/guides/function-calling).
Model calls require your own API credentials; local tests do not call model providers.

## OpenAI Agents SDK

Install `.[openai]` and use the complete native tool set:

```python
from agents import Agent
from askhuman import AskHuman
from askhuman.tools import as_openai_tools

human = AskHuman()
agent = Agent(
    name="Research assistant",
    instructions="Ask a human when a decision is needed. Preserve pending request IDs.",
    tools=as_openai_tools(human),
)
# Use your existing Runner and model configuration; close human after the run.
```

The tools include `ask_human`, `get_human_response`, and `cancel_human_request`.
They are separate from the framework's own approval mechanisms. Your application must
enforce the returned `approved` boolean before performing an action.

## LangChain and LangGraph

Install `.[langchain]`. `as_langchain_tools(human)` returns async `StructuredTool`
instances for an agent or LangGraph `ToolNode`:

```python
from askhuman.tools import as_langchain_tools

tools = as_langchain_tools(human)
# Example in an existing LangGraph project:
# from langgraph.prebuilt import ToolNode
# tool_node = ToolNode(tools)
```

For graph checkpoints that should release the worker while awaiting a person, split the
flow into a node that creates a request and returns `request_id`, then a node that calls
`interrupt({"askhuman_request_id": state["request_id"]})`. Your application waits on
`human.wait(id)` outside the graph and resumes with `Command(resume=answer.model_dump())`.
Use the graph's persistent checkpointer and an idempotency key tied to its task/action ID;
interrupting nodes may execute again. Do not create a fresh question every time they run.

## CrewAI, AutoGen, PydanticAI, and other frameworks

Use their MCP bridge where supported, or register an async Python function as a tool.
The TypeScript client uses the optional HTTP service. No monkey-patching is required. MCP and HTTP
surfaces provide compatibility without importing every agent framework into this package.
Native adapters are implemented and tested only for OpenAI Agents and LangChain;
other framework names describe supported integration approaches, not tested version ranges.

## MCP and skills

Run `askhuman-mcp` in a client that supports stdio MCP servers. By default it owns an
embedded runtime. Set an absolute `ASKHUMAN_DATA_DIR`, configure its messaging channels,
and optionally set `ASKHUMAN_MODE=embedded` to make mode selection explicit. It never
reads stdin for human answers: stdin belongs to MCP. With the default terminal channel,
the human answers from another terminal using `askhuman --mode embedded inbox` and
`askhuman --mode embedded answer REQUEST_ID ...` against that same directory.

For remote mode, supply `ASKHUMAN_BASE_URL` and `ASKHUMAN_API_KEY` in the host's environment
and run the REST service separately. Never provide the operator key to an agent. MCP
discovery returns JSON schemas for all four tools in either mode.

Install the skill with `askhuman install-skill PATH_TO_SKILLS_PARENT`. Common paths
include a repository's `.agents/skills` or a host-specific user skills directory. The
installer changes only the requested directory and refuses to overwrite an existing skill.

## Retry, result, and approval rules

Persist `request.id`; use a stable `idempotency_key` per question/action. Requests have one
terminal result: `answered`, `expired`, `cancelled`, or notification-only `notified`.
`answered` carries `response`. Inspect `selected_option` for choices and
`response.approved is True` for approvals. Never use truthiness of the answer object as
authorization. Include exact targets and action details in the question context.

The same framework tools work in either mode; they receive the chosen `AskHuman` instance.
Embedded mode shares the application's trust boundary and channel credentials. Use the
remote service for a separate operator/agent credential boundary. See [embedded setup](embedded.md).

This package handles human interaction; your workflow schedules and resumes the agent.
It does not automatically detect uncertainty, restart a model session, or execute the
action a person approved.
