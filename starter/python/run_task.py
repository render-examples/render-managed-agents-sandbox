"""Send prompt.txt, stream the agent's work, then wait for the orchestrator to save the result."""
import json
import time
from pathlib import Path

import anthropic
from render import Render

state = json.loads(Path("session.json").read_text())
prompt = (Path(__file__).resolve().parent.parent / "prompt.txt").read_text()
client = anthropic.Anthropic(timeout=600)

with client.beta.sessions.events.stream(state["session_id"]) as stream:
    client.beta.sessions.events.send(
        state["session_id"],
        events=[{"type": "user.message", "content": [{"type": "text", "text": prompt}]}],
    )
    sent = False
    for event in stream:
        if event.type == "user.message":
            sent = True
        elif event.type == "agent.tool_use":
            print(f"\n[{event.name}]")
        elif event.type == "agent.message":
            for block in event.content:
                if block.type == "text":
                    print(block.text, end="", flush=True)
        elif event.type == "session.error":
            raise SystemExit(event.error.message if event.error else "session error")
        elif sent and event.type == "session.status_idle":
            # requires_action means a tool call is waiting on the worker in the sandbox.
            if event.stop_reason.type == "requires_action":
                continue
            if event.stop_reason.type != "end_turn":
                raise SystemExit(f"Session stopped: {event.stop_reason.type}")
            break
print("\nTurn completed. Waiting for the orchestrator to save the sandbox...")

# After WORKER_MAX_IDLE, the orchestrator saves /workspace as a new claude-session-<ID> snapshot.
sandboxes = Render().experimental.sandboxes
name = f"claude-session-{state['session_id']}"
for _ in range(60):
    page = sandboxes.snapshots.list(sandbox_group_id=state["sandbox_group_id"],
                                    status="available", limit=100)
    saved = next((s for s in page.snapshots if s.name == name), None)
    if saved:
        state["result_snapshot_id"] = saved.id
        Path("session.json").write_text(json.dumps(state, indent=2))
        print(f"Saved result snapshot: {saved.id}")
        break
    time.sleep(5)
else:
    raise SystemExit("No result snapshot after five minutes. Check the orchestrator's logs.")
