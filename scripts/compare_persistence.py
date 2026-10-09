"""Compare ways to keep a Render sandbox's state between agent turns.

Methods:
  keep-running         leave the sandbox up while idle (baseline)
  filesystem-snapshot  what the orchestrator does today
  runtime-snapshot     also captures memory and running processes
  suspend-api          probe for a suspend/resume endpoint

Each method gets the same workload, then we check what survived.
Needs RENDER_API_KEY and RENDER_WORKSPACE_ID.
"""
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from render import RenderAsync  # noqa: E402
from orchestrator.launcher import run_cmd  # noqa: E402

WORKLOAD = """set -e
mkdir -p /workspace && cd /workspace
echo data-ok > data.txt
python3 -m venv venv >/dev/null && venv/bin/pip -q install cowsay >/dev/null
mkdir -p /dev/pts && (mount | grep -q devpts || mount -t devpts devpts /dev/pts -o newinstance,ptmxmode=0666,mode=0620)
nohup python3 -c "
import time
n = 0
while True:
    n += 1
    open('/workspace/counter', 'w').write(str(n))
    time.sleep(1)
" > /dev/null 2>&1 &
sleep 3; echo ready"""

PROBE = """cd /workspace 2>/dev/null || { echo 'files=missing'; exit 0; }
echo "files=$(cat data.txt 2>/dev/null || echo missing)"
echo "package=$(venv/bin/python -c 'import cowsay; print("ok")' 2>/dev/null || echo missing)"
a=$(cat counter 2>/dev/null); sleep 2; b=$(cat counter 2>/dev/null)
echo "counter=$a->$b"
echo "process=$([ -n "$a" ] && [ "$a" != "$b" ] && echo running || echo stopped)"
echo "devpts=$(mount | grep -q devpts && echo mounted || echo missing)"
"""


async def wait_running(sb, sid):
    t = time.time()
    while (await sb.from_id(sid)).status != "running":
        await asyncio.sleep(0.5)
    return time.time() - t


async def snapshot(sb, sid, kind):
    t = time.time()
    snap = await sb.snapshots.create(sid, kind=kind, name=f"compare-{kind}")
    while snap.status == "creating":
        await asyncio.sleep(1)
        snap = await sb.snapshots.from_id(sandbox_group_id=snap.sandbox_group_id, snapshot_id=snap.id)
    return snap, time.time() - t


def probe_api(path):
    req = urllib.request.Request(f"https://api.render.com/v1{path}", method="POST", data=b"{}",
                                 headers={"Authorization": "Bearer " + os.environ["RENDER_API_KEY"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


async def fresh(sb):
    s = await sb.create(timeout_seconds=900)
    await wait_running(sb, s.id)
    code, out = await run_cmd(sb, s.id, WORKLOAD)
    assert "ready" in out, out
    return s


async def main():
    sb = RenderAsync().experimental.sandboxes
    owner = os.environ["RENDER_WORKSPACE_ID"]
    rows = {}

    # Baseline: keep running for 30 seconds of "idle".
    s = await fresh(sb)
    await asyncio.sleep(30)
    rows["keep-running"] = {"capture_s": 0, "restore_s": 0, "probe": (await run_cmd(sb, s.id, PROBE))[1]}
    await sb.terminate(s.id)

    for kind in ("filesystem", "runtime"):
        s = await fresh(sb)
        plan = (await sb.from_id(s.id)).plan
        _, before = await run_cmd(sb, s.id, "cat /workspace/counter")
        snap, cap = await snapshot(sb, s.id, kind)
        await sb.terminate(s.id)
        if snap.status != "available":
            rows[f"{kind}-snapshot"] = {"error": f"{snap.status}: {getattr(snap, 'error', '')}"}
            continue
        t = time.time()
        kwargs = {"snapshot_id": snap.id, "timeout_seconds": 300}
        if kind == "runtime":
            kwargs["plan"] = plan
        r = await sb.create(**kwargs)
        await wait_running(sb, r.id)
        restore = time.time() - t
        rows[f"{kind}-snapshot"] = {"capture_s": round(cap, 1), "restore_s": round(restore, 1),
                                    "counter_at_capture": before.strip(), "probe": (await run_cmd(sb, r.id, PROBE))[1]}
        await sb.terminate(r.id)
        await sb.snapshots.delete(sandbox_group_id=snap.sandbox_group_id, snapshot_id=snap.id)

    # Probe for suspend / resume endpoints on a throwaway sandbox.
    s = await sb.create(timeout_seconds=300)
    await wait_running(sb, s.id)
    codes = {p: probe_api(f"/sandboxes/{s.id}/{p}?ownerId={owner}") for p in ("suspend", "resume", "pause", "stop")}
    rows["suspend-api"] = {"http_status": codes, "status_after": (await sb.from_id(s.id)).status}
    await sb.terminate(s.id)

    for name, r in rows.items():
        print(f"== {name}")
        for k, v in r.items():
            print(f"  {k}: {v.strip() if isinstance(v, str) else v}")


asyncio.run(main())
