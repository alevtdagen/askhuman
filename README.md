# askhuman ↗

**Give your agent a human when it gets stuck.**

```python
from askhuman import ask_human

answer = await ask_human(
    "Which customer definition should this report use?",
    options=["Active subscription", "Any customer with revenue"],
)
print(answer.selected_option)
```

Agents ask through one API. Humans choose where those questions arrive: a web inbox,
Slack, Teams, Discord, Telegram, email, SMS, WhatsApp, or a custom webhook. Humans answer
in a shared response form, and the agent receives a typed, timestamped result.

This repository contains the Python package, self-hosted service and inbox, TypeScript
client, MCP server, portable function tools, and an installable agent skill. It is an
initial release for a single trusted workspace. Registry publication is not part of
this checkout; install from source below.

![The AskHuman inbox with demo requests](docs/images/inbox.jpg)

## Run it in three steps

Python 3.11+ is required. From this repository:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,mcp]'
askhuman init
askhuman serve
```

1. Open <http://127.0.0.1:8765>. In a second terminal, run `askhuman token --admin` and
   paste the operator key into the inbox.
2. Try `askhuman ask 'Which database should I use?' --kind decision --option Postgres --option BigQuery`.
   Answer the question in the inbox. Run `askhuman get REQUEST_ID` to see the result.
3. Open **Channels & routing** to add delivery channels. Assign default channels or
   recipient aliases such as `data-owner`, then save. Agents need no channel code.

The Python SDK automatically uses local `.askhuman/settings.json` when environment
variables are absent. For agents running elsewhere, set only the agent credentials:

```sh
export ASKHUMAN_BASE_URL="https://human.example.com"
export ASKHUMAN_API_KEY="your-agent-key"
```

Get the agent key with `askhuman token`. Keep the operator key and server storage out
of agent containers. Local commands `askhuman inbox` and `askhuman answer` provide a
terminal interface for the human operator.

## Ask, decide, approve

```python
from askhuman import AskHuman

async with AskHuman() as human:
    detail = await human.clarify("Which dataset is authoritative?")
    choice = await human.decide("Which approach?", options=["Fast", "Thorough"])
    approval = await human.approve(
        "Deploy commit abc123 to staging?",
        context="Tests passed. This changes the staging API and is reversible.",
        timeout_seconds=3600,
    )
    if approval.approved is True:
        print("The described deployment was approved.")
    await human.notify("The report is ready for review.")
```

Kinds include `ask`, `decision`, `approval`, `clarification`, `expertise`, `verification`,
`exception`, and `notification`. Approval is a separate, explicit boolean; silence,
delivery success, timeout, and cancellation never imply approval. `notify` queues a
notification and does not wait for a response. Its `notified` status means accepted for
delivery; inspect `deliveries` to check provider acceptance.

An answer includes `request_id`, `answer`, `selected_option`, `approved`, `respondent`,
`timestamp`, and `source`. `respondent` is a self-reported name, not verified identity.

## Survive restarts and long human waits

```python
from askhuman import AskHuman, HumanTimeout

async with AskHuman() as human:
    request = await human.create(
        "May I publish report Q3 revision 4?",
        kind="approval",
        idempotency_key="report-q3-revision4-publish",
        timeout_seconds=86400,
    )
    # Persist request.id in your workflow state before waiting.
    try:
        answer = await human.wait(request.id, wait_timeout=25)
    except HumanTimeout as pending:
        print(pending.request_id, "expired" if pending.expired else "still pending")
        # Later, even after restarting: await human.wait(pending.request_id)
