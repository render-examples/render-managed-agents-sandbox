"""Wait up to two minutes for the sandbox in SANDBOX_ID to reach running."""
import os
import time

from render import Render

sandbox_id = os.environ.get("SANDBOX_ID")
if not sandbox_id:
    raise SystemExit("Set SANDBOX_ID first.")
sandboxes = Render().experimental.sandboxes
for _ in range(60):
    status = sandboxes.from_id(sandbox_id).status
    if status == "running":
        print("Sandbox is running.")
        break
    if status in ("errored", "terminated"):
        raise SystemExit(f"{sandbox_id} is {status}.")
    time.sleep(2)
else:
    raise SystemExit(f"{sandbox_id} did not reach running. Stop it before retrying.")
