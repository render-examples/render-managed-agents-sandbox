// Run one Managed Agents session inside one Render sandbox. Mirrors orchestrator/launcher.py.
//
// Flow for each claimed work item:
//   1. Create a fresh sandbox (from a prebuilt snapshot if one exists).
//   2. Wait until it is running.
//   3. Copy in the bootstrap script and the per-session work secret.
//   4. Start the worker (sandbox/runner.py, Anthropic's SDK) detached, so the start command returns at once.
//   5. Follow its log until it exits.
//   6. Save the session's files to a snapshot, then terminate the sandbox.
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import {
  ClientError,
  SandboxSnapshotNotFoundError,
  SandboxSnapshotNotReadyError,
  ServerError,
  type Render,
} from "@renderinc/sdk";
import type { Settings } from "./config.js";
import { log } from "./log.js";

type Sandboxes = Render["experimental"]["sandboxes"];
type CreateInput = NonNullable<Parameters<Sandboxes["create"]>[0]>;

export const STATE_DIR = "/run/claude-worker";
export const BOOTSTRAP_LOCAL = fileURLToPath(new URL("../../sandbox/bootstrap.sh", import.meta.url));
export const BOOTSTRAP_REMOTE = `${STATE_DIR}/bootstrap.sh`;
export const RUNNER_LOCAL = fileURLToPath(new URL("../../sandbox/runner.py", import.meta.url));
export const RUNNER_REMOTE = `${STATE_DIR}/runner.py`;
const EXIT_MARKER = "__CLAUDE_WORKER_EXIT__";

// Starts the worker in its own session so it outlives this exec call.
// Its exit code is written to a file when it finishes. The launch is wrapped in its own
// subshell on purpose: written as `chmod && setsid ... & echo`, bash backgrounds the whole
// chain, and that background job keeps exec's output pipe open until the worker exits.
// A marker file makes the command safe to retry: a second run reports "started"
// without launching another worker.
export const START_CMD =
  `if [ -e ${STATE_DIR}/launched ]; then echo started; exit 0; fi; ` +
  `touch ${STATE_DIR}/launched && chmod 700 ${BOOTSTRAP_REMOTE} && ` +
  `( setsid nohup bash -c '${BOOTSTRAP_REMOTE} > ${STATE_DIR}/worker.log 2>&1; ` +
  `echo $? > ${STATE_DIR}/exit_code' > /dev/null 2>&1 < /dev/null & ) && echo started`;

// One long-lived request per session: stream the log until the exit file appears.
// This open request also counts as activity to Render. Render's sandbox code can pause
// sandboxes with no API request in flight for 15 minutes (built, not yet switched on),
// and a background process like the worker doesn't count, so keep this request open.
const FOLLOW_CMD =
  `touch ${STATE_DIR}/worker.log; ` +
  `tail -n +$((OFFSET+1)) -F ${STATE_DIR}/worker.log 2>/dev/null & T=$!; ` +
  `while [ ! -f ${STATE_DIR}/exit_code ]; do sleep 1; done; sleep 1; kill $T 2>/dev/null; ` +
  `echo; echo ${EXIT_MARKER} $(cat ${STATE_DIR}/exit_code)`;

/** The sandbox could not be prepared, so the work item never started. */
export class LaunchError extends Error {}

export interface SessionResult {
  sandboxId: string;
  exitCode: number | null;
}

export interface Discovered {
  sandboxId: string;
  workId: string;
  sessionId: string;
  exitCode: number | null;
  running: boolean;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Errors a newly running sandbox can return before its agent accepts requests.
// Render's own SDK tests retry exec on these (404, 429, 503) after creating a sandbox.
const isTransient = (e: unknown) =>
  (e instanceof ClientError && (e.statusCode === 404 || e.statusCode === 429)) || e instanceof ServerError;

/** Run call() and retry while the sandbox returns transient errors. */
export async function retryTransient<T>(call: () => Promise<T>, what: string, attempts = 20, delayMs = 3000): Promise<T> {
  for (let attempt = 1; ; attempt++) {
    try {
      return await call();
    } catch (e) {
      if (!isTransient(e) || attempt >= attempts) throw e;
      log.info(`${what} not ready (${(e as Error).message}); retrying`);
      await sleep(delayMs);
    }
  }
}
const parseCode = (s: string) => (/^-?\d+$/.test(s) ? Number(s) : null);

export async function runCmd(sandboxes: Sandboxes, sandboxId: string, command: string): Promise<[number | null, string]> {
  let out = "";
  let code: number | null = null;
  for await (const event of await sandboxes.exec(sandboxId, command)) {
    if (event.type === "output") out += event.data;
    else if (event.type === "exit") code = event.exit_code;
  }
  return [code, out];
}

export class RenderSandboxLauncher {
  private captures = new Map<string, Promise<void>>();

