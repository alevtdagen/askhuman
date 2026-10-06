import test from "node:test";
import assert from "node:assert/strict";
import {
  AskHuman,
  AskHumanError,
  HumanTimeout,
  HumanCancelled,
  toolDefinitions,
  handleTool,
} from "../dist/index.js";

const saved = { id: "request-1", status: "pending", response: null, deliveries: [] };
const answer = {
  request_id: "request-1",
  answer: "Finance",
  selected_option: "Finance",
  approved: null,
  respondent: "Sam",
  timestamp: "2026-10-06T12:00:00Z",
  source: "admin",
};
const response = (body) =>
  new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });

test("creation sends only question fields, bearer auth, and an idempotency key", async () => {
  let received;
  const human = new AskHuman({
    apiKey: "test-key",
    fetch: async (url, options) => {
      received = options;
      return response(saved);
    },
  });
  await human.create("Which source?", { options: ["Finance", "CRM"], idempotencyKey: "job-1" });
  assert.equal(received.headers.Authorization, "Bearer test-key");
  assert.equal(received.headers["Idempotency-Key"], "job-1");
  assert.equal(JSON.parse(received.body).idempotencyKey, undefined);
  assert.deepEqual(JSON.parse(received.body).options, ["Finance", "CRM"]);
});

test("retry after a lost response keeps the same idempotency key", async () => {
  const keys = [];
  const human = new AskHuman({
    apiKey: "test",
    fetch: async (url, options) => {
      keys.push(options.headers["Idempotency-Key"]);
      if (keys.length === 1) throw new TypeError("network interrupted");
      return response(saved);
    },
  });
  await human.create("Test");
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);
  assert.ok(keys[0]);
});

test("a pending request times out locally and resumes with a typed answer", async () => {
  let answered = false;
  const human = new AskHuman({
    apiKey: "test",
    fetch: async () =>
      response(answered ? { ...saved, status: "answered", response: answer } : saved),
  });
  await assert.rejects(
    human.wait(saved.id, { waitTimeout: 0 }),
    (error) => error instanceof HumanTimeout && error.requestId === saved.id && !error.expired,
  );
  answered = true;
  assert.equal((await human.wait(saved.id)).selected_option, "Finance");
});

test("expired and cancelled requests never produce approval", async () => {
  for (const status of ["expired", "cancelled"]) {
    const human = new AskHuman({
      apiKey: "test",
      fetch: async () => response({ ...saved, status }),
    });
    await assert.rejects(
      human.wait(saved.id),
      status === "expired" ? HumanTimeout : HumanCancelled,
    );
  }
});

test("rejection remains a false boolean", async () => {
  const human = new AskHuman({
    apiKey: "test",
    fetch: async () =>
      response({ ...saved, status: "answered", response: { ...answer, approved: false } }),
  });
  assert.equal((await human.approve("Proceed?")).approved, false);
});

test("abort cancels waiting without cancelling the saved question", async () => {
  const controller = new AbortController();
  controller.abort(new Error("Stop waiting"));
  let calls = 0;
  const human = new AskHuman({
    apiKey: "test",
    fetch: async () => {
      calls++;
      return response(saved);
    },
  });
  await assert.rejects(human.wait(saved.id, { signal: controller.signal }), /Stop waiting/);
  assert.equal(calls, 0);
});

test("invalid wait budgets fail immediately", async () => {
  const human = new AskHuman({ apiKey: "test" });
  for (const waitTimeout of [-1, Infinity, NaN])
    await assert.rejects(human.wait(saved.id, { waitTimeout }), RangeError);
});

test("authentication failures do not retry", async () => {
  let calls = 0;
  const human = new AskHuman({
    apiKey: "test",
    fetch: async () => {
      calls++;
      return new Response("Unauthorized", { status: 401 });
    },
  });
  await assert.rejects(
    human.get(saved.id),
    (error) => error instanceof AskHumanError && error.statusCode === 401,
  );
  assert.equal(calls, 1);
});

test("function tool dispatch persists the provider call ID", async () => {
  let body, key;
  const human = new AskHuman({
    apiKey: "test",
    fetch: async (url, options) => {
      body = JSON.parse(options.body);
      key = options.headers["Idempotency-Key"];
      return response(saved);
    },
  });
  await handleTool(human, "ask_human", { question: "Which source?", kind: "ask" }, "call-123");
  assert.equal(key, "call-123");
  assert.equal(body.question, "Which source?");
  assert.equal(toolDefinitions().length, 3);
});
