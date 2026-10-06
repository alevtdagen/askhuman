export type Kind =
  | "ask"
  | "decision"
  | "approval"
  | "clarification"
  | "expertise"
  | "verification"
  | "exception"
  | "notification";
export type Status = "pending" | "answered" | "expired" | "cancelled" | "notified";
export interface QuestionOptions {
  kind?: Kind;
  context?: string;
  options?: string[];
  recipient?: string | null;
  urgency?: "low" | "normal" | "high";
  timeout_seconds?: number;
  idempotencyKey?: string;
}
export interface Answer {
  request_id: string;
  answer: string;
  selected_option: string | null;
  approved: boolean | null;
  respondent: string;
  timestamp: string;
  source: "admin" | "reply_link";
}
export interface HumanRequest {
  id: string;
  question: string;
  kind: Kind;
  context: string;
  options: string[];
  recipient: string | null;
  urgency: "low" | "normal" | "high";
  timeout_seconds: number;
  status: Status;
  created_at: string;
  expires_at: string;
  response: Answer | null;
  deliveries: {
    channel: string;
    status: "pending" | "sending" | "delivered" | "failed" | "skipped";
    attempts: number;
    error: string | null;
  }[];
}
export interface WaitOptions {
  waitTimeout?: number;
  signal?: AbortSignal;
}
export interface ClientOptions {
  baseUrl?: string;
  apiKey?: string;
  fetch?: typeof fetch;
}

export class AskHumanError extends Error {
  constructor(
    message: string,
    public readonly statusCode?: number,
  ) {
    super(message);
    this.name = "AskHumanError";
  }
}
export class HumanTimeout extends AskHumanError {
  constructor(
    public readonly requestId: string,
    public readonly expired = false,
  ) {
    super(`Request ${requestId} ${expired ? "expired" : "is still pending"}`);
    this.name = "HumanTimeout";
  }
}
export class HumanCancelled extends AskHumanError {
  constructor(public readonly requestId: string) {
    super(`Request ${requestId} was cancelled`);
    this.name = "HumanCancelled";
  }
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });

export class AskHuman {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetcher: typeof fetch;
  constructor(options: ClientOptions = {}) {
    const env = typeof process !== "undefined" ? process.env : {};
    this.baseUrl = (options.baseUrl ?? env.ASKHUMAN_BASE_URL ?? "http://127.0.0.1:8765").replace(
      /\/$/,
      "",
    );
    this.apiKey = options.apiKey ?? env.ASKHUMAN_API_KEY ?? "";
    this.fetcher = options.fetch ?? globalThis.fetch;
    if (!this.apiKey) throw new AskHumanError("Set ASKHUMAN_API_KEY or supply apiKey");
  }

  private async request(
    method: string,
    path: string,
    body?: unknown,
    key?: string,
    signal?: AbortSignal,
  ): Promise<HumanRequest> {
    for (let attempt = 0; attempt < 3; attempt++) {
      signal?.throwIfAborted();
      try {
        const deadline = AbortSignal.timeout(35_000);
        const response = await this.fetcher(`${this.baseUrl}${path}`, {
          method,
          redirect: "error",
          signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
          headers: {
            Authorization: `Bearer ${this.apiKey}`,
            "Content-Type": "application/json",
            ...(key ? { "Idempotency-Key": key } : {}),
          },
          body: body === undefined ? undefined : JSON.stringify(body),
        });
        if (response.status >= 500 && attempt < 2) {
          await response.body?.cancel();
          await sleep(200 * 2 ** attempt, signal);
          continue;
        }
        if (!response.ok)
          throw new AskHumanError((await response.text()).slice(0, 1500), response.status);
        return (await response.json()) as HumanRequest;
      } catch (error) {
        signal?.throwIfAborted();
        if (error instanceof AskHumanError) throw error;
        if (attempt === 2)
          throw new AskHumanError("AskHuman is unreachable or returned an invalid response");
        await sleep(200 * 2 ** attempt, signal);
      }
    }
    throw new AskHumanError("Request failed");
  }

