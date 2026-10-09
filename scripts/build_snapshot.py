"""Build a sandbox snapshot with Anthropic's SDK preinstalled.

The orchestrator works without it (the startup script installs the SDK in about six
seconds), but a snapshot removes the install from every session start. Snapshots expire
after three days, so render.yaml runs this daily as a cron job.

Usage: python -m scripts.build_snapshot   (needs RENDER_API_KEY, RENDER_WORKSPACE_ID)
"""
from __future__ import annotations

import asyncio
import os
import sys

from render import RenderAsync

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orchestrator.launcher import run_cmd  # noqa: E402

NAME = os.environ.get("SANDBOX_SNAPSHOT_NAME", "claude-worker")
SDK_VERSION = os.environ.get("ANTHROPIC_SDK_VERSION", "1.12.1")
# Add your agent's toolchain here (apt packages, language runtimes, repos).
EXTRA_SETUP = os.environ.get("SNAPSHOT_EXTRA_SETUP", "")

SETUP = f"""set -euo pipefail
python3 -m venv /opt/claude-runner
/opt/claude-runner/bin/pip install --quiet --disable-pip-version-check "anthropic=={SDK_VERSION}"
mkdir -p /workspace
{EXTRA_SETUP}
/opt/claude-runner/bin/python -c 'import anthropic; print("anthropic", anthropic.__version__)'"""


async def wait(get, ok: str, bad: tuple[str, ...], what: str, tries: int = 200):
    for _ in range(tries):
        obj = await get()
        if obj.status == ok:
            return obj
        if obj.status in bad:
            raise RuntimeError(f"{what} is {obj.status}: {getattr(obj, 'error', '')}")
        await asyncio.sleep(1.5)
    raise TimeoutError(f"{what} not {ok} in time")


async def main() -> None:
    sandboxes = RenderAsync().experimental.sandboxes
    sb = await sandboxes.create(timeout_seconds=900)
    try:
        await wait(lambda: sandboxes.from_id(sb.id), "running", ("errored", "terminated"), "sandbox")
        code, out = await run_cmd(sandboxes, sb.id, SETUP)
        print(out.strip())
        if code != 0:
            raise SystemExit(f"setup failed with exit code {code}")
        snap = await sandboxes.snapshots.create(sb.id, name=NAME)
        snap = await wait(lambda: sandboxes.snapshots.from_id(sandbox_group_id=snap.sandbox_group_id,
                                                               snapshot_id=snap.id),
                          "available", ("failed",), "snapshot")
        print(f"snapshot {snap.id} name={NAME} available, expires {snap.expires_at}")
    finally:
        await sandboxes.terminate(sb.id)


if __name__ == "__main__":
    asyncio.run(main())
