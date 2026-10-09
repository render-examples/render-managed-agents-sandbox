"""Live tests against real Render sandboxes, with a fake Anthropic work item.

They prove the Render half end to end: sandbox creation, secret delivery, SDK
install, detached start, log following, exit detection, cleanup, and recovery.
The fake secret makes the worker fail authentication, which is the expected result.

Run: pytest -m live tests/test_render_live.py
"""
import asyncio
import os

import pytest
from render import RenderAsync
from render.experimental.sandbox import SandboxNotFoundError

from orchestrator.config import Settings
from orchestrator.launcher import STATE_DIR, RenderSandboxLauncher, run_cmd

pytestmark = [pytest.mark.live, pytest.mark.skipif(
    not (os.environ.get("RENDER_API_KEY") and os.environ.get("RENDER_WORKSPACE_ID")),
    reason="needs RENDER_API_KEY and RENDER_WORKSPACE_ID")]


def settings(**over) -> Settings:
    base = dict(environment_id="env_fake", environment_key="sk-ant-oat01-NEVER-SENT",
                anthropic_base_url=None, sandbox_timeout_seconds=600, max_concurrent_sessions=2,
                snapshot_name=None, network_policy="allow-all", allowed_domains=(), sdk_version="1.12.1",
                worker_max_idle="10s", keep_sandboxes=False, forward_environment_key=False)
    base.update(over)
    return Settings(**base)


async def gone(sandboxes, sandbox_id: str) -> bool:
    try:
        return (await sandboxes.from_id(sandbox_id)).status == "terminated"
    except SandboxNotFoundError:
        return True


async def test_full_session_lifecycle_with_fake_work():
    sandboxes = RenderAsync().experimental.sandboxes
    launcher = RenderSandboxLauncher(sandboxes, settings())
    result = await asyncio.wait_for(
        launcher.run_session("work_fake123", "sesn_fake123", "fake-work-secret"), timeout=180)

    text = "\n".join(result.log_lines)
    assert result.started
    assert "installing anthropic 1.12.1" in text           # bootstrap ran inside the sandbox
    assert result.exit_code not in (0, None)               # fake secret -> worker refuses
    # the worker read the secret file and rejected it, and no environment key was there to fall back on
    assert "yielded no sessions token and no environment key" in text
    assert await gone(sandboxes, result.sandbox_id)        # sandbox cleaned up


async def test_secret_is_a_file_and_environment_key_never_enters_sandbox():
    sandboxes = RenderAsync().experimental.sandboxes
    launcher = RenderSandboxLauncher(sandboxes, settings(keep_sandboxes=True))
    sandbox_id = await launcher.start("work_fake456", "sesn_fake456", "fake-work-secret")
    try:
        _, out = await run_cmd(sandboxes, sandbox_id,
                               f"stat -c '%a %U' {STATE_DIR}/work_secret; env | grep -c NEVER-SENT; "
                               f"grep -rl NEVER-SENT / --exclude-dir=proc --exclude-dir=sys 2>/dev/null | wc -l")
        mode_owner, env_hits, file_hits = out.split("\n")[:3]
        assert mode_owner == "400 root"
        assert env_hits.strip() == "0"
        assert file_hits.strip() == "0"
    finally:
        await sandboxes.terminate(sandbox_id)


async def test_discover_adopts_running_and_cleans_finished():
    sandboxes = RenderAsync().experimental.sandboxes
    keep = RenderSandboxLauncher(sandboxes, settings(keep_sandboxes=True))
    # A sandbox whose worker already finished, as if the orchestrator restarted mid-session.
    finished = await keep.start("work_done", "sesn_done", "fake-work-secret")
    _, _ = await keep.follow(finished, "sesn_done")
    # A sandbox whose "worker" is still running.
    running = await keep.start("work_live", "sesn_live", "fake-work-secret")
    await run_cmd(sandboxes, running, f"rm -f {STATE_DIR}/exit_code; "
                  f"setsid nohup bash -c 'sleep 30; echo 0 > {STATE_DIR}/exit_code' >/dev/null 2>&1 </dev/null &")
    try:
        found = await RenderSandboxLauncher(sandboxes, settings()).discover()
        by_id = {f["sandbox_id"]: f for f in found}
        assert by_id[finished]["session_id"] == "sesn_done"
        assert by_id[running].get("running") is True
        assert by_id[running]["work_id"] == "work_live"
        assert await gone(sandboxes, finished)
        assert not await gone(sandboxes, running)
    finally:
        for sid in (finished, running):
            await sandboxes.terminate(sid)