  constructor(private sandboxes: Sandboxes, private s: Settings) {}

  sandboxEnv(workId: string, sessionId: string, environmentKey?: string): Record<string, string> {
    const env: Record<string, string> = {
      ANTHROPIC_ENVIRONMENT_ID: this.s.environmentId,
      ANTHROPIC_SESSION_ID: sessionId,
      ANTHROPIC_WORK_ID: workId,
      ANTHROPIC_SDK_VERSION: this.s.sdkVersion,
      WORKER_MAX_IDLE: this.s.workerMaxIdle,
    };
    if (this.s.anthropicBaseUrl) env.ANTHROPIC_BASE_URL = this.s.anthropicBaseUrl;
    if (environmentKey) env.ANTHROPIC_ENVIRONMENT_KEY = environmentKey; // only when FORWARD_ENVIRONMENT_KEY=true
    return env;
  }

  static sessionSnapshotName(sessionId: string): string {
    return `claude-session-${sessionId}`;
  }

  private async createOne(env: Record<string, string>, snapshotName?: string, snapshotId?: string) {
    // Send current rules and the legacy fields while SDK 1.2 and older servers coexist.
    const networkPolicy = this.s.networkPolicy === "allow-list"
      ? { type: "allow-list" as const, default: "allow-list" as const,
          rules: this.s.allowedDomains.map(domain => ({ domain, protocol: "https" as const })),
          allowedDomains: this.s.allowedDomains }
      : { type: this.s.networkPolicy, default: this.s.networkPolicy };
    const input: CreateInput = {
      timeoutSeconds: this.s.sandboxTimeoutSeconds,
      env,
      networkPolicy,
    };
    if (snapshotId) {
      input.snapshotId = snapshotId;
    } else if (snapshotName) {
      input.snapshotName = snapshotName;
      // A runtime snapshot restores only onto the plan it was taken on.
      if (this.s.persistenceKind === "runtime") input.plan = this.s.sandboxPlan;
    }
    return this.sandboxes.create(input);
  }

  /**
   * Pick the first snapshot that exists, in this order: the session's own saved snapshot,
   * the snapshot named in the session's metadata, the prepared snapshot, then the base image.
   */
  async createSandbox(env: Record<string, string>, sessionId?: string, startSnapshotId?: string) {
    const candidates: { name?: string; id?: string }[] = [];
    if (sessionId && this.s.sessionPersistence) {
      await this.waitForCapture(sessionId);
      candidates.push({ name: RenderSandboxLauncher.sessionSnapshotName(sessionId) });
    }
    if (startSnapshotId) candidates.push({ id: startSnapshotId });
    if (this.s.snapshotName) candidates.push({ name: this.s.snapshotName });
    for (const { name, id } of candidates) {
      try {
        const sandbox = await this.createOne(env, name, id);
        if (name !== this.s.snapshotName) log.info(`session=${sessionId} restored files from snapshot ${name ?? id}`);
        return sandbox;
      } catch (e) {
        const missing = e instanceof SandboxSnapshotNotFoundError || e instanceof SandboxSnapshotNotReadyError
          || (e instanceof ClientError && [400, 404, 409].includes(e.statusCode));
        if (!missing) throw e;
        if (id) log.warn(`session=${sessionId} metadata snapshot ${id} unavailable (${(e as Error).message})`);
        else if (name !== this.s.snapshotName) log.info(`session=${sessionId} has no saved snapshot yet`);
        else {
          // Snapshots expire after three days by default. The base image still works; the startup script installs the SDK.
          log.warn(`snapshot '${name}' unavailable (${(e as Error).message}); using base image`);
        }
      }
    }
    return this.createOne(env);
  }

  private async waitForCapture(sessionId: string) {
    const pending = this.captures.get(sessionId);
    if (!pending) return;
    log.info(`session=${sessionId} waiting for its previous sandbox to finish saving`);
    const timedOut = await Promise.race([pending.then(() => false), sleep(300_000).then(() => true)]);
    if (timedOut) log.warn(`session=${sessionId} previous save still running; continuing`);
  }

  /**
   * Snapshot the sandbox under the session's name so the next turn can resume it.
   * The work secret, worker log, and exit marker are removed first, so no credential
   * is captured. Older snapshots for the same session are deleted once the new one is ready.
   */
  async saveSession(sandboxId: string, sessionId: string): Promise<void> {
    const work = this.doSave(sandboxId, sessionId);
    this.captures.set(sessionId, work);
    try {
      await work;
    } finally {
      this.captures.delete(sessionId);
    }
  }