  async create(question: string, options: QuestionOptions = {}): Promise<HumanRequest> {
    const { idempotencyKey, ...body } = options;
    return this.request(
      "POST",
      "/v1/requests",
      { question, ...body },
      idempotencyKey ?? crypto.randomUUID(),
    );
  }
  async get(requestId: string): Promise<HumanRequest> {
    return this.request("GET", `/v1/requests/${encodeURIComponent(requestId)}`);
  }
  async cancel(requestId: string): Promise<HumanRequest> {
    return this.request("POST", `/v1/requests/${encodeURIComponent(requestId)}/cancel`);
  }
  async wait(requestId: string, options: WaitOptions = {}): Promise<Answer> {
    if (
      options.waitTimeout !== undefined &&
      (!Number.isFinite(options.waitTimeout) || options.waitTimeout < 0)
    ) {
      throw new RangeError("waitTimeout must be a finite nonnegative number");
    }
    const end =
      options.waitTimeout === undefined ? Infinity : performance.now() + options.waitTimeout * 1000;
    while (true) {
      const seconds = Math.max(0, Math.min(25, Math.floor((end - performance.now()) / 1000)));
      const request = await this.request(
        "GET",
        `/v1/requests/${encodeURIComponent(requestId)}/wait?seconds=${seconds}`,
        undefined,
        undefined,
        options.signal,
      );
      if (request.status === "answered") {
        if (!request.response) throw new AskHumanError("Answered request has no response");
        return request.response;
      }
      if (request.status === "expired") throw new HumanTimeout(requestId, true);
      if (request.status === "cancelled") throw new HumanCancelled(requestId);
      if (request.status === "notified")
        throw new AskHumanError("Notifications do not have an answer");
      if (performance.now() >= end) throw new HumanTimeout(requestId);
      if (seconds === 0) await sleep(Math.min(100, end - performance.now()), options.signal);
    }
  }
  async ask(question: string, options: QuestionOptions & WaitOptions = {}): Promise<Answer> {
    const { waitTimeout, signal, ...input } = options;
    const request = await this.create(question, input);
    return this.wait(request.id, { waitTimeout, signal });
  }
  async decide(
    question: string,
    options: Omit<QuestionOptions, "kind" | "options"> & WaitOptions & { options: string[] },
  ): Promise<Answer> {
    return this.ask(question, { ...options, kind: "decision" });
  }
  async approve(
    question: string,
    options: Omit<QuestionOptions, "kind" | "options"> & WaitOptions = {},
  ): Promise<Answer> {
    return this.ask(question, { ...options, kind: "approval" });
  }
  async clarify(
    question: string,
    options: Omit<QuestionOptions, "kind"> & WaitOptions = {},
  ): Promise<Answer> {
    return this.ask(question, { ...options, kind: "clarification" });
  }
  async notify(
    question: string,
    options: Omit<QuestionOptions, "kind" | "options"> = {},
  ): Promise<HumanRequest> {
    return this.create(question, { ...options, kind: "notification" });
  }
}

/** JSON Schema function tools for custom agents and OpenAI Responses API. */
export function toolDefinitions() {
  const parameters = {
    type: "object",
    additionalProperties: false,
    properties: {
      question: { type: "string" },
      kind: {
        type: "string",
        enum: [
          "ask",
          "decision",
          "approval",
          "clarification",
          "expertise",
          "verification",
          "exception",
          "notification",
        ],
      },
      context: { type: "string" },
      options: { type: "array", items: { type: "string" } },
      recipient: { type: ["string", "null"] },
      urgency: { type: "string", enum: ["low", "normal", "high"] },
      timeout_seconds: { type: "integer" },
      idempotency_key: { type: ["string", "null"] },
    },
    required: [
      "question",
      "kind",
      "context",
      "options",
      "recipient",
      "urgency",
      "timeout_seconds",
      "idempotency_key",
    ],
  };
  const idSchema = {
    type: "object",
    additionalProperties: false,
    properties: { request_id: { type: "string" } },
    required: ["request_id"],
  };
  return [
    {
      type: "function",
      name: "ask_human",
      description:
        "Ask a human for input, a decision, or explicit approval. Retain the request ID and check pending requests with get_human_response. Only response.approved=true grants approval.",
      parameters,
      strict: true,
    },
    {
      type: "function",
      name: "get_human_response",
      description: "Read a saved human request by ID.",
      parameters: idSchema,
      strict: true,
    },
    {
      type: "function",
      name: "cancel_human_request",
      description: "Cancel a pending human request.",
      parameters: idSchema,
      strict: true,
    },
  ];
}

export async function handleTool(
  client: AskHuman,
  name: string,
  arguments_: Record<string, unknown> | string,
  callId?: string,
): Promise<HumanRequest> {
  const args = typeof arguments_ === "string" ? JSON.parse(arguments_) : { ...arguments_ };
  if (name === "ask_human") {
    const { question, idempotency_key, ...options } = args;
    return client.create(question, { ...options, idempotencyKey: idempotency_key ?? callId });
  }
  if (name === "get_human_response") return client.get(args.request_id);
  if (name === "cancel_human_request") return client.cancel(args.request_id);
  throw new AskHumanError(`Unknown AskHuman tool: ${name}`);
}
