// The orchestrator's main loop. Mirrors the Orchestrator class in orchestrator/main.py.
import type Anthropic from "@anthropic-ai/sdk";
import type { BetaSelfHostedWork } from "@anthropic-ai/sdk/resources/beta/environments/work";
import type { Render } from "@renderinc/sdk";
import type { Settings } from "./config.js";
import { LaunchError, RenderSandboxLauncher } from "./launcher.js";
import { log } from "./log.js";

/** A counting semaphore: one slot per concurrent session. */
export class Slots {
  private waiters: (() => void)[] = [];
  constructor(private free: number) {}
  async acquire() {
    if (this.free > 0) {
      this.free -= 1;
      return;
    }
    await new Promise<void>((resolve) => this.waiters.push(resolve));
  }
  get available() {
    return this.free;
  }
  release() {
    const next = this.waiters.shift();
    if (next) next();
    else this.free += 1;
  }
}

export class Orchestrator {
  launcher: Pick<RenderSandboxLauncher, "runSession" | "watch" | "discover">;
  readonly slots: Slots;
  private tasks = new Set<Promise<void>>();
  private stopping = new AbortController();

  constructor(private s: Settings, private anthropic: Anthropic, sandboxes?: Render["experimental"]["sandboxes"]) {
    this.launcher = new RenderSandboxLauncher(sandboxes!, s);
    this.slots = new Slots(s.maxConcurrentSessions);
  }

  /** Release a work item we could not serve, so the session doesn't hang on us. */
  async forceStop(workId: string, reason: string) {
    log.error(`work=${workId} force-stopping: ${reason}`);
    try {
      await this.anthropic.beta.environments.work.stop(workId, { environment_id: this.s.environmentId, force: true });
    } catch {
      // Best effort: the work item may already be stopped.
    }
  }

  /**
   * The snapshot a session asks to start from, set as `render_snapshot_id` in its metadata.
   * This is how you stage files for a session: Anthropic doesn't mount files into
   * self-hosted sandboxes, so the session points at a Render snapshot that holds them.
   */
  async startSnapshot(sessionId: string): Promise<string | undefined> {
    try {
      const session = await this.anthropic.beta.sessions.retrieve(sessionId);
      return session.metadata?.render_snapshot_id || undefined;
    } catch (e) {
      log.warn(`session=${sessionId} could not read metadata: ${(e as Error).message}`);
      return undefined;
    }
  }

  private spawn(task: Promise<void>) {
    const tracked = task.catch(() => {}).finally(() => this.tasks.delete(tracked));
    this.tasks.add(tracked);
  }

  private async adopt(sandboxId: string, workId: string, sessionId: string) {
    try {
      const result = await this.launcher.watch(sandboxId, sessionId, this.stopping.signal);
      if (result.exitCode !== 0) await this.forceStop(workId, `adopted worker exited with code ${result.exitCode}`);
    } finally {
      this.slots.release();
    }
  }

  /** Pick up sandboxes left by a previous run (for example, after a redeploy). */
  async recover() {
    for (const item of await this.launcher.discover()) {
      if (item.running) {
        await this.slots.acquire();
        log.info(`adopting sandbox=${item.sandboxId} session=${item.sessionId}`);
        this.spawn(this.adopt(item.sandboxId, item.workId, item.sessionId));
      } else if (item.exitCode !== 0 && item.exitCode !== null) {
        await this.forceStop(item.workId, `worker had exited with code ${item.exitCode}`);
      }
    }
  }

  async handle(work: BetaSelfHostedWork) {
    const workId = work.id;
    const sessionId = work.data.id;
    try {
      const forwardKey = this.s.forwardEnvironmentKey ? this.s.environmentKey : undefined;
      if (!work.secret && !forwardKey) {
        await this.forceStop(workId, "work item carried no secret (set FORWARD_ENVIRONMENT_KEY=true to give sandboxes the environment key)");
        return;
      }
      let result;
      try {
        result = await this.launcher.runSession(workId, sessionId, work.secret, forwardKey, this.stopping.signal,
          await this.startSnapshot(sessionId));
      } catch (e) {
        if (this.stopping.signal.aborted) return;
        if (e instanceof LaunchError) {
          await this.forceStop(workId, `sandbox launch failed: ${e.message}`);
          return;
        }
        throw e;
      }
      // The in-sandbox worker normally stops its own work item. If it crashed
      // or the sandbox timed out, make sure the item doesn't linger.
      if (result.exitCode !== 0) await this.forceStop(workId, `worker exited with code ${result.exitCode}`);
    } catch (e) {
      log.error(`work=${workId} session=${sessionId} unexpected error: ${(e as Error).stack ?? e}`);
      await this.forceStop(workId, `unexpected error: ${(e as Error).message}`);
    } finally {
      this.slots.release();
    }
  }

  async run() {
    await this.recover();
    log.info(`polling environment=${this.s.environmentId} max_concurrent=${this.s.maxConcurrentSessions} snapshot=${this.s.snapshotName}`);
    const workItems = this.anthropic.beta.environments.work.poller({
      environmentId: this.s.environmentId,
      environmentKey: this.s.environmentKey,
      autoStop: false, // the worker inside each sandbox owns the stop call
      signal: this.stopping.signal,
    })[Symbol.asyncIterator]();
    while (!this.stopping.signal.aborted) {
      // Take a slot *before* claiming, so a claimed item never waits on its lease.
      await this.slots.acquire();
      const next = await workItems.next().catch((e) => {
        this.slots.release();
        throw e;
      });
      if (next.done) {
        this.slots.release();
        break;
      }
      const work = next.value;
      log.info(`claimed work=${work.id} session=${work.data.id}`);
      this.spawn(this.handle(work));
    }
  }

  async shutdown() {
    // Sandboxes keep running on their own and stop the work item themselves.
    // We only stop following them; each is still bounded by SANDBOX_TIMEOUT_SECONDS.
    if (this.tasks.size) log.info(`shutting down; ${this.tasks.size} session(s) keep running in their sandboxes`);
    this.stopping.abort();
    await Promise.allSettled([...this.tasks]);
  }
}
