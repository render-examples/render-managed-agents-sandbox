"""Runs inside a Render sandbox. Serves one Managed Agents session with Anthropic's SDK worker, then exits.

The orchestrator sets ANTHROPIC_ENVIRONMENT_ID, ANTHROPIC_SESSION_ID, ANTHROPIC_WORK_ID, and
WORKER_MAX_IDLE when it creates the sandbox, and uploads the session's secret to
/run/claude-worker/work_secret. The secret's session token authenticates the worker, so the
environment key stays out of the sandbox. ANTHROPIC_ENVIRONMENT_KEY is present only when the
orchestrator runs with FORWARD_ENVIRONMENT_KEY=true.
"""
import asyncio
import logging
import os
import re
from pathlib import Path

from anthropic import AsyncAnthropic

SECRET_FILE = Path("/run/claude-worker/work_secret")


def seconds(value: str) -> float:
    """Parse a duration such as 60s, 5m, or 90."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", value)
    if not match:
        raise SystemExit(f"invalid WORKER_MAX_IDLE: {value!r}")
    return float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]


async def main() -> None:
    secret = SECRET_FILE.read_text().strip() if SECRET_FILE.exists() else None
    environment_key = os.environ.get("ANTHROPIC_ENVIRONMENT_KEY")
    if not secret and not environment_key:
        raise SystemExit("no work secret and no environment key; cannot authenticate")
    async with AsyncAnthropic(api_key=None, auth_token=environment_key,
                              base_url=os.environ.get("ANTHROPIC_BASE_URL")) as client:
        worker = client.beta.environments.work.worker(
            workdir="/workspace",
            max_idle=seconds(os.environ.get("WORKER_MAX_IDLE", "60s")),
        )
        await worker.handle_item(
            work_id=os.environ["ANTHROPIC_WORK_ID"],
            environment_id=os.environ["ANTHROPIC_ENVIRONMENT_ID"],
            session_id=os.environ["ANTHROPIC_SESSION_ID"],
            environment_key=environment_key,
            work_secret=secret,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpx2"):  # one line per HTTP request otherwise
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(main())
