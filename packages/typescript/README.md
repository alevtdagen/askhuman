# AskHuman for TypeScript

Give any agent a human when it gets stuck. This zero-dependency client connects to the
AskHuman server in the repository root. Requires Node 20+ (or a modern server JS runtime).
Keep the agent API key on your backend.

Build locally with `npm install && npm run build`, then install this directory into your
application. The package has not been published to npm.

```ts
import { AskHuman } from "askhuman-agent";

const human = new AskHuman(); // ASKHUMAN_BASE_URL and ASKHUMAN_API_KEY
const result = await human.decide("Which customer definition should I use?", {
  options: ["Active subscription", "Any customer with revenue"],
  context: "The board report needs a consistent customer count.",
});
console.log(result.selected_option);
```

For durable workflows, persist `await human.create(...)`'s ID, then call `human.wait(id)`
after resuming. `waitTimeout` is the local waiting budget in seconds; expiration is
controlled by `timeout_seconds` when creating a request. A `HumanTimeout` carries
`requestId` and `expired`. Local timeout or abort leaves the request available for resume.
`human.approve()` returns an answer object; check `result.approved === true` explicitly.

`toolDefinitions()` exports provider-neutral JSON Schema tools in OpenAI Responses
format. Dispatch them with `handleTool(human, name, arguments, callId)`; keep the same
call ID on retries. The handler returns a durable request immediately, including pending
status. The `get_human_response` tool retrieves the eventual human answer.

Licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