@pytest.mark.skipif(not os.environ.get("SANDBOX_SNAPSHOT_NAME"), reason="build a snapshot first")
async def test_snapshot_skips_install_and_missing_snapshot_falls_back():
    sandboxes = RenderAsync().experimental.sandboxes
    snap = RenderSandboxLauncher(sandboxes, settings(snapshot_name=os.environ["SANDBOX_SNAPSHOT_NAME"]))
    r1 = await snap.run_session("work_snap", "sesn_snap", "fake-work-secret")
    assert not any("installing anthropic" in l for l in r1.log_lines)   # SDK came from the snapshot

    missing = RenderSandboxLauncher(sandboxes, settings(snapshot_name="does-not-exist-xyz"))
    r2 = await missing.run_session("work_nosnap", "sesn_nosnap", "fake-work-secret")
    assert any("installing anthropic" in l for l in r2.log_lines)       # fell back to the base image
    assert await gone(sandboxes, r1.sandbox_id) and await gone(sandboxes, r2.sandbox_id)


async def test_allow_list_lets_the_worker_reach_anthropic_only():
    """With an allow-list, the session still starts and only listed domains are reachable."""
    sandboxes = RenderAsync().experimental.sandboxes
    s = settings(network_policy="allow-list", keep_sandboxes=True,
                 allowed_domains=("api.anthropic.com", "pypi.org", "files.pythonhosted.org"))
    launcher = RenderSandboxLauncher(sandboxes, s)
    sandbox_id = await launcher.start("work_allow", "sesn_allow", "fake-work-secret")
    try:
        code, lines = await launcher.follow(sandbox_id, "sesn_allow")
        assert any("installing anthropic" in l for l in lines)  # PyPI download got through
        assert any("sessions token" in l for l in lines)        # worker started and read the secret
        _, out = await run_cmd(sandboxes, sandbox_id,
                               "curl -s -m 8 -o /dev/null -w '%{http_code} ' https://api.anthropic.com/; "
                               "curl -s -m 8 -o /dev/null -w '%{http_code}' https://example.com || true")
        anthropic, other = out.split()[:2] if len(out.split()) > 1 else (out.strip(), "000")
        assert anthropic != "000" and other == "000"
    finally:
        await sandboxes.terminate(sandbox_id)


@pytest.mark.skipif(not os.environ.get("SANDBOX_SNAPSHOT_NAME"), reason="build a snapshot first")
async def test_blueprint_default_allow_list_with_snapshot():
    """The Blueprint's defaults together: allow-list egress and the prepared snapshot."""
    sandboxes = RenderAsync().experimental.sandboxes
    s = settings(network_policy="allow-list", snapshot_name=os.environ["SANDBOX_SNAPSHOT_NAME"],
                 allowed_domains=("api.anthropic.com", "pypi.org", "files.pythonhosted.org"))
    result = await RenderSandboxLauncher(sandboxes, s).run_session("work_bp", "sesn_bp", "fake-work-secret")
    text = "\n".join(result.log_lines)
    assert "installing anthropic" not in text                # SDK came from the snapshot
    assert "sessions token" in text                          # worker started and read the secret
    assert await gone(sandboxes, result.sandbox_id)


async def test_start_returns_while_the_worker_is_still_running():
    """Regression: the launch command must not block until the worker exits."""
    import time
    from orchestrator.launcher import START_CMD
    sandboxes = RenderAsync().experimental.sandboxes
    s = await sandboxes.create(timeout_seconds=300)
    launcher = RenderSandboxLauncher(sandboxes, settings())
    try:
        await launcher.wait_running(s.id)
        await run_cmd(sandboxes, s.id, f"mkdir -p {STATE_DIR} && printf '#!/bin/bash\\nsleep 20\\n' > {STATE_DIR}/bootstrap.sh")
        t0 = time.monotonic()
        code, out = await run_cmd(sandboxes, s.id, START_CMD)
        assert code == 0 and "started" in out
        assert time.monotonic() - t0 < 5, "launch blocked on the worker"
        _, ps = await run_cmd(sandboxes, s.id, "pgrep -f 'sleep 20' >/dev/null && echo running")
        assert "running" in ps
        # Retrying the launch (as the orchestrator does after a transient error) starts nothing new.
        code, out = await run_cmd(sandboxes, s.id, START_CMD)
        assert code == 0 and "started" in out
        _, count = await run_cmd(sandboxes, s.id, "pgrep -fc 'sleep 20'")
        assert count.strip() == "1", f"expected one worker, found {count.strip()}"
    finally:
        await sandboxes.terminate(s.id)
