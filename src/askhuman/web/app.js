import { api, el, requestCard } from "./shared.js";

const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("askhuman.operator") || "";
let config,
  fields,
  requests = [],
  filter = "pending",
  limit = 100;
const names = {
  web: "Web inbox",
  slack: "Slack",
  teams: "Microsoft Teams",
  discord: "Discord",
  telegram: "Telegram",
  email: "Email",
  sms: "SMS · Twilio",
  whatsapp: "WhatsApp · Twilio",
  webhook: "Custom webhook",
};
const fieldLabels = {
  url: "Webhook URL",
  bot_token: "Bot token",
  chat_id: "Chat ID",
  host: "SMTP host",
  port: "SMTP port",
  username: "Username",
  password: "Password",
  from: "From",
  to: "To",
  security: "Security (starttls or tls)",
  account_sid: "Twilio account SID",
  auth_token: "Twilio auth token",
  secret: "Signing secret",
};

function feedback(message, error = false) {
  $("feedback").textContent = message;
  $("feedback").hidden = false;
  $("feedback").className = error ? "feedback error" : "feedback";
}
async function refresh() {
  const [pending, answered, all] = await Promise.all([
    api("/api/admin/requests?status=pending&limit=200", token),
    api("/api/admin/requests?status=answered&limit=200", token),
    api(
      `/api/admin/requests?limit=${Math.min(limit, 200)}${filter !== "all" ? `&status=${filter}` : ""}`,
      token,
    ),
  ]);
  requests = all;
  $("pending-count").textContent = pending.length >= 200 ? "200+" : pending.length;
  $("nav-count").textContent = $("pending-count").textContent;
  $("answered-count").textContent = answered.length >= 200 ? "200+" : answered.length;
  $("channel-count").textContent = config.channels.filter((c) => c.enabled).length;
  renderRequests();
}
function renderRequests() {
  $("requests").replaceChildren();
  for (const request of requests)
    $("requests").append(
      requestCard(request, async (body) => {
        await api(`/api/admin/requests/${request.id}/answer`, token, "POST", body);
        feedback("Answer sent. Your agent can continue.");
        await refresh();
      }),
    );
  if (!requests.length)
    $("requests").append(
      el(
        "div",
        { className: "empty-state" },
        el("div", { className: "empty-icon" }, "✓"),
        el("h3", {}, filter === "pending" ? "You’re all caught up." : "Nothing here yet."),
        el(
          "p",
          {},
          filter === "pending"
            ? "When an agent needs your judgment, its question will appear here."
            : "Your workspace activity will appear here.",
        ),
        el("code", {}, 'await human.ask("Which direction should I take?")'),
      ),
    );
  $("count-note").textContent =
    `Showing ${requests.length} requests. Requests stay saved while your agents wait.`;
  $("load-more").hidden = requests.length < Math.min(limit, 200);
}

function checks(selected, update) {
  return config.channels
    .filter((c) => c.enabled)
    .map((channel) =>
      el(
        "label",
        { className: "check-chip" },
        el("input", {
          type: "checkbox",
          checked: selected.includes(channel.id),
          onChange: (event) => {
            const next = event.target.checked
              ? [...selected, channel.id]
              : selected.filter((id) => id !== channel.id);
            selected = next;
            update(next);
          },
        }),
        channel.id,
      ),
    );
}
function renderRouting() {
  $("defaults").replaceChildren(
    ...checks(config.default_channels, (values) => (config.default_channels = values)),
  );
  $("routes").replaceChildren();
  for (const [recipient, targets] of Object.entries(config.routes)) {
    $("routes").append(
      el(
        "div",
        { className: "route" },
        el(
          "div",
          { className: "route-title" },
          el("strong", {}, recipient),
          el(
            "button",
            {
              type: "button",
              className: "quiet danger",
              onClick: () => {
                delete config.routes[recipient];
                renderRouting();
              },
            },
            "Remove",
          ),
        ),
        el(
          "div",
          { className: "checks" },
          ...checks(targets, (values) => (config.routes[recipient] = values)),
        ),
      ),
    );
  }
}
function renderChannels() {
  $("channels").replaceChildren();
  for (const channel of config.channels) {
    const card = el("article", { className: "channel-card" });
    card.append(
      el(
        "div",
        { className: "channel-header" },
        el("span", { className: `channel-symbol ${channel.type}` }, names[channel.type][0]),
        el("div", {}, el("h3", {}, names[channel.type]), el("small", {}, channel.id)),
        el(
          "button",
          {
            className: "quiet danger",
            type: "button",
            "aria-label": `Remove ${channel.id}`,
            onClick: () => {
              config.channels = config.channels.filter((c) => c !== channel);
              config.default_channels = config.default_channels.filter((id) => id !== channel.id);
              for (const key of Object.keys(config.routes))
                config.routes[key] = config.routes[key].filter((id) => id !== channel.id);
              renderChannels();
            },
          },
          "×",
        ),
      ),
    );
    if (channel.type === "web")
      card.append(
        el(
          "p",
          { className: "hint" },
          "A quiet home for every request. Answer directly in this inbox.",
        ),
      );
    else {
      const details = el("details", {
        className: "channel-settings",
        open: !Object.keys(channel.settings).length,
      });
      details.append(el("summary", {}, "Connection settings"));
      for (const field of fields[channel.type]) {
        const secret = ["url", "bot_token", "password", "auth_token", "secret"].includes(field);
        const input = el("input", {
          type: secret ? "password" : "text",
          value: channel.settings[field] || "",
          autoComplete: "off",
          placeholder: field === "security" ? "starttls" : field === "port" ? "587" : "",
          onInput: (event) => {
            if (event.target.value) channel.settings[field] = event.target.value;
            else delete channel.settings[field];
          },
        });
        details.append(el("label", {}, fieldLabels[field], input));
      }
      card.append(details);
    }
    card.append(
      el(
        "label",
        { className: "check-chip enabled-toggle" },
        el("input", {
          type: "checkbox",
          checked: channel.enabled,
          onChange: (event) => {
            channel.enabled = event.target.checked;
            if (!channel.enabled) {
              config.default_channels = config.default_channels.filter((id) => id !== channel.id);
              for (const key of Object.keys(config.routes))
                config.routes[key] = config.routes[key].filter((id) => id !== channel.id);
            }
            renderRouting();
          },
        }),
        "Enabled",
      ),
    );
    $("channels").append(card);
  }
  renderRouting();
}

