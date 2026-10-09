"""Delete the session, its snapshots, the staged task snapshot, and the agent if create_session made it."""
import json
from pathlib import Path

import anthropic
from render import Render

state = json.loads(Path("session.json").read_text())
client = anthropic.Anthropic()
sandboxes = Render().experimental.sandboxes
failures = []


def attempt(what, run):
    try:
        run()
        print(f"Deleted {what}.")
    except Exception as e:
        failures.append(f"{what}: {e}")


if state.get("session_id"):
    attempt(f"session {state['session_id']}", lambda: client.beta.sessions.delete(state["session_id"]))
if state.get("sandbox_group_id"):
    group = state["sandbox_group_id"]
    # Delete the staged snapshot by ID, then every snapshot saved for this session.
    # The list is newest first, 100 at a time, so read every page.
    ids = [state["staged_snapshot_id"]] if state.get("staged_snapshot_id") else []
    cursor = None
    while True:
        page = sandboxes.snapshots.list(sandbox_group_id=group, cursor=cursor, limit=100)
        ids += [s.id for s in page.snapshots if s.name == f"claude-session-{state.get('session_id')}"]
        cursor = page.next_cursor
        if not cursor:
            break
    for snapshot_id in dict.fromkeys(ids):
        attempt(f"snapshot {snapshot_id}", lambda snapshot_id=snapshot_id: sandboxes.snapshots.delete(
            sandbox_group_id=group, snapshot_id=snapshot_id))
if state.get("created_agent"):
    attempt(f"agent {state['agent_id']} (archived)", lambda: client.beta.agents.archive(state["agent_id"]))
if failures:
    raise SystemExit("Retry these, then run cleanup again:\n" + "\n".join(failures))
