"""Run one Managed Agents session inside one Render sandbox.

Flow for each claimed work item:
  1. Create a fresh sandbox (from a prebuilt snapshot if one exists).
  2. Wait until it is running.
  3. Copy in the bootstrap script and the per-session work secret.
  4. Start the worker (sandbox/runner.py, Anthropic's SDK) detached, so the start command returns at once.
  5. Follow its log until it exits.
  6. Terminate the sandbox.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from render.client.errors import RateLimitError, ServerError
from render.experimental.sandbox import (
    SandboxExecExit,
    SandboxExecOutput,
    SandboxNotFoundError,
    SnapshotNotFoundError,
    SnapshotNotReadyError,
)

from .config import Settings

log = logging.getLogger("render-worker")

STATE_DIR = "/run/claude-worker"
BOOTSTRAP_LOCAL = Path(__file__).resolve().parent.parent / "sandbox" / "bootstrap.sh"
BOOTSTRAP_REMOTE = f"{STATE_DIR}/bootstrap.sh"
RUNNER_LOCAL = BOOTSTRAP_LOCAL.parent / "runner.py"
RUNNER_REMOTE = f"{STATE_DIR}/runner.py"
EXIT_MARKER = "__CLAUDE_WORKER_EXIT__"

# Errors a newly running sandbox can return before its agent accepts requests.
# Render's own SDK tests retry exec on these (404, 429, 503) after creating a sandbox.
TRANSIENT_ERRORS = (SandboxNotFoundError, RateLimitError, ServerError)

# Starts the worker in its own session so it outlives this exec call.
# Its exit code is written to a file when it finishes. The launch is wrapped in its own
# subshell on purpose: written as `chmod && setsid ... & echo`, bash backgrounds the whole
# chain, and that background job keeps exec's output pipe open until the worker exits.
# A marker file makes the command safe to retry: a second run reports "started"
# without launching another worker.
START_CMD = (
    f"if [ -e {STATE_DIR}/launched ]; then echo started; exit 0; fi; "
    f"touch {STATE_DIR}/launched && chmod 700 {BOOTSTRAP_REMOTE} && "
    f"( setsid nohup bash -c '{BOOTSTRAP_REMOTE} > {STATE_DIR}/worker.log 2>&1; "
    f"echo $? > {STATE_DIR}/exit_code' > /dev/null 2>&1 < /dev/null & ) && echo started"
)

# One long-lived request per session: stream the log until the exit file appears.
# This open request also counts as activity to Render. Render's sandbox code can pause
# sandboxes with no API request in flight for 15 minutes (built, not yet switched on),
# and a background process like the worker doesn't count, so keep this request open.
FOLLOW_CMD = (
    f"touch {STATE_DIR}/worker.log; "
    f"tail -n +$((OFFSET+1)) -F {STATE_DIR}/worker.log 2>/dev/null & T=$!; "
    f"while [ ! -f {STATE_DIR}/exit_code ]; do sleep 1; done; sleep 1; kill $T 2>/dev/null; "
    f"echo; echo {EXIT_MARKER} $(cat {STATE_DIR}/exit_code)"
)


class LaunchError(RuntimeError):
    """The sandbox could not be prepared, so the work item never started."""


@dataclass
class SessionResult:
    sandbox_id: str | None
    exit_code: int | None
    started: bool
    log_lines: list[str] = field(default_factory=list)


async def run_cmd(sandboxes, sandbox_id: str, command: str) -> tuple[int | None, str]:
    out: list[str] = []
    code = None
    async for event in sandboxes.exec(sandbox_id, command):
        if isinstance(event, SandboxExecOutput):
            out.append(event.data)
        elif isinstance(event, SandboxExecExit):
            code = event.exit_code
    return code, "".join(out)


async def retry_transient(call, what: str, attempts: int = 20, delay: float = 3.0):
    """Run call() and retry while the sandbox returns transient errors."""
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except TRANSIENT_ERRORS as e:
            if attempt == attempts:
                raise
            log.info("%s not ready (%s); retrying", what, type(e).__name__)
            await asyncio.sleep(delay)


class RenderSandboxLauncher:
    def __init__(self, sandboxes, settings: Settings, owner_id: str | None = None):
        self.sandboxes = sandboxes
        self.s = settings
        self.owner_id = owner_id or os.environ.get("RENDER_WORKSPACE_ID", "")
        self._captures: dict[str, asyncio.Event] = {}

    def sandbox_env(self, work_id: str, session_id: str, environment_key: str | None = None) -> dict[str, str]:
        env = {
            "ANTHROPIC_ENVIRONMENT_ID": self.s.environment_id,
            "ANTHROPIC_SESSION_ID": session_id,
            "ANTHROPIC_WORK_ID": work_id,
            "ANTHROPIC_SDK_VERSION": self.s.sdk_version,
            "WORKER_MAX_IDLE": self.s.worker_max_idle,
        }
        if self.s.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = self.s.anthropic_base_url
        if environment_key:  # only when FORWARD_ENVIRONMENT_KEY=true; see GUIDE.md
            env["ANTHROPIC_ENVIRONMENT_KEY"] = environment_key
        return env

    async def _create_allow_list(self, env: dict[str, str], snapshot_name: str | None,
                                 snapshot_id: str | None = None):
        """The SDK's create() takes only a policy name, so build the request with its generated model."""
        from render.experimental.sandbox.api import SandboxPOST, _to_sandbox, create_sandbox
        from render.public_api.models.sandbox_network_policy import SandboxNetworkPolicy
        from render.public_api.models.sandbox_post_env import SandboxPOSTEnv

        body = SandboxPOST(owner_id=self.owner_id)
        body.timeout_seconds = self.s.sandbox_timeout_seconds
        # Send current rules and the legacy fields while SDK 1.2 and older servers coexist.
        body.network_policy = SandboxNetworkPolicy.from_dict({
            "type": "allow-list",
            "default": "allow-list",
            "rules": [{"domain": domain, "protocol": "https"} for domain in self.s.allowed_domains],
            "allowedDomains": list(self.s.allowed_domains),
        })
        body.env = SandboxPOSTEnv.from_dict(env)
        if snapshot_id:
            body.snapshot_id = snapshot_id
        elif snapshot_name:
            body.snapshot_name = snapshot_name
            if self.s.persistence_kind == "runtime":
                from render.public_api.models.sandbox_plan import SandboxPlan
                body.plan = SandboxPlan(self.s.sandbox_plan)
        resp = await create_sandbox.asyncio_detailed(client=self.sandboxes.api.client, body=body)
        if resp.status_code != 201:
            text = resp.content.decode(errors="replace")[:300]
            if (snapshot_name or snapshot_id) and resp.status_code in (400, 404, 409):
                raise SnapshotNotFoundError(text)
            raise LaunchError(f"create sandbox failed: {resp.status_code} {text}")
        return _to_sandbox(resp.parsed)

    @staticmethod
    def session_snapshot_name(session_id: str) -> str:
        return f"claude-session-{session_id}"

    async def _create_one(self, env: dict[str, str], snapshot_name: str | None,
                          snapshot_id: str | None = None):
        if self.s.network_policy == "allow-list":
            return await self._create_allow_list(env, snapshot_name, snapshot_id)
        kwargs = dict(timeout_seconds=self.s.sandbox_timeout_seconds,
                      network_policy=self.s.network_policy, env=env)
        if snapshot_id:
            kwargs["snapshot_id"] = snapshot_id
        elif snapshot_name:
            kwargs["snapshot_name"] = snapshot_name
            if self.s.persistence_kind == "runtime":
                kwargs["plan"] = self.s.sandbox_plan  # a runtime snapshot restores only onto its own plan
        return await self.sandboxes.create(**kwargs)

    async def create_sandbox(self, env: dict[str, str], session_id: str | None = None,
                             start_snapshot_id: str | None = None):
        """Pick the first snapshot that exists, in this order: the session's own saved snapshot,
        the snapshot named in the session's metadata, the prepared snapshot, then the base image."""
        candidates: list[tuple[str | None, str | None]] = []  # (snapshot name, snapshot ID)
        if session_id and self.s.session_persistence:
            await self._wait_for_capture(session_id)
            candidates.append((self.session_snapshot_name(session_id), None))
        if start_snapshot_id:
            candidates.append((None, start_snapshot_id))
        if self.s.snapshot_name:
            candidates.append((self.s.snapshot_name, None))
        for name, snapshot_id in candidates:
            try:
                sandbox = await self._create_one(env, name, snapshot_id)
                if name != self.s.snapshot_name:
                    log.info("session=%s restored files from snapshot %s", session_id, name or snapshot_id)
                return sandbox
            except (SnapshotNotFoundError, SnapshotNotReadyError) as e:
                if snapshot_id:
                    log.warning("session=%s metadata snapshot %s unavailable (%s)", session_id, snapshot_id, e)
                elif name != self.s.snapshot_name:
                    log.info("session=%s has no saved snapshot yet", session_id)
                if name == self.s.snapshot_name:
                    # Snapshots expire after three days by default. The base image still works; the startup script installs the SDK.
                    log.warning("snapshot %r unavailable (%s); using base image", name, e)
        return await self._create_one(env, None)

    async def _wait_for_capture(self, session_id: str) -> None:
        pending = self._captures.get(session_id)
        if pending:
            log.info("session=%s waiting for its previous sandbox to finish saving", session_id)
            try:
                await asyncio.wait_for(pending.wait(), timeout=300)
            except asyncio.TimeoutError:
                log.warning("session=%s previous save still running; continuing", session_id)

    async def save_session(self, sandbox_id: str, session_id: str) -> None:
        """Snapshot the sandbox's filesystem under the session's name so the next turn can resume it.

        The work secret, worker log, and exit marker are removed first, so no credential
        is captured. Older snapshots for the same session are deleted once the new one is ready.
        """
        name = self.session_snapshot_name(session_id)
        done = asyncio.Event()
        self._captures[session_id] = done
        try:
            await run_cmd(self.sandboxes, sandbox_id, f"rm -rf {STATE_DIR}")
            snap = await self.sandboxes.snapshots.create(sandbox_id, name=name, kind=self.s.persistence_kind)
            deadline = time.monotonic() + 300
            while snap.status == "creating" and time.monotonic() < deadline:
                await asyncio.sleep(2)
                snap = await self.sandboxes.snapshots.from_id(sandbox_group_id=snap.sandbox_group_id,
                                                              snapshot_id=snap.id)
            if snap.status != "available":
                log.warning("session=%s snapshot %s ended %s: %s", session_id, snap.id, snap.status,
                            getattr(snap, "error", ""))
                return
            log.info("session=%s saved files to snapshot %s (%s)", session_id, name, snap.id)
            older = []
            cursor = None
            while True:  # read every page; the list is newest first, 100 at a time
                page = await self.sandboxes.snapshots.list(sandbox_group_id=snap.sandbox_group_id,
                                                           status="available", cursor=cursor, limit=100)
                older += [s for s in page.snapshots if getattr(s, "name", None) == name and s.id != snap.id]
                cursor = page.next_cursor
                if not cursor:
                    break
            for old in older:
                    try:
                        await self.sandboxes.snapshots.delete(sandbox_group_id=snap.sandbox_group_id,
                                                              snapshot_id=old.id)
                    except Exception as e:
                        log.warning("could not delete old snapshot %s: %s", old.id, e)
        except Exception as e:
            log.warning("session=%s could not save files: %s", session_id, e)
        finally:
            done.set()
            self._captures.pop(session_id, None)

    async def wait_running(self, sandbox_id: str) -> None:
        deadline = time.monotonic() + self.s.ready_timeout_seconds
        while time.monotonic() < deadline:
            status = (await self.sandboxes.from_id(sandbox_id)).status
            if status == "running":
                return
            if status in ("errored", "terminated"):
                raise LaunchError(f"sandbox {sandbox_id} is {status}")
            await asyncio.sleep(1)
        raise LaunchError(f"sandbox {sandbox_id} not running after {self.s.ready_timeout_seconds}s")

    async def upload_secret(self, sandbox_id: str, secret: str) -> None:
        fd, path = tempfile.mkstemp(prefix="work-secret-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secret)
            await retry_transient(lambda: self.sandboxes.copy_to(sandbox_id, path, f"{STATE_DIR}/work_secret"),
                                  f"sandbox={sandbox_id} upload")
        finally:
            os.unlink(path)
        await retry_transient(lambda: run_cmd(self.sandboxes, sandbox_id, f"chmod 400 {STATE_DIR}/work_secret"),
                              f"sandbox={sandbox_id} exec")

    async def start(self, work_id: str, session_id: str, secret: str | None,
                    environment_key: str | None = None, start_snapshot_id: str | None = None) -> str:
        """Create and prepare a sandbox, start the worker, and return the sandbox ID."""
        if not secret and not environment_key:
            raise LaunchError("work item has no secret and FORWARD_ENVIRONMENT_KEY is off")
        sandbox = await self.create_sandbox(self.sandbox_env(work_id, session_id, environment_key), session_id,
                                            start_snapshot_id)
        log.info("session=%s sandbox=%s created", session_id, sandbox.id)
        try:
            await self.wait_running(sandbox.id)
            # The first requests to a new sandbox can fail briefly, so retry them.
            for local, remote in ((BOOTSTRAP_LOCAL, BOOTSTRAP_REMOTE), (RUNNER_LOCAL, RUNNER_REMOTE)):
                await retry_transient(lambda: self.sandboxes.copy_to(sandbox.id, str(local), remote),
                                      f"sandbox={sandbox.id} upload")
            if secret:
                await self.upload_secret(sandbox.id, secret)
            code, out = await retry_transient(lambda: run_cmd(self.sandboxes, sandbox.id, START_CMD),
                                              f"sandbox={sandbox.id} exec")
            if code != 0 or "started" not in out:
                raise LaunchError(f"start command failed exit={code} output={out.strip()[:500]}")
        except BaseException:
            await self.terminate(sandbox.id)
            raise
        log.info("session=%s sandbox=%s worker started", session_id, sandbox.id)
        return sandbox.id

    async def follow(self, sandbox_id: str, session_id: str, keep_lines: int = 200) -> tuple[int | None, list[str]]:
        """Stream the worker's log until it exits. Reconnects if the stream drops."""
        offset = 0
        lines: list[str] = []
        buf = ""
        attempt = 0
        while True:
            try:
                async for event in self.sandboxes.exec(sandbox_id, f"OFFSET={offset}; " + FOLLOW_CMD):
                    if not isinstance(event, SandboxExecOutput):
                        continue
                    buf += event.data
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if line.startswith(EXIT_MARKER):
                            code = line.split()[-1]
                            return (int(code) if code.lstrip("-").isdigit() else None), lines
                        if not line:
                            continue
                        offset += 1
                        lines = (lines + [line])[-keep_lines:]
                        log.info("session=%s sandbox=%s | %s", session_id, sandbox_id, line)
                attempt = 0
            except SandboxNotFoundError:
                log.warning("session=%s sandbox=%s disappeared (timeout reached?)", session_id, sandbox_id)
                return None, lines
            except Exception as e:  # stream dropped: reconnect from the last line we saw
                attempt += 1
                if attempt > 20:
                    log.error("session=%s sandbox=%s giving up following log: %s", session_id, sandbox_id, e)
                    return None, lines
                log.warning("session=%s sandbox=%s log stream dropped (%s); reconnecting", session_id, sandbox_id, e)
                await asyncio.sleep(min(2 ** attempt, 30))

    async def terminate(self, sandbox_id: str) -> None:
        if self.s.keep_sandboxes:
            log.info("sandbox=%s kept for debugging (KEEP_SANDBOXES=true)", sandbox_id)
            return
        try:
            await self.sandboxes.terminate(sandbox_id)
            log.info("sandbox=%s terminated", sandbox_id)
        except Exception as e:
            log.warning("sandbox=%s terminate failed: %s", sandbox_id, e)

    async def run_session(self, work_id: str, session_id: str, secret: str | None,
                          environment_key: str | None = None,
                          start_snapshot_id: str | None = None) -> SessionResult:
        sandbox_id = await self.start(work_id, session_id, secret, environment_key, start_snapshot_id)
        return await self.watch(sandbox_id, session_id)

    async def watch(self, sandbox_id: str, session_id: str) -> SessionResult:
        """Follow a running worker to the end, then terminate its sandbox.

        If this task is cancelled (the orchestrator is shutting down or redeploying),
        the sandbox is left running so the session isn't interrupted. The next
        orchestrator adopts it with `discover()`.
        """
        try:
            code, lines = await self.follow(sandbox_id, session_id)
        except asyncio.CancelledError:
            log.info("session=%s sandbox=%s left running for the next orchestrator", session_id, sandbox_id)
            raise
        log.info("session=%s sandbox=%s worker exited code=%s", session_id, sandbox_id, code)
        if code == 0 and self.s.session_persistence and not self.s.keep_sandboxes:
            await self.save_session(sandbox_id, session_id)
        await self.terminate(sandbox_id)
        return SessionResult(sandbox_id, code, True, lines)

    async def discover(self) -> list[dict]:
        """Find sandboxes from an earlier orchestrator run.

        Returns running workers to adopt and terminates ones whose worker already exited.
        Each running sandbox gets one read-only check for our state directory;
        sandboxes without it are left alone.
        """
        probe = (f"if [ -d {STATE_DIR} ]; then "
                 f"echo \"ours $ANTHROPIC_WORK_ID $ANTHROPIC_SESSION_ID $(cat {STATE_DIR}/exit_code 2>/dev/null)\"; "
                 f"else echo foreign; fi")
        found = []
        cursor = None
        while True:
            page = await self.sandboxes.list(status="running", cursor=cursor, limit=100)
            for sb in page.sandboxes:
                try:
                    _, out = await run_cmd(self.sandboxes, sb.id, probe)
                except Exception as e:
                    log.warning("sandbox=%s probe failed: %s", sb.id, e)
                    continue
                parts = out.split()
                if len(parts) < 3 or parts[0] != "ours":
                    continue
                work_id, session_id = parts[1], parts[2]
                exit_code = parts[3] if len(parts) > 3 else None
                if exit_code is not None:
                    log.info("sandbox=%s session=%s already finished (code=%s); cleaning up",
                             sb.id, session_id, exit_code)
                    if exit_code == "0" and self.s.session_persistence and not self.s.keep_sandboxes:
                        await self.save_session(sb.id, session_id)
                    await self.terminate(sb.id)
                    found.append({"sandbox_id": sb.id, "work_id": work_id, "session_id": session_id,
                                  "exit_code": int(exit_code) if exit_code.lstrip("-").isdigit() else None})
                else:
                    found.append({"sandbox_id": sb.id, "work_id": work_id, "session_id": session_id,
                                  "exit_code": None, "running": True})
            cursor = page.next_cursor
            if not cursor:
                return found
