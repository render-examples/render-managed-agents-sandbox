"""Orchestrator: claim Managed Agents work and give each session its own Render sandbox.

Run it anywhere with outbound HTTPS. On Render, deploy it as a background worker
(see render.yaml). It holds the environment key and the Render API key; the
sandboxes it creates hold neither.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from anthropic import AsyncAnthropic, AuthenticationError, PermissionDeniedError
from render import RenderAsync

from .config import Settings
from .launcher import LaunchError, RenderSandboxLauncher

log = logging.getLogger("render-worker")


class Orchestrator:
    def __init__(self, settings: Settings, anthropic: AsyncAnthropic, sandboxes):
        self.s = settings
        self.anthropic = anthropic
        self.launcher = RenderSandboxLauncher(sandboxes, settings)
        self.slots = asyncio.Semaphore(settings.max_concurrent_sessions)
        self.tasks: set[asyncio.Task] = set()

    async def force_stop(self, work_id: str, reason: str) -> None:
        """Release a work item we could not serve, so the session doesn't hang on us."""
        log.error("work=%s force-stopping: %s", work_id, reason)
        with contextlib.suppress(Exception):
            await self.anthropic.beta.environments.work.stop(
                work_id, environment_id=self.s.environment_id, force=True)

    async def adopt(self, sandbox_id: str, work_id: str, session_id: str) -> None:
        try:
            result = await self.launcher.watch(sandbox_id, session_id)
            if result.exit_code != 0:
                await self.force_stop(work_id, f"adopted worker exited with code {result.exit_code}")
        finally:
            self.slots.release()

    async def recover(self) -> None:
        """Pick up sandboxes left by a previous run (for example, after a redeploy)."""
        for item in await self.launcher.discover():
            if item.get("running"):
                await self.slots.acquire()
                log.info("adopting sandbox=%s session=%s", item["sandbox_id"], item["session_id"])
                self._spawn(self.adopt(item["sandbox_id"], item["work_id"], item["session_id"]),
                            f"adopt-{item['session_id']}")
            elif item["exit_code"] not in (0, None):
                await self.force_stop(item["work_id"], f"worker had exited with code {item['exit_code']}")

    async def start_snapshot(self, session_id: str) -> str | None:
        """The snapshot a session asks to start from, set as `render_snapshot_id` in its metadata.

        This is how you stage files for a session: Anthropic doesn't mount files into
        self-hosted sandboxes, so the session points at a Render snapshot that holds them.
        """
        try:
            session = await self.anthropic.beta.sessions.retrieve(session_id)
        except Exception as e:
            log.warning("session=%s could not read metadata: %s", session_id, e)
            return None
        return (session.metadata or {}).get("render_snapshot_id") or None

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def handle(self, work) -> None:
        work_id, session_id, secret = work.id, work.data.id, work.secret
        try:
            forward_key = self.s.environment_key if self.s.forward_environment_key else None
            if not secret and not forward_key:
                await self.force_stop(work_id, "work item carried no secret "
                                      "(set FORWARD_ENVIRONMENT_KEY=true to give sandboxes the environment key)")
                return
            try:
                result = await self.launcher.run_session(work_id, session_id, secret, forward_key,
                                                         await self.start_snapshot(session_id))
            except LaunchError as e:
                await self.force_stop(work_id, f"sandbox launch failed: {e}")
                return
            if result.exit_code != 0:
                # The in-sandbox worker normally stops its own work item. If it crashed
                # or the sandbox timed out, make sure the item doesn't linger.
                await self.force_stop(work_id, f"worker exited with code {result.exit_code}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("work=%s session=%s unexpected error", work_id, session_id)
            await self.force_stop(work_id, f"unexpected error: {e}")
        finally:
            self.slots.release()

    async def run(self) -> None:
        await self.recover()
        log.info("polling environment=%s max_concurrent=%d snapshot=%s",
                 self.s.environment_id, self.s.max_concurrent_sessions, self.s.snapshot_name)
        work_items = self.anthropic.beta.environments.work.poller(
            environment_id=self.s.environment_id,
            environment_key=self.s.environment_key,
            auto_stop=False,  # the worker inside each sandbox owns the stop call
        ).__aiter__()
        while True:
            # Take a slot *before* claiming, so a claimed item never waits on its lease.
            await self.slots.acquire()
            try:
                work = await work_items.__anext__()
            except BaseException:
                self.slots.release()
                raise
            log.info("claimed work=%s session=%s", work.id, work.data.id)
            self._spawn(self.handle(work), f"session-{work.data.id}")

    async def shutdown(self) -> None:
        # Sandboxes keep running on their own and stop the work item themselves.
        # We only stop following them; each is still bounded by SANDBOX_TIMEOUT_SECONDS.
        if self.tasks:
            log.info("shutting down; %d session(s) keep running in their sandboxes", len(self.tasks))
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def amain() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not os.environ.get("LOG_HTTP"):  # LOG_HTTP=1 keeps request logs, e.g. to count Render API calls
        for noisy in ("httpx", "httpx2"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = Settings.from_env()
    # Polls block for at most a second, so a short timeout keeps one stalled connection from
    # freezing the queue for the SDK's default 10 minutes. The poller inherits it.
    async with AsyncAnthropic(auth_token=settings.environment_key, base_url=settings.anthropic_base_url,
                              timeout=60.0) as anthropic:
        orch = Orchestrator(settings, anthropic, RenderAsync().experimental.sandboxes)
        loop_task = asyncio.create_task(orch.run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, loop_task.cancel)
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        except (AuthenticationError, PermissionDeniedError) as e:
            log.error("Anthropic rejected the environment key (%s). Check ANTHROPIC_ENVIRONMENT_KEY "
                      "and ANTHROPIC_ENVIRONMENT_ID, or generate a new key in the Console.", e.status_code)
            raise SystemExit(1)
        finally:
            await orch.shutdown()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