```

Requests, answers, routing, audit events, and the delivery outbox live in SQLite.
Idempotency keys deduplicate retries; reusing a key for different input returns HTTP 409.
The first valid response wins atomically. Late or duplicate responses return 409.
Deadlines are enforced by the server. `human.cancel(id)` makes a pending question invalid.
`wait_timeout` bounds each local waiting session, not the request lifetime; HTTP request
timeouts and retries can add overhead. Cancelling the caller does not cancel the request.

## Add it to your agent

- **Python / custom agents:** call `AskHuman` from any async function.
- **TypeScript / JavaScript:** use the client in [`packages/typescript`](packages/typescript).
- **MCP-compatible agents:** use the stdio server below, including Claude, Codex, and
  frameworks that expose MCP tools.
- **OpenAI, Anthropic, Gemini:** export JSON Schema tools and dispatch tool calls.
- **OpenAI Agents SDK, LangChain, LangGraph:** optional native adapters and workflow examples.
- **Other frameworks, including CrewAI and AutoGen:** wrap an async SDK call as a tool,
  connect MCP where supported, or call the HTTP API directly.
- **Agent skills:** install the bundled `SKILL.md` into your agent's skills directory.

These are integration surfaces, not a claim that every version of every framework has
been tested. See [integration recipes](docs/integrations.md) and executable [examples](examples).

### MCP

Install `.[mcp]` and add this to your MCP client's configuration, using the absolute path
to the installed executable and your agent credentials:

```json
{
  "mcpServers": {
    "askhuman": {
      "command": "/absolute/path/to/askhuman/.venv/bin/askhuman-mcp",
      "env": {
        "ASKHUMAN_BASE_URL": "http://127.0.0.1:8765",
        "ASKHUMAN_API_KEY": "your-agent-key"
      }
    }
  }
}
```

Tools: `ask_human`, `get_human_response`, `wait_for_human`, `cancel_human_request`.
The MCP process is an API client; run the service separately. Waiting is bounded to 25
seconds per tool call to fit host timeouts. Pending requests include IDs for later resume.

### Skill

```sh
askhuman install-skill .agents/skills
```

The destination is a skills parent directory; the command creates its `askhuman` folder
and never overwrites an existing skill. Configure MCP or the CLI credentials separately.
See the [bundled skill](src/askhuman/skill/SKILL.md).

### TypeScript

```sh
cd packages/typescript
npm install
npm run build
# In your agent project: npm install /absolute/path/to/askhuman/packages/typescript
```

```ts
import { AskHuman } from "askhuman-agent";
const human = new AskHuman();
const answer = await human.ask("Which source should I trust?", {
  options: ["Finance", "CRM"],
});
console.log(answer.selected_option);
```

## Channels and routing

The inbox configures built-in adapters for web, Slack incoming webhooks, Teams Adaptive
Card webhooks, Discord webhooks, Telegram bots, SMTP email, Twilio SMS/WhatsApp, and
signed generic webhooks. All outbound notifications use browser response links; replies
typed directly into Slack/email/messaging threads are not ingested.

Choose multiple channels per route to fan out a request. There is one answer across all
channels. Unknown explicit recipients fail instead of falling back to someone else.
Configured settings can reference `env:VARIABLE_NAME` for server-side secrets.
See [channel setup and webhook contracts](docs/channels.md).

External human recipients need a reachable service URL. Initialize with
`askhuman init --base-url https://human.example.com` or set `ASKHUMAN_BASE_URL` for the
server, use `askhuman serve --host 0.0.0.0`, and terminate HTTPS at your reverse proxy.
An existing initialization is preserved; update the environment to change the URL.

## Operating this release

Run one service process against a persistent local `.askhuman` directory. Use
`ASKHUMAN_DATA_DIR` to choose another directory. Back up the settings and database together;
the signing key is necessary for existing reply links. The directory and new credentials
are private to the local OS user. Provider secrets are stored in plaintext in that private
database unless you use environment references. The service does not provide a secret vault.

Agent and operator keys are separate. Reply links are bearer capabilities scoped to one
request; recipients with the link can read it and submit its first answer before expiry.
Tokens are in URL fragments to avoid access logs and referrers. Human names are
self-reported. Use this release with your own trusted humans; SSO, individual identities,
per-agent tenants, RBAC, rate limiting, secret encryption, and formal approval-policy
enforcement are future work. A reverse proxy should enforce request-size and rate limits
when exposed beyond a trusted network. Do not send passwords or credentials as questions.

Delivery retries use a durable outbox with up to five attempts and exponential backoff.
A restart reclaims unfinished deliveries after a 60-second lease. Delivery is at least
once: provider acceptance followed by a process crash can produce a duplicate notification.
Replies remain single-winner. The inbox displays delivery errors without provider secrets.
An agent restarting itself requires its host workflow to resume the saved ID; AskHuman
does not restart arbitrary agent processes.

## Development

```sh
pip install -e '.[server,mcp,dev,openai,langchain]'
pytest
ruff check src tests examples
ruff format --check src tests examples
python -m build
cd packages/typescript && npm install && npm test
```

The tests cover request semantics, permissions, deadline and concurrency behavior,
idempotent retries, delivery contracts, SDK resume, MCP protocol calls, and packaging.
External provider tests use mocked transports; live credentials are intentionally unnecessary.
OpenAPI documentation is served at `/docs`.

## Why this shape

The useful wedge in the ideation is removing channel-specific human-input code from
agent applications. A small library, durable service, and human-owned routing make that
testable today. Distinct questions, decisions, and approvals matter more than many helper
names. The first product should prove that developers keep it in real workflows.

Expertise inference, automatic escalation, learned organizational memory, marketplaces,
voice, and mobile push are later extensions. Automatically reusing an old answer as a new
approval would be unsafe, so this release does not infer or reuse authorization. The
competitor and market-attention claims in the supplied ideation have not been independently
verified here. Validate adoption with completed human interactions, time to answer, and
interventions per successfully completed agent task before expanding into a platform.

Licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
