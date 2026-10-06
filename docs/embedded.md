# Embedded human input

`AskHuman()` runs the request engine in your Python process. It stores state in SQLite and
starts only outbound connections for configured messaging channels. No HTTP server, public
URL, API key, Docker container, or hosted AskHuman account is required.

## Terminal

No configuration is necessary. `await human.ask(...)` presents the question in an interactive
terminal; `await human.decide(...)` numbers the options. Choose an exact option label or
number. Approvals require `Approve`, `Reject`, or the displayed number. Terminal answers
are one line; the CLI's `--text` option can supply additional context for an explicit choice.

Prompts go to stderr; CLI results go to stdout as JSON. Interactive input is enabled only
when stdin is a terminal. Timeouts and task cancellation stop listening for input, rather
than leaving a blocked executor thread behind. POSIX uses event-loop file-descriptor
readiness; Windows consoles use cancellable keyboard polling.

Noninteractive applications, including MCP, can wait for the human-side CLI instead:

```sh
askhuman inbox
askhuman answer REQUEST_ID --text 'Use finance.revenue_v3' --name 'Sam'
askhuman answer REQUEST_ID --option 'Finance' --name 'Sam'
askhuman answer REQUEST_ID --reject --text 'Tests are still running' --name 'Sam'
```

Point both processes at the same `ASKHUMAN_DATA_DIR`. `inbox`, `get`, and the human `answer`
command can read/update the database while the embedded receiver runtime owns its lock.
`askhuman ask --wait 0` creates a saved request without waiting. The default `askhuman ask`
waits for a response until the question expires.

## Telegram: direct replies through polling

Run `askhuman configure --channel telegram`. Use a **dedicated bot**, start a conversation
with it, and supply the destination chat ID and allowed human user IDs. No additional Python
extra is needed. A representative `.askhuman/config.json` is:

```json
{
  "channels": [{
    "id": "my-telegram",
    "type": "telegram_polling",
    "enabled": true,
    "settings": {
      "bot_token": "env:TELEGRAM_BOT_TOKEN",
      "chat_id": "123456789",
      "allowed_user_ids": "123456789"
    }
  }],
  "default_channels": ["my-telegram"],
  "routes": {"data-owner": ["my-telegram"]}
}
```

Use Telegram's **Reply** action on the question message. Send free text for an open question,
or an option number/exact label for a decision. For an approval, reply `Approve` or `Reject`.
An optional explanation can follow on subsequent lines in the same message. Telegram's
Force Reply markup opens the appropriate reply UI when supported.

The receiver verifies the configured chat, the allowed sender ID, the parent message ID,
and that the sender is a human. Forwarded messages and edits are ignored. Reply mappings
and the polling offset are saved in SQLite. Polling resumes after a process restart without
resending questions already recorded as delivered. Telegram retains updates for at most
24 hours, so an answer can be lost if no consumer runs within that period.

A bot with an active webhook cannot use polling. AskHuman reports the setup problem; it
never deletes your webhook automatically. Do not run another `getUpdates` consumer for the
same bot, even from a different data directory. Questions exceeding Telegram's full message
limit fail delivery rather than silently truncating the action or its options.

[Telegram update and polling reference](https://core.telegram.org/bots/api#getting-updates)

## Slack: direct thread replies through Socket Mode

Install the extra and configure:

```sh
pip install -e '.[slack]'
askhuman configure --channel slack
```

Create a Slack app, enable **Socket Mode**, and create an app-level token with
`connections:write`. Give the bot `chat:write` and the history/event permissions appropriate
for the conversation: for example `channels:history` with `message.channels` for public
channels, or `im:history` with `message.im` for direct messages. Private channels use
`groups:history` / `message.groups`. Install the app and invite it into the channel.

```json
{
  "channels": [{
    "id": "engineering",
    "type": "slack_socket",
    "settings": {
      "bot_token": "env:SLACK_BOT_TOKEN",
      "app_token": "env:SLACK_APP_TOKEN",
      "channel_id": "C0123456789",
      "allowed_user_ids": "U0123456789,U9876543210"
    }
  }],
  "default_channels": ["engineering"]
}
```

Reply **in the question's thread**. The parser uses the same choice/approval rules as
Telegram. Only messages from allowed user IDs in the configured channel and matching thread
are eligible. Bot messages, edits, and unrelated threads cannot answer a request.

The official Slack SDK maintains the outbound WebSocket connection. No public callback URL
is required. Socket Mode needs the process running to receive events; this release does not
backfill missed replies from Slack history. After a long outage, resume the request and ask
the human to reply again in the original thread. Already recorded answers survive normally.

[Slack Python Socket Mode reference](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/)

## Routing, configuration, and lifecycle

Each recipient alias maps to one or more enabled channel IDs. Omit `recipient` to use
`default_channels`. An unknown explicit alias fails; it does not fall back to another person.
To configure several channels, edit the JSON file or import a full configuration:

```sh
askhuman configure --from-file ./human-channels.json
```

Configuration is loaded when a client is constructed and applied after it acquires the
workspace's receiver lock. Restart the client to apply changes. A second runtime cannot
change the first runtime's active routes. Existing pending deliveries use the newly loaded
channel settings after restart, so changing destinations can affect queued notifications.
Changing a bot token or chat changes its receiver identity: keep the old configuration when
resuming outstanding requests, or cancel and recreate them explicitly.

The same Python process has access to the state and credentials. Embedded mode is a trusted
application component, not a security boundary against malicious agent code. Optional server
mode separates the agent key from operator/channel credentials. Never put secrets into a
question or grant authorization based only on a truthy answer object.

Keep a single `AskHuman` instance alive for the task's lifetime. Use `async with` or call
`await human.close()`. A lock prevents competing runtimes in one directory; it is released
on close or process exit. Different processes can use different directories, but each needs
its own dedicated bot/receiver. If multiple independent agents need one human inbox, use
[the optional service](channels.md).

Each direct channel needs its own bot/app; configuring two receivers with the same bot/app
token is rejected to avoid competing consumers. Several recipient routes can reuse one
channel. When an option label itself is a number, a valid displayed option number takes
precedence; select that numbered entry.

Request state and pending delivery survive restarts. Delivery and receiving stop while the
process is down. In-flight sends can be duplicated after a crash; first valid answers still
win atomically. `HumanDeliveryError` identifies requests whose outbound deliveries all failed.
A transient receiver outage is visible in `request.deliveries[*].error` where reported and
can leave an already-delivered request pending. Deadlines continue to apply.
