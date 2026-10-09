"""End-to-end test: a real Managed Agents session whose tools run in a Render sandbox.

Run from your own machine (not the orchestrator host). It needs:
  ANTHROPIC_API_KEY          your Claude API key (stays on this machine)
  ANTHROPIC_ENVIRONMENT_ID   a self-hosted environment
  ANTHROPIC_ENVIRONMENT_KEY  that environment's key (Console only)
  RENDER_API_KEY, RENDER_WORKSPACE_ID
  AGENT_ID                   optional; a small test agent is created if unset

It starts the orchestrator locally WITHOUT your API key, sends the agent one task,
and checks that the answer came from a Render sandbox (Debian 12, Render's kernel)
and that the orchestrator created a sandbox for that session.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time

from anthropic import Anthropic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK = ("Run `uname -a` and `head -1 /etc/os-release` with bash, write both outputs to "
        "hello.txt in your working directory, then print the contents of hello.txt.")


def main() -> int:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_ENVIRONMENT_KEY",
                 "RENDER_API_KEY", "RENDER_WORKSPACE_ID"):
        if not os.environ.get(name):
            print(f"missing {name}", file=sys.stderr)
            return 2
    env_id = os.environ["ANTHROPIC_ENVIRONMENT_ID"]
    client = Anthropic()

    agent_id = os.environ.get("AGENT_ID")
    if not agent_id:
        agent = client.beta.agents.create(name="render-sandbox-e2e", model="claude-opus-5-5",
                                          tools=[{"type": "agent_toolset_20260401"}])
        agent_id = agent.id
        print(f"created agent {agent_id}")

    # The orchestrator must never see the organization API key.
    orch_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    orch = subprocess.Popen([sys.executable, "-m", "orchestrator.main"], cwd=ROOT, env=orch_env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    orch_log: list[str] = []

    def pump():
        for line in orch.stdout:
            orch_log.append(line)
            print("  [orchestrator]", line.rstrip())
    threading.Thread(target=pump, daemon=True).start()

    try:
        session = client.beta.sessions.create(agent=agent_id, environment_id=env_id,
                                              title="render sandbox e2e")
        print(f"session {session.id}")
        transcript: list[str] = []
        with client.beta.sessions.events.stream(session.id) as stream:
            client.beta.sessions.events.send(session.id, events=[
                {"type": "user.message", "content": [{"type": "text", "text": TASK}]}])
            deadline = time.time() + 300
            for event in stream:
                transcript.append(event.model_dump_json())
                print(f"  [event] {event.type}")
                if event.type == "session.status_idle":
                    reason = getattr(getattr(event, "stop_reason", None), "type", None)
                    print(f"  [idle] stop_reason={reason}")
                    # requires_action means a tool call is waiting for the worker; keep going.
                    if reason != "requires_action":
                        break
                if time.time() > deadline:
                    print("timed out waiting for the session", file=sys.stderr)
                    return 1

        blob = "\n".join(transcript)
        if os.environ.get("E2E_DUMP"):
            with open(os.environ["E2E_DUMP"], "w") as f:
                f.write(blob + "\n\n=== ORCHESTRATOR LOG ===\n" + "".join(orch_log))
        log = "".join(orch_log)
        checks = {
            "agent saw Debian 12 (Render sandbox base image)": "Debian GNU/Linux 12" in blob,
            "agent saw a Render sandbox kernel": bool(re.search(r"Linux \S+ 6\.\d+\.\d+\+", blob)),
            "orchestrator created a sandbox for this session":
                bool(re.search(rf"session={session.id} sandbox=sbx-\S+ created", log)),
        }
        for name, ok in checks.items():
            print(("PASS " if ok else "FAIL ") + name)
        return 0 if all(checks.values()) else 1
    finally:
        orch.terminate()
        try:
            orch.wait(timeout=30)
        except subprocess.TimeoutExpired:
            orch.kill()


if __name__ == "__main__":
    sys.exit(main())
