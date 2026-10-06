# Human-owned channel setup

Log into the web inbox with the operator key and open **Channels & routing**. Add a
channel ID, enter its connection settings, enable it, assign it to a default or recipient
route, and save. All requests remain visible in the operator inbox even when a different
delivery channel is selected. Adding a channel alone does not route requests to it.

Every outbound adapter delivers the question and a scoped response link. The human
answers on the AskHuman page. This avoids separate response parsing, approval semantics,
and callback deployments for each channel. Direct thread replies are not supported.

Use a reachable HTTPS `ASKHUMAN_BASE_URL` before sending links to other people. A local
127.0.0.1 address works only on the service computer. The server does not create a public
tunnel or deploy itself automatically.

## Web inbox and terminal

The default `inbox` channel requires no provider account. The human operator can also use:

```sh
askhuman inbox
askhuman answer REQUEST_ID --text 'Use the finance definition' --name 'Sam'
askhuman answer REQUEST_ID --option 'Finance' --name 'Sam'
askhuman answer REQUEST_ID --approve --name 'Sam'
askhuman answer REQUEST_ID --reject --text 'Wait for the test results' --name 'Sam'
```

These commands load local operator credentials; do not expose the storage directory to
agent containers. Remote humans should use the inbox or the scoped notification link.

## Slack

Create a Slack app with an incoming webhook for the intended conversation. Configure
`url` as the webhook URL or `env:SLACK_WEBHOOK_URL`. Notifications contain a plain-text
question and an **Open in AskHuman** button. Slack interactive callbacks and OAuth app
installation are not required. Questions cannot trigger Slack mention syntax.

## Microsoft Teams

Create a webhook endpoint that accepts Teams Adaptive Card messages, such as a Teams
Workflow configured for that payload. Set `url` to its HTTPS endpoint. Your Workflow's
authentication policy must allow server-side webhook delivery. Tenant policies and
Workflow templates vary; test your selected endpoint before routing important requests.
The card links to the common response page.

## Discord

Create a channel webhook and set `url`. Mentions are disabled in outgoing payloads.
The service does not run a Discord bot or ingest Discord messages.

## Telegram

Create a bot, start a conversation with it (or add it to the intended group), and set
`bot_token` and `chat_id`. The destination must allow that bot to send messages.

## Email

Configure `host`, `from`, and `to`. Optional settings are `port`, `username`, `password`,
and `security`. Default transport is STARTTLS on port 587. Set `security` to `tls` for
implicit TLS, defaulting to port 465 when no port is supplied. TLS certificate validation
is mandatory. Credentials are optional for authenticated network relays. Inbound email
parsing is not required; the email contains a response link.

## SMS and WhatsApp

Both use Twilio. Set `account_sid`, `auth_token`, `from`, and `to`. Use E.164 phone numbers.
WhatsApp prefixes are added automatically. You must provision a sending number or
WhatsApp sender and meet Twilio/WhatsApp account, opt-in, template, and conversation-window
requirements. This adapter submits a free-form message; it does not manage approved
WhatsApp templates. Outside the allowed messaging window, use a custom webhook that
selects an approved template. Provider delivery may incur fees.

## Custom webhooks, mobile push, or another channel

Set `url` to an HTTPS endpoint and `secret` to a random shared signing secret. The
endpoint can connect to your own messaging, mobile push, ticketing, or voice system.
It receives JSON:

```json
{
  "event": "human.requested",
  "delivery_id": "42",
  "request": { "id": "uuid", "question": "Which source?", "status": "pending" },
  "reply_url": "https://human.example.com/reply/uuid#scoped-token"
}
```

The actual `request` is the complete REST request object. Verify the signature against
the raw request body, using a constant-time comparison:

```python
import hashlib
import hmac
import time

def verify(secret: str, headers: dict, raw_body: bytes) -> bool:
    timestamp = headers["x-askhuman-timestamp"]
    if abs(time.time() - int(timestamp)) > 300:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(headers["x-askhuman-signature"], expected)
```

Return 2xx once the notification has been accepted. Deduplicate
`X-AskHuman-Delivery` / `delivery_id` across retries, including across service restarts.
The signature timestamp changes on retry; the delivery ID remains stable. Keep reply
links private. Custom receivers may submit the same `AnswerInput` JSON to
`POST /api/replies/REQUEST_ID` with `Authorization: Bearer FRAGMENT_TOKEN`, or display the
response form. Never send the operator key to an external receiver.

## Retry and configuration behavior

Notifications get at most five attempts. Backoff is 2 raised to the attempt number in
seconds, capped at 300 seconds. Unfinished work is reclaimed after a 60-second lease.
Provider acceptance is recorded as `delivered`; it does not prove a person read the
message. Deadlines, answers, and cancellations stop queued retries. A notification already
in flight may arrive after an answer; the response page still rejects another answer.

Routing is chosen at request creation. Pending deliveries look up their channel's latest
settings when they run. Changing a destination can therefore affect already queued
notifications. Removing or disabling a channel causes its queued deliveries to fail.
Inspect errors in the inbox. Existing questions remain answerable there.

Channel URLs must be HTTPS. Only the operator may configure delivery destinations; the
service intentionally permits operator-selected internal HTTPS webhooks. It is not a
multi-tenant, untrusted webhook relay. Outbound HTTP does not follow redirects or inherit
proxy credentials from the environment.

Configuration and payload contracts are covered by local mocked tests. Actual delivery
requires validating your provider account and endpoint with your own credentials.