  private async doSave(sandboxId: string, sessionId: string) {
    const name = RenderSandboxLauncher.sessionSnapshotName(sessionId);
    try {
      await runCmd(this.sandboxes, sandboxId, `rm -rf ${STATE_DIR}`);
      let snap = await this.sandboxes.snapshots.create({ sandboxId, name, kind: this.s.persistenceKind });
      const deadline = Date.now() + 300_000;
      while (snap.status === "creating" && Date.now() < deadline) {
        await sleep(2000);
        snap = await this.sandboxes.snapshots.get({ sandboxGroupId: snap.sandboxGroupId, snapshotId: snap.id });
      }
      if (snap.status !== "available") {
        log.warn(`session=${sessionId} snapshot ${snap.id} ended ${snap.status}: ${snap.error ?? ""}`);
        return;
      }
      log.info(`session=${sessionId} saved files to snapshot ${name} (${snap.id})`);
      // Read every page; the list is newest first, 100 at a time.
      const older = [];
      for (let cursor: string | undefined; ; ) {
        const page = await this.sandboxes.snapshots.list({
          sandboxGroupId: snap.sandboxGroupId, status: ["available"], cursor, limit: 100,
        });
        older.push(...page.map((p) => p.snapshot).filter((s) => s.name === name && s.id !== snap.id));
        if (page.length < 100) break;
        cursor = page[page.length - 1].cursor;
      }
      for (const old of older) {
        try {
          await this.sandboxes.snapshots.delete({ sandboxGroupId: snap.sandboxGroupId, snapshotId: old.id });
        } catch (e) {
          log.warn(`could not delete old snapshot ${old.id}: ${(e as Error).message}`);
        }
      }
    } catch (e) {
      log.warn(`session=${sessionId} could not save files: ${(e as Error).message}`);
    }
  }

  async waitRunning(sandboxId: string) {
    const deadline = Date.now() + this.s.readyTimeoutSeconds * 1000;
    while (Date.now() < deadline) {
      const { status } = await this.sandboxes.get(sandboxId);
      if (status === "running") return;
      if (status === "errored" || status === "terminated") throw new LaunchError(`sandbox ${sandboxId} is ${status}`);
      await sleep(1000);
    }
    throw new LaunchError(`sandbox ${sandboxId} not running after ${this.s.readyTimeoutSeconds}s`);
  }

  async uploadSecret(sandboxId: string, secret: string) {
    await retryTransient(() => this.sandboxes.upload(sandboxId, `${STATE_DIR}/work_secret`, secret), `sandbox=${sandboxId} upload`);
    await retryTransient(() => runCmd(this.sandboxes, sandboxId, `chmod 400 ${STATE_DIR}/work_secret`), `sandbox=${sandboxId} exec`);
  }

  /** Create and prepare a sandbox, start the worker, and return the sandbox ID. */
  async start(workId: string, sessionId: string, secret: string | null, environmentKey?: string,
    startSnapshotId?: string): Promise<string> {
    if (!secret && !environmentKey) throw new LaunchError("work item has no secret and FORWARD_ENVIRONMENT_KEY is off");
    const sandbox = await this.createSandbox(this.sandboxEnv(workId, sessionId, environmentKey), sessionId, startSnapshotId);
    log.info(`session=${sessionId} sandbox=${sandbox.id} created`);
    try {
      await this.waitRunning(sandbox.id);
      // The first requests to a new sandbox can fail briefly, so retry them.
      for (const [local, remote] of [[BOOTSTRAP_LOCAL, BOOTSTRAP_REMOTE], [RUNNER_LOCAL, RUNNER_REMOTE]]) {
        const data = await readFile(local);
        await retryTransient(() => this.sandboxes.upload(sandbox.id, remote, data), `sandbox=${sandbox.id} upload`);
      }
      if (secret) await this.uploadSecret(sandbox.id, secret);
      const [code, out] = await retryTransient(() => runCmd(this.sandboxes, sandbox.id, START_CMD), `sandbox=${sandbox.id} exec`);
      if (code !== 0 || !out.includes("started")) {
        throw new LaunchError(`start command failed exit=${code} output=${out.trim().slice(0, 500)}`);
      }
    } catch (e) {
      await this.terminate(sandbox.id);
      throw e;
    }
    log.info(`session=${sessionId} sandbox=${sandbox.id} worker started`);
    return sandbox.id;
  }

