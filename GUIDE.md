# Claude Managed Agents on Render Sandboxes: reference

This is the reference for the orchestrator in this repository. For a step-by-step tutorial that
deploys it, runs a task, and verifies the result, see
[Run Claude Managed Agents in Render Sandboxes](https://render.com/docs/sandboxes-claude-managed-agents).

The orchestrator is a Render background worker. It claims sessions from a Claude Managed Agents
self-hosted environment, starts one Render sandbox per session, and runs a worker inside it.
The worker is a short Python program, `sandbox/runner.py`, built on Anthropic's SDK. The orchestrator comes in TypeScript (`orchestrator-ts/`) and Python (`orchestrator/`). Both versions
behave the same, read the same settings, and write the same log lines.

> Anthropic's self-hosted sandboxes for Managed Agents are in beta, and Render Sandboxes are in
> early access. Details might change.

## Stage files for a session

Anthropic doesn't mount files or GitHub repositories into self-hosted sandboxes. Its
[documented approach](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-workers#stage-files-for-a-session)
is to put a reference in the session's `metadata` and have your own code stage the files.

This orchestrator reads `render_snapshot_id` from the session's metadata. When the session has no
saved snapshot of its own yet, the orchestrator starts its sandbox from that snapshot. To stage files:

1. Create a sandbox, copy your files into `/workspace`, and snapshot it.
2. Create the session with `metadata={"render_snapshot_id": "<snapshot ID>"}`.

Stage the files before you create the session. Anthropic queues a session as soon as it's created,
so the orchestrator can claim it when polling for work. `starter/stage_task` and `starter/create_session`
show both steps in Python and TypeScript.

The orchestrator picks the first snapshot that exists, in this order:

1. The session's own saved snapshot, `claude-session-<session ID>`.
2. The snapshot in the session's `render_snapshot_id` metadata.
3. The prepared snapshot, `SANDBOX_SNAPSHOT_NAME` (`claude-worker` in the Blueprint).
4. The base image.

## How the orchestrator works

The TypeScript version is in `orchestrator-ts/src` and the Python version is in `orchestrator/`. They work the same way, use the same settings, and write the same logs. Both use Render's sandbox SDK ([TypeScript](https://render.com/docs/sandboxes-sdk-typescript), [Python](https://render.com/docs/sandboxes-sdk-python)) and Anthropic's SDK.

The main loop uses the Anthropic SDK's work poller. It waits for a free concurrency slot before claiming each item, so it never claims a session it can't start right away. It also turns off automatic stopping in the poller, because the worker inside the sandbox is responsible for stopping the work item. Simplified:

**TypeScript**

```typescript
const workItems = this.anthropic.beta.environments.work.poller({
  environmentId: this.s.environmentId,
  environmentKey: this.s.environmentKey,
  autoStop: false, // the worker inside each sandbox owns the stop call
})[Symbol.asyncIterator]();
while (true) {
  await this.slots.acquire();
  const { value: work } = await workItems.next();
  this.spawn(this.handle(work));
}
```

**Python**

```python
work_items = self.anthropic.beta.environments.work.poller(
    environment_id=self.s.environment_id,
    environment_key=self.s.environment_key,
    auto_stop=False,  # the worker inside each sandbox owns the stop call
).__aiter__()
while True:
    await self.slots.acquire()
    work = await work_items.__anext__()
    self._spawn(self.handle(work), f"session-{work.data.id}")
```

For each item, the launcher creates the sandbox, waits until it's running, uploads the startup script and the session secret, and starts the worker in the background:

**TypeScript**

```typescript
const sandbox = await this.createSandbox(this.sandboxEnv(workId, sessionId, environmentKey), sessionId);
await this.waitRunning(sandbox.id);
await this.sandboxes.upload(sandbox.id, BOOTSTRAP_REMOTE, await readFile(BOOTSTRAP_LOCAL));
await this.uploadSecret(sandbox.id, secret);
const [code, out] = await runCmd(this.sandboxes, sandbox.id, START_CMD);
```

**Python**

```python
sandbox = await self.create_sandbox(self.sandbox_env(work_id, session_id, environment_key), session_id)
await self.wait_running(sandbox.id)
await self.sandboxes.copy_to(sandbox.id, str(BOOTSTRAP_LOCAL), BOOTSTRAP_REMOTE)
await self.upload_secret(sandbox.id, secret)
code, out = await run_cmd(self.sandboxes, sandbox.id, START_CMD)
```

The worker runs detached from the command that started it, so that command returns right away. Render runs each command in its own `bash -c` process and keeps it running if the client disconnects, but a background process that inherits the command's output keeps the request open until it exits, which is why the worker's output goes to a log file. An orchestrator redeploy doesn't interrupt the session. The orchestrator follows the worker's log over a single long-lived stream, and when the worker exits, it saves the session's files and terminates the sandbox. After a redeploy, the new orchestrator finds sandboxes that are still running, follows them to completion, and cleans up any whose worker already finished.

### Inside each sandbox

`sandbox/bootstrap.sh` makes sure Anthropic's SDK is installed (the prepared snapshot already has it in `/opt/claude-runner`; otherwise it installs it in about six seconds), then starts `sandbox/runner.py`. The runner hands the session to the SDK's `EnvironmentWorker`, which runs Claude's built-in tools (bash, read, write, edit, glob, and grep) in `/workspace` and posts the results back. It authenticates with the session secret file, so the long-lived environment key never enters the sandbox:

```python
worker = client.beta.environments.work.worker(workdir="/workspace", max_idle=max_idle)
await worker.handle_item(
    work_id=os.environ["ANTHROPIC_WORK_ID"],
    environment_id=os.environ["ANTHROPIC_ENVIRONMENT_ID"],
    session_id=os.environ["ANTHROPIC_SESSION_ID"],
    work_secret=SECRET_FILE.read_text().strip(),
)
```

Anthropic also ships the `ant` CLI worker for this role. This setup uses the SDK worker because its bash tool doesn't need a pseudo-terminal, which Render sandboxes don't mount, and because it can serve custom tools.

## Keep files between messages

By default, a session's files survive the gaps between messages. When a session goes idle, the orchestrator removes the session secret and the worker's state from the sandbox, saves the filesystem to a snapshot named `claude-session-<session ID>`, and deletes the sandbox. When the session's next message arrives, the new sandbox starts from that snapshot. If the message arrives while the save is still running, the orchestrator waits for it to finish first.

The orchestrator waits for each snapshot to become available before stopping the sandbox, then removes older session snapshots. Snapshots expire after three days by default; check `expiresAt` for the actual expiry. A session with no remaining snapshot starts fresh. Set `SESSION_PERSISTENCE=none` to delete sandboxes without saving them.

A filesystem snapshot keeps files and installed packages but not running processes. If Claude starts a dev server or a long job and you need it still running on the next message, set `SESSION_PERSISTENCE=runtime`. That saves memory too, so processes pick up where they left off. Two things to know about it:

- Only processes started with `setsid` survive. When the worker exits, it stops every process in its own group, so a plain `nohup command &` is gone before the snapshot is taken. Tell your agent to start background work as `setsid nohup command > log 2>&1 &`.
- A runtime snapshot restores only onto the same sandbox plan. The orchestrator handles this with `SANDBOX_PLAN`, so change that setting only when no sessions are waiting to resume.

The Render Sandboxes API has no suspend or resume endpoint, so a snapshot is the only way to free a sandbox and come back to it later. Here's how the options compare, with typical timings:

| Option | Time to save | Time to resume | What carries over |
|---|---|---|---|
| Keep the sandbox running (`KEEP_SANDBOXES=true`) | none | none | Everything, but you pay for the sandbox while it's idle, and it ends at `SANDBOX_TIMEOUT_SECONDS` (at most 24 hours) |
| `snapshot` (default) | about 16 seconds | 2 to 4 seconds | Files and installed packages |
| `runtime` | about 38 seconds | about 1 second | Files, packages, memory, and processes started with `setsid` |
| `none` | none | none | Nothing |

## Use memory stores

Memory stores work as they do on Anthropic's cloud. Attach a store in the session's `resources` when you create it, and the worker in the sandbox mounts it under `/mnt/memory/`, syncs changes back to Anthropic while the session runs, and runs a final sync before the sandbox is deleted:

**TypeScript**

```typescript
const session = await client.beta.sessions.create({
  agent: "agent_...",
  environment_id: "env_...",
  resources: [{ type: "memory_store", memory_store_id: "memstore_...", access: "read_write" }],
});
```

**Python**

```python
session = client.beta.sessions.create(
    agent="agent_...",
    environment_id="env_...",
    resources=[{"type": "memory_store", "memory_store_id": "memstore_...", "access": "read_write"}],
)
```

## Control network access

By default, the orchestrator creates sandboxes with an allow-list network policy. Sandboxes can reach `api.anthropic.com`, which the worker needs, and PyPI (`pypi.org` and `files.pythonhosted.org`), which serves the SDK when no prepared snapshot is available. Requests to any other domain are blocked, and so is other outbound TCP traffic.

If your agent's tasks need more, such as a package registry, add those domains:

```bash
SANDBOX_NETWORK_POLICY=allow-list
SANDBOX_ALLOWED_DOMAINS=api.anthropic.com,pypi.org,files.pythonhosted.org,registry.npmjs.org
```

Domain matching is exact, and a leading `*.` matches subdomains. Only HTTPS traffic can reach allowed domains. Set `SANDBOX_NETWORK_POLICY=allow-all` to lift the restriction. Avoid `deny-all`: it also blocks Anthropic's API, so the worker can't run.

## Configuration

The orchestrator reads its settings from environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `SANDBOX_TIMEOUT_SECONDS` | `3600` | Maximum lifetime of one session's sandbox. Render allows up to 86400. |
| `MAX_CONCURRENT_SESSIONS` | `20` | Sandboxes running at once. Early access allows 100 per workspace. |
| `WORKER_MAX_IDLE` | `60s` | How long a session can stay idle before its worker exits and the sandbox is saved and deleted. |
| `SANDBOX_SNAPSHOT_NAME` | `claude-worker` in the Blueprint, unset otherwise | Snapshot to start sandboxes from. Falls back to the base image if missing. |
| `SANDBOX_NETWORK_POLICY` | `allow-list` | `allow-list`, `allow-all`, or `deny-all`. |
| `SANDBOX_ALLOWED_DOMAINS` | Anthropic and GitHub | Domains sandboxes can reach under `allow-list`. |
| `ANTHROPIC_SDK_VERSION` | `1.12.1` | Version of Anthropic's Python SDK the worker uses inside sandboxes. |
| `FORWARD_ENVIRONMENT_KEY` | `false` | Also give sandboxes the environment key. See [Security](#security). |
| `SESSION_PERSISTENCE` | `snapshot` | Save each idle session's files and restore them on its next message. `runtime` also saves memory and running processes. `none` turns it off. |
| `SANDBOX_PLAN` | `starter` | Sandbox plan. Runtime snapshots restore only onto the plan they were taken on. |
| `KEEP_SANDBOXES` | `false` | Leave sandboxes running after a session for debugging. |

To add tools to every sandbox, set `SNAPSHOT_EXTRA_SETUP` on the snapshot cron job to a shell snippet, for example `apt-get update && apt-get install -y ripgrep`. Render's sandbox API doesn't take a custom container image, so snapshots are how you prepare the environment. Snapshots expire after three days by default, which is why the job runs daily.

## Security

Each sandbox is an isolated environment, kept apart from your other Render services, and Claude's commands run as root inside it. The orchestrator never passes your environment key, Claude API key, or Render API key into a sandbox, as environment variables or as files. Keep in mind what code in the sandbox can reach:

- **The session secret.** The worker needs it, so code in the sandbox can read it. It's removed before the sandbox is saved to a snapshot. The tokens inside it can read and write only their own session. Anthropic's API refuses them for other sessions, agents, environments, and the work queue. They can, however, call the Claude API, and they stay valid for about an hour, even after the session goes idle. Code running in the sandbox could use them to make model calls billed to your organization until they expire. Network rules don't help here, since the worker needs `api.anthropic.com` itself, so treat this as the main thing an untrusted command can take.
- **The environment key, only if you forward it.** Leave `FORWARD_ENVIRONMENT_KEY` off. When it's on, any command Claude runs can read a key that claims every session in the environment. Turn it on only if the worker reports that the session secret carries no sessions token, and understand the trade-off first.
- **Anything you add.** Don't stage unrelated secrets in sandboxes, and build snapshots before any secret is present, since a filesystem snapshot captures whatever is on disk.

The orchestrator's Render API key acts as the user who created it, in every workspace that user belongs to. Choosing a workspace for the sandboxes doesn't narrow it. Keep the key only on the orchestrator, and consider creating it from a Render user who belongs only to the workspace you use for agent sandboxes.

## Limitations

- **Processes carry over only with runtime snapshots.** With the default setting, background processes stop when a session goes idle. Saved snapshots of either kind expire after three days by default.
- **File tools stay in `/workspace`.** Claude's read, write, edit, glob, and grep tools only work inside the worker's directory, which is Anthropic's built-in guardrail. Bash can still reach the rest of the sandbox.
- **Built-in tools only, as shipped.** The runner serves Claude's built-in tools. A session that calls a custom tool pauses until something posts the result. To serve custom tools, pass a `tools` list to the SDK worker in `sandbox/runner.py`.
- **Polling, not webhooks.** Anthropic can send a `session.status_run_started` webhook when work is queued, but this orchestrator polls the queue instead, which suits an always-on Render worker. To wake on webhooks, register an endpoint in the Claude Console under **Manage > Webhooks** (there's no API for it) and run the orchestrator as a Render web service that polls after each verified webhook.
- **No mounted resources.** Self-hosted environments don't mount files or GitHub repositories for you. Pass a reference in the session's `metadata` and fetch it in the sandbox, as described in Anthropic's [guide to staging files](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-workers#stage-files-for-a-session).
- **Early access limits.** Each sandbox has 2 CPU, 4 GB of memory, and 10 GB of disk. A workspace can run 100 sandboxes at once and make 100 API requests per minute, sandboxes run in Oregon only, and each lives at most 24 hours. Each session uses about nine Render API calls from start to cleanup, so the orchestrator can start roughly ten sessions a minute before it reaches the request limit.

## Clean up

To remove everything this guide creates:

1. In the Render Dashboard, delete the **claude-sandbox-orchestrator** worker, the **claude-sandbox-snapshot** cron job, and the **claude-sandbox** environment group. Delete the worker first so it stops claiming sessions.
2. Delete leftover snapshots. List them with `render ea sandboxes snapshots list`, then delete the `claude-worker`, `claude-session-...`, and `claude-task-...` ones with `render ea sandboxes snapshots delete snp-... --confirm`. Otherwise they expire at their recorded `expiresAt`, three days after creation by default.
3. In the Claude Console, open your environment and delete its keys under **Environment keys**.
4. Archive the environment:

**TypeScript**

```typescript
await new Anthropic().beta.environments.archive("env_...");
```

**Python**

```python
anthropic.Anthropic().beta.environments.archive("env_...")
```

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Logs show `Anthropic rejected the environment key (401)` | The key or environment ID is wrong. | Check both values, or generate a new key in the Console. |
| Sessions stay queued | The orchestrator isn't polling, or every concurrency slot is busy. | Run `ant beta:environments:work stats --environment-id env_...` with your Claude API key. `workers_polling` should be at least 1. Then check the worker's logs for `polling environment=...`, or raise `MAX_CONCURRENT_SESSIONS`. |
| `the work secret carries no sessions token and no environment key is set` | The session secret can't authenticate on its own. Anthropic's docs tie this to memory stores for self-hosted sandboxes not being enabled for your organization. | Contact Anthropic support. As a stopgap, set `FORWARD_ENVIRONMENT_KEY=true`. |
| `snapshot 'claude-worker' unavailable (...); using base image` | The snapshot expired or was never built. | Trigger the snapshot cron job. Sessions keep working in the meantime. |
| The worker can't reach Anthropic | The allow-list is missing `api.anthropic.com`. | Add it to `SANDBOX_ALLOWED_DOMAINS`. |
| `RateLimitError` from Render | Too many sandboxes or API calls. | Lower `MAX_CONCURRENT_SESSIONS`, or ask Render to raise your limits. |
| A session stops after exactly one hour | It reached `SANDBOX_TIMEOUT_SECONDS`. | Raise the value, up to 86400. |

## Related

- [Self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes) and [deploying workers](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-workers) in Anthropic's docs
- [Security model for self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-security)
- [Render Sandboxes](https://render.com/docs/sandboxes), the [TypeScript SDK](https://render.com/docs/sandboxes-sdk-typescript), the [Python SDK](https://render.com/docs/sandboxes-sdk-python), and the [CLI reference](https://render.com/docs/sandboxes-cli-reference)
