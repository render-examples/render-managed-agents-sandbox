"""Create an agent (unless AGENT_ID is set) and a session that starts from the staged snapshot."""
import json
import os
from pathlib import Path

import anthropic

environment_id = os.environ.get("ANTHROPIC_ENVIRONMENT_ID")
if not environment_id:
    raise SystemExit("Set ANTHROPIC_ENVIRONMENT_ID first.")
state = json.loads(Path("session.json").read_text())
client = anthropic.Anthropic()

agent_id = os.environ.get("AGENT_ID")
created_agent = not agent_id
if not agent_id:
    agent_id = client.beta.agents.create(
        name="render-sandbox-tutorial",
        model="claude-opus-5-5",
        tools=[{"type": "agent_toolset_20260401"}],
    ).id
# The orchestrator reads render_snapshot_id when it claims the session.
session = client.beta.sessions.create(
    agent=agent_id,
    environment_id=environment_id,
    metadata={"render_snapshot_id": state["staged_snapshot_id"]},
)
state.update(agent_id=agent_id, created_agent=created_agent, session_id=session.id)
Path("session.json").write_text(json.dumps(state, indent=2))
print(f"Created session {session.id}. Saved session.json.")
