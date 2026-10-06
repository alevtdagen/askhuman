export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "className") node.className = value;
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined)
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export async function api(path, token, method = "GET", body) {
  const result = await fetch(path, {
    method,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await result.json();
  if (!result.ok) {
    const message = Array.isArray(data.detail)
      ? data.detail.map((e) => e.msg).join("; ")
      : data.detail;
    const error = new Error(message || "Request failed. Please try again.");
    error.status = result.status;
    throw error;
  }
  return data;
}

export function requestCard(request, onAnswer, expanded = false) {
  const card = el("article", { className: "request-card" });
  const meta = el(
    "div",
    { className: "request-meta" },
    el("span", { className: `badge ${request.kind}` }, request.kind),
    request.urgency === "high" ? el("span", { className: "urgent" }, "High priority") : null,
    el("span", { className: "recipient" }, request.recipient || "Default recipient"),
    el(
      "time",
      { dateTime: request.created_at },
      new Date(request.created_at).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      }),
    ),
  );
  card.append(meta, el("h3", {}, request.question));
  if (request.context) card.append(el("p", { className: "context" }, request.context));
  if (request.status !== "pending") {
    const response = request.response;
    card.append(
      el(
        "div",
        { className: `outcome ${response?.approved === false ? "rejected" : ""}` },
        el(
          "strong",
          {},
          response
            ? response.approved === true
              ? "Approved"
              : response.approved === false
                ? "Rejected"
                : "Answered"
            : request.status === "notified"
              ? "Notification queued"
              : request.status,
        ),
        response
          ? el(
              "p",
              {},
              response.selected_option && response.selected_option !== response.answer
                ? `${response.selected_option} · ${response.answer}`
                : response.answer,
            )
          : null,
        response
          ? el(
              "small",
              {},
              `By ${response.respondent} · ${new Date(response.timestamp).toLocaleString()}`,
            )
          : null,
      ),
    );
  } else {
    const details = el("details", { className: "respond", open: expanded });
    details.append(el("summary", {}, "Respond to request", el("span", {}, "↗")));
    const form = el("form", { className: "answer-form" });
    let selected = null;
    const optionButtons = [];
    if (request.options.length) {
      const choices = el("div", {
        className: "options",
        role: "group",
        "aria-label": "Choose your answer",
      });
      for (const option of request.options) {
        const button = el(
          "button",
          {
            type: "button",
            className: "option",
            "aria-pressed": "false",
            onClick: () => {
              selected = option;
              for (const item of optionButtons) {
                item.classList.toggle("chosen", item === button);
                item.setAttribute("aria-pressed", String(item === button));
              }
            },
          },
          option,
        );
        optionButtons.push(button);
        choices.append(button);
      }
      form.append(choices);
    }
    const answer = el("textarea", {
      rows: 3,
      maxLength: 24000,
      required: !request.options.length,
      placeholder: request.options.length
        ? "Add context for your agent (optional)"
        : "What should your agent know?",
    });
    form.append(
      el("label", {}, request.options.length ? "Additional context" : "Your answer", answer),
    );
    const respondent = el("input", {
      required: true,
      maxLength: 120,
      placeholder: "Your name",
      value: localStorage.getItem("askhuman.name") || "",
      autoComplete: "name",
    });
    form.append(el("label", {}, "Answered by", respondent));
    const error = el("p", { className: "error", role: "alert" });
    const submit = el(
      "button",
      { className: "primary", type: "submit" },
      "Send answer",
      el("span", {}, "→"),
    );
    form.append(
      error,
      el(
        "div",
        { className: "answer-footer" },
        el("small", {}, `Waiting until ${new Date(request.expires_at).toLocaleString()}`),
        submit,
      ),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.textContent = "";
      if (request.options.length && selected === null) {
        error.textContent = "Choose an option first.";
        return;
      }
      submit.disabled = true;
      try {
        const body = {
          answer: answer.value,
          selected_option: selected,
          respondent: respondent.value,
          approved: request.kind === "approval" ? selected === "Approve" : null,
        };
        await onAnswer(body);
        localStorage.setItem("askhuman.name", respondent.value);
      } catch (err) {
        error.textContent = err.message;
        submit.disabled = false;
      }
    });
    details.append(form);
    card.append(details);
  }
  const failed = request.deliveries?.filter((d) => d.status === "failed" || d.error);
  if (failed?.length)
    card.append(
      el(
        "p",
        { className: "delivery-warning" },
        `Delivery needs attention: ${failed.map((d) => `${d.channel} (${d.error || d.status})`).join(", ")}. You can still answer here.`,
      ),
    );
  card.append(el("div", { className: "request-id" }, request.id));
  return card;
}