async function login() {
  [config, fields] = await Promise.all([
    api("/api/admin/config", token),
    api("/api/admin/channel-types", token),
  ]);
  await refresh();
  renderChannels();
  sessionStorage.setItem("askhuman.operator", token);
  $("login").hidden = true;
  $("app").hidden = false;
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  token = $("operator-key").value.trim();
  try {
    await login();
    $("operator-key").value = "";
  } catch (error) {
    $("login-error").textContent = error.message;
  }
});
$("logout").addEventListener("click", () => {
  sessionStorage.removeItem("askhuman.operator");
  location.reload();
});
for (const name of ["inbox", "channels"])
  $("nav-" + name).addEventListener("click", () => {
    $("inbox-view").hidden = name !== "inbox";
    $("channels-view").hidden = name !== "channels";
    $("nav-inbox").classList.toggle("active", name === "inbox");
    $("nav-channels").classList.toggle("active", name === "channels");
    $("breadcrumb").textContent =
      `Workspace / ${name === "inbox" ? "Inbox" : "Channels & routing"}`;
    $("feedback").hidden = true;
  });
$("refresh").addEventListener("click", () =>
  refresh().catch((error) => feedback(error.message, true)),
);
document.querySelectorAll("[data-filter]").forEach((button) =>
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    limit = 100;
    document
      .querySelectorAll("[data-filter]")
      .forEach((b) => b.classList.toggle("selected", b === button));
    refresh().catch((error) => feedback(error.message, true));
  }),
);
$("load-more").addEventListener("click", async () => {
  try {
    const more = await api(
      `/api/admin/requests?limit=100&offset=${requests.length}${filter !== "all" ? `&status=${filter}` : ""}`,
      token,
    );
    requests.push(...more);
    renderRequests();
    $("load-more").hidden = more.length < 100;
  } catch (error) {
    feedback(error.message, true);
  }
});
$("add-channel").addEventListener("submit", (event) => {
  event.preventDefault();
  const id = $("channel-id").value.trim();
  if (config.channels.some((c) => c.id === id)) {
    feedback("That channel ID already exists.", true);
    return;
  }
  config.channels.push({ id, type: $("channel-type").value, enabled: true, settings: {} });
  $("channel-id").value = "";
  renderChannels();
  feedback("Channel added. Fill in its settings, choose a route, then save changes.");
});
$("add-route").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = $("route-name").value.trim();
  if (!name || Object.hasOwn(config.routes, name)) {
    feedback("Choose a new recipient name.", true);
    return;
  }
  Object.defineProperty(config.routes, name, {
    value: [...config.default_channels],
    writable: true,
    configurable: true,
    enumerable: true,
  });
  $("route-name").value = "";
  renderRouting();
});
$("save-config").addEventListener("click", async () => {
  try {
    await api("/api/admin/config", token, "PUT", config);
    feedback("Your delivery preferences are saved. New requests will use these routes.");
    await refresh();
  } catch (error) {
    feedback(error.message, true);
  }
});
if (token)
  login().catch(() => {
    sessionStorage.removeItem("askhuman.operator");
  });
setInterval(() => {
  if (
    !$("app").hidden &&
    !$("inbox-view").hidden &&
    !document.querySelector("details.respond[open]") &&
    requests.length <= 100
  ) {
    refresh().catch((error) => feedback(error.message, true));
  }
}, 10000);