  /** Stream the worker's log until it exits. Reconnects from the last line seen if the stream drops. */
  async follow(sandboxId: string, sessionId: string, signal?: AbortSignal): Promise<number | null> {
    let offset = 0;
    let attempt = 0;
    while (!signal?.aborted) {
      let buf = "";
      try {
        const events = await this.sandboxes.exec(sandboxId, `OFFSET=${offset}; ${FOLLOW_CMD}`, undefined, signal);
        for await (const event of events) {
          if (event.type !== "output") continue;
          buf += event.data;
          let nl: number;
          while ((nl = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, nl);
            buf = buf.slice(nl + 1);
            if (line.startsWith(EXIT_MARKER)) return parseCode(line.split(/\s+/).pop() ?? "");
            if (!line) continue;
            offset += 1;
            log.info(`session=${sessionId} sandbox=${sandboxId} | ${line}`);
          }
        }
        attempt = 0;
      } catch (e) {
        if (signal?.aborted) break;
        if (e instanceof ClientError && e.statusCode === 404) {
          log.warn(`session=${sessionId} sandbox=${sandboxId} disappeared (timeout reached?)`);
          return null;
        }
        attempt += 1;
        if (attempt > 20) {
          log.error(`session=${sessionId} sandbox=${sandboxId} giving up following log: ${(e as Error).message}`);
          return null;
        }
        log.warn(`session=${sessionId} sandbox=${sandboxId} log stream dropped (${(e as Error).message}); reconnecting`);
        await sleep(Math.min(2 ** attempt, 30) * 1000);
      }
    }
    throw new Error("aborted");
  }

  async terminate(sandboxId: string) {
    if (this.s.keepSandboxes) {
      log.info(`sandbox=${sandboxId} kept for debugging (KEEP_SANDBOXES=true)`);
      return;
    }
    try {
      await this.sandboxes.terminate(sandboxId);
      log.info(`sandbox=${sandboxId} terminated`);
    } catch (e) {
      log.warn(`sandbox=${sandboxId} terminate failed: ${(e as Error).message}`);
    }
  }

  async runSession(workId: string, sessionId: string, secret: string | null, environmentKey?: string,
    signal?: AbortSignal, startSnapshotId?: string) {
    const sandboxId = await this.start(workId, sessionId, secret, environmentKey, startSnapshotId);
    return this.watch(sandboxId, sessionId, signal);
  }

  /**
   * Follow a running worker to the end, then save and terminate its sandbox.
   * If the orchestrator is shutting down (signal aborted), the sandbox is left running
   * so the session isn't interrupted. The next orchestrator adopts it with discover().
   */
  async watch(sandboxId: string, sessionId: string, signal?: AbortSignal): Promise<SessionResult> {
    let code: number | null;
    try {
      code = await this.follow(sandboxId, sessionId, signal);
    } catch (e) {
      if (signal?.aborted) log.info(`session=${sessionId} sandbox=${sandboxId} left running for the next orchestrator`);
      throw e;
    }
    log.info(`session=${sessionId} sandbox=${sandboxId} worker exited code=${code}`);
    if (code === 0 && this.s.sessionPersistence && !this.s.keepSandboxes) await this.saveSession(sandboxId, sessionId);
    await this.terminate(sandboxId);
    return { sandboxId, exitCode: code };
  }

  /**
   * Find sandboxes from an earlier orchestrator run. Returns running workers to adopt
   * and cleans up ones whose worker already exited. Each running sandbox gets one
   * read-only check for our state directory; sandboxes without it are left alone.
   */
  async discover(): Promise<Discovered[]> {
    const probe = `if [ -d ${STATE_DIR} ]; then ` +
      `echo "ours $ANTHROPIC_WORK_ID $ANTHROPIC_SESSION_ID $(cat ${STATE_DIR}/exit_code 2>/dev/null)"; ` +
      `else echo foreign; fi`;
    const found: Discovered[] = [];
    let cursor: string | undefined;
    while (true) {
      const page = await this.sandboxes.list({ status: ["running"], cursor, limit: 100 });
      for (const { sandbox } of page) {
        let out: string;
        try {
          [, out] = await runCmd(this.sandboxes, sandbox.id, probe);
        } catch (e) {
          log.warn(`sandbox=${sandbox.id} probe failed: ${(e as Error).message}`);
          continue;
        }
        const [tag, workId, sessionId, exit] = out.trim().split(/\s+/);
        if (tag !== "ours" || !sessionId) continue;
        if (exit !== undefined) {
          log.info(`sandbox=${sandbox.id} session=${sessionId} already finished (code=${exit}); cleaning up`);
          if (exit === "0" && this.s.sessionPersistence && !this.s.keepSandboxes) await this.saveSession(sandbox.id, sessionId);
          await this.terminate(sandbox.id);
          found.push({ sandboxId: sandbox.id, workId, sessionId, exitCode: parseCode(exit), running: false });
        } else {
          found.push({ sandboxId: sandbox.id, workId, sessionId, exitCode: null, running: true });
        }
      }
      if (page.length < 100) return found;
      cursor = page[page.length - 1].cursor;
    }
  }
}
