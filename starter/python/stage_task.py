"""Save the task files in a Render snapshot.

create_session points the session at it, and the orchestrator starts the session's
sandbox from it, so the agent finds the files in /workspace.
"""
import json
import time
from pathlib import Path

from render import Render
from render.experimental.sandbox import SandboxExecExit, SnapshotNotFoundError

fixture = Path(__file__).resolve().parent.parent / "fixture"
sandboxes = Render().experimental.sandboxes
options = {"timeout_seconds": 900, "network_policy": "deny-all"}

# Start from the orchestrator's prepared snapshot, so ant is already installed.
try:
    sandbox = sandboxes.create(snapshot_name="claude-worker", **options)
except SnapshotNotFoundError:
    sandbox = sandboxes.create(**options)
try:
    for _ in range(120):
        if sandboxes.from_id(sandbox.id).status == "running":
            break
        time.sleep(1)
    else:
        raise SystemExit(f"Sandbox {sandbox.id} did not start.")
    for name in ("orders.csv", "total.mjs", "test_total.mjs"):
        sandboxes.copy_to(sandbox.id, fixture / name, f"/workspace/{name}")
    for event in sandboxes.exec(sandbox.id, "mkdir -p /workspace/outputs"):
        if isinstance(event, SandboxExecExit) and event.exit_code != 0:
            raise SystemExit("Could not create /workspace/outputs.")
    snapshot = sandboxes.snapshots.create(sandbox.id, name=f"claude-task-{int(time.time() * 1000)}")
    for _ in range(150):
        if snapshot.status != "creating":
            break
        time.sleep(2)
        snapshot = sandboxes.snapshots.from_id(sandbox_group_id=snapshot.sandbox_group_id,
                                               snapshot_id=snapshot.id)
    if snapshot.status != "available":
        raise SystemExit(f"Snapshot {snapshot.id} is {snapshot.status}.")
    state = {"sandbox_group_id": snapshot.sandbox_group_id, "staged_snapshot_id": snapshot.id}
    Path("session.json").write_text(json.dumps(state, indent=2))
    print(f"Staged the task in snapshot {snapshot.id}.")
finally:
    sandboxes.terminate(sandbox.id)
