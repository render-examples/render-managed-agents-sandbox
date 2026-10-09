// Offline tests for the orchestrator's decisions. No network. Mirrors tests/test_orchestrator.py.
import assert from "node:assert/strict";
import { test } from "node:test";
import type Anthropic from "@anthropic-ai/sdk";
import type { BetaSelfHostedWork } from "@anthropic-ai/sdk/resources/beta/environments/work";
import { type Settings, settingsFromEnv } from "../src/config.js";
import { ServerError } from "@renderinc/sdk";
import { LaunchError, retryTransient, START_CMD } from "../src/launcher.js";
import { Orchestrator } from "../src/orchestrator.js";

const settings = (over: Partial<Settings> = {}): Settings => ({
  environmentId: "env_1", environmentKey: "sk-ant-oat01-key", sandboxTimeoutSeconds: 600,
  maxConcurrentSessions: 2, networkPolicy: "allow-all", allowedDomains: [], sdkVersion: "1.12.1",
  workerMaxIdle: "60s", keepSandboxes: false, forwardEnvironmentKey: false, readyTimeoutSeconds: 90,
  sessionPersistence: true, persistenceKind: "filesystem", sandboxPlan: "starter", ...over,
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

class FakeWork {
  stopped: [string, boolean][] = [];
  items: BetaSelfHostedWork[] = [];
  pollerOpts: Record<string, unknown> = {};
  async stop(workId: string, p: { environment_id: string; force?: boolean }) {
    this.stopped.push([workId, Boolean(p.force)]);
  }
  poller(opts: Record<string, unknown>) {
    this.pollerOpts = opts;
    const items = this.items;
    return {
      async *[Symbol.asyncIterator]() {
        yield* items;
        await new Promise(() => {}); // then block like a real long-poll
      },
    };
  }
}

class FakeLauncher {
  calls: [string, string | null, string | undefined][] = [];
  active = 0;
  peak = 0;
  constructor(private exitCode = 0, private raiseLaunch = false, private delay = 0) {}
  startSnapshots: (string | undefined)[] = [];
  async runSession(workId: string, _sessionId: string, secret: string | null, environmentKey?: string,
    _signal?: AbortSignal, startSnapshotId?: string) {
    this.calls.push([workId, secret, environmentKey]);
    this.startSnapshots.push(startSnapshotId);
    this.active += 1;
    this.peak = Math.max(this.peak, this.active);
    try {
      await sleep(this.delay);
      if (this.raiseLaunch) throw new LaunchError("boom");
      return { sandboxId: "sbx_1", exitCode: this.exitCode };
    } finally {
      this.active -= 1;
    }
  }
  async watch() {
    return { sandboxId: "sbx_1", exitCode: 0 };
  }
  async discover() {
    return [];
  }
}

function make(launcher: FakeLauncher, over: Partial<Settings> = {}, metadata: Record<string, string> = {}) {
  const work = new FakeWork();
  const sessions = { retrieve: async (id: string) => ({ id, metadata }) };
  const anthropic = { beta: { environments: { work }, sessions } } as unknown as Anthropic;
  const orch = new Orchestrator(settings(over), anthropic);
  orch.launcher = launcher;
  return { orch, work };
}

const item = (i: number, secret: string | null = "sec") =>
  ({ id: `work_${i}`, data: { id: `sesn_${i}` }, secret }) as unknown as BetaSelfHostedWork;

async function runHandle(orch: Orchestrator, w: BetaSelfHostedWork) {
  await orch.slots.acquire();
  await orch.handle(w);
}

test("clean exit does not stop the work item", async () => {
  const { orch, work } = make(new FakeLauncher(0));
  await runHandle(orch, item(1));
  assert.deepEqual(work.stopped, []);
});

test("missing secret is refused without forwarding", async () => {
  const launcher = new FakeLauncher();
  const { orch, work } = make(launcher);
  await runHandle(orch, item(1, null));
  assert.deepEqual(launcher.calls, []);
  assert.deepEqual(work.stopped, [["work_1", true]]);
});

test("environment key only sent when opted in", async () => {
  const launcher = new FakeLauncher();
  await runHandle(make(launcher).orch, item(1));
  assert.equal(launcher.calls[0][2], undefined);
  const launcher2 = new FakeLauncher();
  await runHandle(make(launcher2, { forwardEnvironmentKey: true }).orch, item(2, null));
  assert.deepEqual(launcher2.calls[0], ["work_2", null, "sk-ant-oat01-key"]);
});

test("launch failure and crash force-stop the work item", async () => {
  const a = make(new FakeLauncher(0, true));
  await runHandle(a.orch, item(1));
  const b = make(new FakeLauncher(1));
  await runHandle(b.orch, item(2));
  assert.deepEqual(a.work.stopped, [["work_1", true]]);
  assert.deepEqual(b.work.stopped, [["work_2", true]]);
});

test("concurrency cap holds and slots are released", async () => {
  const launcher = new FakeLauncher(0, false, 50);
  const { orch, work } = make(launcher, { maxConcurrentSessions: 2 });
  work.items = [0, 1, 2, 3, 4, 5].map((i) => item(i));
  void orch.run();
  for (let i = 0; i < 100 && !(launcher.calls.length === 6 && launcher.active === 0); i++) await sleep(20);
  assert.equal(launcher.calls.length, 6);
  assert.equal(launcher.peak, 2);
  assert.equal(work.pollerOpts.autoStop, false);
  assert.equal(orch.slots.available, 1); // one slot held by the blocked poll, like the Python version's semaphore
});

test("settings default to an allow-list", () => {
  const env = { ANTHROPIC_ENVIRONMENT_ID: "env_1", ANTHROPIC_ENVIRONMENT_KEY: "k", RENDER_API_KEY: "r", RENDER_WORKSPACE_ID: "tea-1" };
  const s = settingsFromEnv(env);
  assert.equal(s.networkPolicy, "allow-list");
  assert.deepEqual(s.allowedDomains, ["api.anthropic.com", "pypi.org", "files.pythonhosted.org"]);
  assert.throws(() => settingsFromEnv({ ...env, SANDBOX_NETWORK_POLICY: "open" }));
});

test("start command detaches the worker so exec returns right away", () => {
  assert.match(START_CMD, /\( setsid nohup bash -c '.*' > \/dev\/null 2>&1 < \/dev\/null & \) && echo started$/);
  assert.match(START_CMD, /^if \[ -e \/run\/claude-worker\/launched \]; then echo started; exit 0; fi; /);
});

test("session metadata names the starting snapshot", async () => {
  const launcher = new FakeLauncher();
  await runHandle(make(launcher, {}, { render_snapshot_id: "snp-task" }).orch, item(1));
  await runHandle(make(launcher).orch, item(2));
  assert.deepEqual(launcher.startSnapshots, ["snp-task", undefined]);
});

test("allow-list serializes current and legacy policy", async (t) => {
  const { Render } = await import("@renderinc/sdk");
  const { RenderSandboxLauncher } = await import("../src/launcher.js");
  let captured: any;
  t.mock.method(globalThis, "fetch", async (request: Request) => {
    captured = await request.json();
    return new Response(JSON.stringify({ id: "sbx-test", status: "creating" }), {
      status: 201, headers: { "content-type": "application/json" },
    });
  });
  const sdk = new Render({ token: "test-key", ownerId: "tea-test" });
  const launcher = new RenderSandboxLauncher(sdk.experimental.sandboxes,
    settings({ networkPolicy: "allow-list", allowedDomains: ["api.anthropic.com", "*.githubusercontent.com"] }));
  await launcher.createSandbox({});
  assert.deepEqual(captured.networkPolicy, {
    type: "allow-list", default: "allow-list",
    rules: [{ domain: "api.anthropic.com", protocol: "https" },
      { domain: "*.githubusercontent.com", protocol: "https" }],
    allowedDomains: ["api.anthropic.com", "*.githubusercontent.com"],
  });
});

test("transient sandbox errors are retried", async () => {
  let calls = 0;
  const result = await retryTransient(async () => {
    calls += 1;
    if (calls < 3) throw new ServerError("503", 503);
    return "ok";
  }, "test", 5, 0);
  assert.equal(result, "ok");
  assert.equal(calls, 3);
});
