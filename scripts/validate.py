"""Real-session validation suite for the Render orchestrator.

Runs real Claude Managed Agents sessions through a locally started orchestrator and
checks behavior the guide describes. Needs ANTHROPIC_API_KEY, ANTHROPIC_ENVIRONMENT_ID,
ANTHROPIC_ENVIRONMENT_KEY, AGENT_ID, RENDER_API_KEY, RENDER_WORKSPACE_ID.
The orchestrator process never receives ANTHROPIC_API_KEY.

Usage: python scripts/validate.py [tools idle allowlist memory restart load]
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from anthropic import Anthropic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDLE = "20s"
client = Anthropic()
ENV_ID = os.environ["ANTHROPIC_ENVIRONMENT_ID"]
AGENT_ID = os.environ["AGENT_ID"]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)


class Orchestrator:
    def __init__(self, label: str, **extra_env):
        self.label, self.lines = label, []
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env.update(WORKER_MAX_IDLE=IDLE, LOG_HTTP="1", **extra_env)
        # ORCHESTRATOR=ts runs the TypeScript port (build it first: cd orchestrator-ts && npm run build).
        cmd = (["node", "orchestrator-ts/dist/main.js"] if os.environ.get("ORCHESTRATOR") == "ts"
               else [sys.executable, "-m", "orchestrator.main"])
        self.p = subprocess.Popen(cmd, cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.p.stdout:
            self.lines.append(line.rstrip())

    def text(self) -> str:
        return "\n".join(self.lines)

    def wait_for(self, pattern: str, timeout: float = 120) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if re.search(pattern, self.text()):
                return True
            time.sleep(0.5)
        return False

    def stop(self):
        self.p.send_signal(signal.SIGTERM)
        try:
            self.p.wait(30)
        except subprocess.TimeoutExpired:
            self.p.kill()


def turn(session_id: str, text: str, send: bool = True, timeout: float = 300) -> list:
    """Send a message (unless send=False) and collect events until Claude finishes the turn."""
    events = []
    with client.beta.sessions.events.stream(session_id) as stream:
        if send:
            client.beta.sessions.events.send(session_id, events=[
                {"type": "user.message", "content": [{"type": "text", "text": text}]}])
        end = time.time() + timeout
        # An idle event from the previous turn can arrive after the stream opens. Only count
        # idle events once our own message has come back on the stream.
        seen_ours = not send
        for e in stream:
            events.append(e)
            if e.type == "user.message":
                seen_ours = True
            if (seen_ours and e.type == "session.status_idle"
                    and getattr(e.stop_reason, "type", None) != "requires_action"):
                break
            if e.type == "session.error" or time.time() > end:
                break
    return events


def tools_used(events) -> list[str]:
    return [e.name for e in events if e.type == "agent.tool_use"]


def tool_results(events) -> list[tuple[bool, str]]:
    out = []
    for e in events:
        if e.type == "user.tool_result":
            text = "".join(getattr(c, "text", "") for c in (e.content or []))
            out.append((bool(e.is_error), text))
    return out


def sandboxes_for(orch: Orchestrator, session_id: str) -> list[str]:
    return re.findall(rf"session={session_id} sandbox=(sbx-\S+) created", orch.text())


def new_session(title: str, **kw):
    return client.beta.sessions.create(agent=AGENT_ID, environment_id=ENV_ID, title=title, **kw)


def test_tools_and_idle(orch: Orchestrator):
    s = new_session("validate: tools + follow-ups")
    ev = turn(s.id, "Do these steps with the named tools, in order. 1) write: create /workspace/a.txt containing "
                    "'alpha' and /workspace/notes/b.md containing 'beta TODO'. 2) glob: find files matching the "
                    "relative pattern **/*.md. 3) grep: search /workspace for 'TODO'. 4) edit: in /workspace/notes/b.md "
                    "replace 'beta TODO' with 'beta DONE'. 5) read: show /workspace/notes/b.md.")
    used = set(tools_used(ev))
    res = tool_results(ev)
    check("tools: write, glob, grep, edit, read all used", {"write", "glob", "grep", "edit", "read"} <= used,
          f"used={sorted(used)}")
    check("tools: no tool errors", not any(err for err, _ in res),
          "; ".join(t[:80] for err, t in res if err))
    check("tools: edit took effect", any("beta DONE" in t for _, t in res))

    ev2 = turn(s.id, "Use bash to run: cat /workspace/notes/b.md")
    out2 = " ".join(t for _, t in tool_results(ev2))
    check("follow-up within idle window reuses the same files", "beta DONE" in out2, out2[:80])
    check("follow-up within idle window reuses the same sandbox", len(sandboxes_for(orch, s.id)) == 1,
          str(sandboxes_for(orch, s.id)))

    first = sandboxes_for(orch, s.id)[0]
    gone = orch.wait_for(rf"sandbox={first} terminated", timeout=120)
    check("idle session's sandbox is deleted after WORKER_MAX_IDLE", gone)
    ev3 = turn(s.id, "Use bash to run: ls /workspace/notes 2>&1; echo exit=$?")
    out3 = " ".join(t for _, t in tool_results(ev3))
    check("follow-up after idle gets a fresh sandbox", len(sandboxes_for(orch, s.id)) == 2,
          str(sandboxes_for(orch, s.id)))
    # Persistence is on by default, so the files come back from the session snapshot.
    # test_idle covers SESSION_PERSISTENCE=none, where they're gone.
    check("follow-up after idle still has the files", "b.md" in out3, out3[:80])


def test_idle(orch: Orchestrator):
    s = new_session("validate: idle")
    turn(s.id, "Use bash to run: echo keep > /workspace/k.txt && cat /workspace/k.txt")
    first = sandboxes_for(orch, s.id)
    gone = bool(first) and orch.wait_for(rf"sandbox={first[0]} terminated", timeout=120)
    check("idle: sandbox deleted after WORKER_MAX_IDLE", gone)
    print("  sending follow-up after idle at", time.strftime("%H:%M:%S"), flush=True)
    ev = turn(s.id, "Use bash to run: ls /workspace/k.txt 2>&1; echo done", timeout=180)
    out = " ".join(t for _, t in tool_results(ev))
    print("  follow-up events:", [e.type for e in ev][:30], flush=True)
    check("idle: follow-up after idle gets a fresh sandbox", len(sandboxes_for(orch, s.id)) == 2,
          str(sandboxes_for(orch, s.id)))
    check("idle: follow-up after idle has lost the files", "No such file" in out, out[:80])


def test_persist(orch: Orchestrator):
    s = new_session("validate: persistence")
    turn(s.id, "Use bash to run: mkdir -p /workspace/proj && echo persisted-ok > /workspace/proj/state.txt && cat /workspace/proj/state.txt")
    saved = orch.wait_for(rf"session={s.id} saved files to snapshot", timeout=180)
    check("persist: idle sandbox saved to a session snapshot", saved)
    first = sandboxes_for(orch, s.id)
    orch.wait_for(rf"sandbox={first[0]} terminated", timeout=60)
    ev = turn(s.id, "Use bash to run: cat /workspace/proj/state.txt; ls -la /run/claude-worker/work_secret 2>&1 | head -1")
    out = " ".join(t for _, t in tool_results(ev))
    check("persist: follow-up after idle sees the earlier file", "persisted-ok" in out, out[:100])
    check("persist: restored from the session snapshot", bool(re.search(rf"session={s.id} restored files", orch.text())))
    check("persist: follow-up ran in a new sandbox", len(sandboxes_for(orch, s.id)) == 2, str(sandboxes_for(orch, s.id)))

    # Edge case: a follow-up sent while the previous sandbox is still being saved.
    second = sandboxes_for(orch, s.id)[-1]
    orch.wait_for(rf"session={s.id} sandbox={second} worker exited", timeout=180)
    ev = turn(s.id, "Use bash to run: echo round3 >> /workspace/proj/state.txt && cat /workspace/proj/state.txt")
    out = " ".join(t for _, t in tool_results(ev))
    check("persist: follow-up during a save waits and keeps the files", "persisted-ok" in out and "round3" in out, out[:100])
    check("persist: it waited for the save", "waiting for its previous sandbox to finish saving" in orch.text())


def test_runtime(orch: Orchestrator):
    s = new_session("validate: runtime persistence")
    turn(s.id, "Use bash to run exactly: setsid nohup python3 -c \"import time\nn=0\nwhile True:\n n+=1; open('/workspace/counter','w').write(str(n)); time.sleep(1)\" > /dev/null 2>&1 & sleep 3; cat /workspace/counter")
    saved = orch.wait_for(rf"session={s.id} saved files to snapshot", timeout=240)
    check("runtime: idle sandbox saved to a runtime snapshot", saved)
    ev = turn(s.id, "Use bash to run exactly: a=$(cat /workspace/counter); sleep 3; b=$(cat /workspace/counter); echo counter=$a-$b")
    out = " ".join(t for _, t in tool_results(ev))
    m = re.search(r"counter=(\d+)-(\d+)", out)
    check("runtime: background process still running after restore", bool(m) and int(m.group(2)) > int(m.group(1)), out[:60])
    check("runtime: restored from the session snapshot", bool(re.search(rf"session={s.id} restored files", orch.text())))


def test_creds(orch: Orchestrator):
    """While a session is mid-turn, run sandbox/audit_inside.sh in its sandbox from the operator side."""
    import asyncio
    from render import RenderAsync
    sys.path.insert(0, ROOT)
    from orchestrator.launcher import run_cmd

    s = new_session("validate: credentials inside the sandbox")
    worker = threading.Thread(target=turn, args=(s.id, "Use bash to run exactly: sleep 90; echo done"))
    worker.start()
    sbx = None
    for _ in range(120):
        ids = sandboxes_for(orch, s.id)
        if ids and orch.wait_for(rf"sandbox={ids[-1]} worker started", timeout=1):
            sbx = ids[-1]
            break
        time.sleep(1)
    check("creds: sandbox running for the session", bool(sbx))
    if not sbx:
        return
    time.sleep(15)  # let the worker start serving the bash call
    async def audit():
        sandboxes = RenderAsync().experimental.sandboxes
        await sandboxes.copy_to(sbx, os.path.join(ROOT, "sandbox", "audit_inside.sh"), "/root/audit_inside.sh")
        return await run_cmd(sandboxes, sbx, "bash /root/audit_inside.sh 2>&1")

    code, out = asyncio.run(audit())
    if not out.strip():
        print(f"  audit printed nothing (exit {code})")
    worker.join()
    print("  " + "\n  ".join(l for l in out.splitlines() if "=" in l))
    grab = lambda k: (re.search(rf"{k}=(\S+)", out) or [None, "?"])[1]
    check("creds: no environment key in the sandbox's environment", grab("env_has_environment_key") == "no")
    check("creds: no Render API key in the sandbox's environment", grab("env_has_render_key") == "no")
    check("creds: no environment key anywhere on disk", grab("files_with_environment_key_prefix") == "0")
    check("creds: no Claude API key anywhere on disk", grab("files_with_api_key_prefix") == "0")
    check("creds: no Render API key anywhere on disk", grab("files_with_render_key_prefix") == "0")
    check("creds: worker's process environment has no environment key", grab("worker_environ_has_environment_key") == "no")


def test_allowlist(orch: Orchestrator):
    s = new_session("validate: allow-list")
    ev = turn(s.id, "Use bash to run exactly: "
                    "curl -s -m 8 -o /dev/null -w 'anthropic=%{http_code} ' https://api.anthropic.com/; "
                    "curl -s -m 8 -o /dev/null -w 'example=%{http_code}' https://example.com/; echo")
    out = " ".join(t for _, t in tool_results(ev))
    a = re.search(r"anthropic=(\d+)", out)
    x = re.search(r"example=(\d+)", out)
    check("allow-list: Anthropic reachable from a real session", bool(a) and a.group(1) != "000", out[:80])
    check("allow-list: other domains blocked in a real session", bool(x) and x.group(1) == "000", out[:80])


def test_memory(orch: Orchestrator):
    store = client.beta.memory_stores.create(name="render-validate", description="Render sandbox validation test")
    try:
        s = new_session("validate: memory store",
                        resources=[{"type": "memory_store", "memory_store_id": store.id, "access": "read_write"}])
        ev = turn(s.id, "Save a new memory: create a file named render-test.md in your memory store directory "
                        "containing exactly 'render-memory-ok'. Then list the memory directory with bash.")
        res = tool_results(ev)
        check("memory: store mounted and writable in the sandbox", not any(err for err, _ in res),
              "; ".join(t[:100] for err, t in res if err))
        sbx = sandboxes_for(orch, s.id)
        if sbx:
            orch.wait_for(rf"sandbox={sbx[-1]} terminated", timeout=150)
        found, paths = False, []
        for _ in range(10):
            items = list(client.beta.memory_stores.memories.list(store.id))
            paths = [getattr(i, "path", "") for i in items]
            if any("render-test" in p for p in paths):
                found = True
                break
            time.sleep(3)
        check("memory: file synced back to the store", found, str(paths))
    finally:
        client.beta.memory_stores.archive(store.id)


def test_restart():
    a = Orchestrator("A")
    a.wait_for(r"polling environment=")
    s = new_session("validate: restart recovery")
    client.beta.sessions.events.send(s.id, events=[{"type": "user.message", "content": [{"type": "text", "text":
        "Use bash to run exactly: sleep 45 && echo survived > /workspace/s.txt && cat /workspace/s.txt"}]}])
    started = a.wait_for(rf"session={s.id} sandbox=\S+ worker started", timeout=120)
    time.sleep(8)
    a.stop()
    check("restart: first orchestrator left the sandbox running",
          "left running for the next orchestrator" in a.text() or "keep running in their sandboxes" in a.text())
    b = Orchestrator("B")
    adopted = b.wait_for(r"adopting sandbox=", timeout=120)
    ev = turn(s.id, "", send=False, timeout=240)
    out = " ".join(t for _, t in tool_results(ev))
    check("restart: worker was running before the restart", started)
    check("restart: new orchestrator adopted the running sandbox", adopted)
    check("restart: the session finished its work", "survived" in out, out[:80])
    cleaned = b.wait_for(r"sandbox=sbx-\S+ terminated", timeout=150)
    check("restart: adopted sandbox cleaned up afterwards", cleaned)
    b.stop()


def test_load(orch: Orchestrator, n: int = 4):
    before = len(orch.lines)
    sessions = [new_session(f"validate: load {i}") for i in range(n)]
    t0 = time.time()
    with ThreadPoolExecutor(n) as pool:
        outs = list(pool.map(lambda s: turn(s.id, "Use bash to run: echo load-ok"), sessions))
    elapsed = time.time() - t0
    ok = sum(any("load-ok" in t for _, t in tool_results(ev)) for ev in outs)
    check(f"load: {n} concurrent sessions all ran", ok == n, f"{ok}/{n} in {elapsed:.0f}s")
    for s in sessions:
        for sbx in sandboxes_for(orch, s.id):
            orch.wait_for(rf"sandbox={sbx} terminated", timeout=150)
    window = orch.lines[before:]
    calls = [l for l in window if "HTTP Request" in l and "api.render.com" in l]
    check("load: no Render rate-limit responses", not any(" 429 " in l for l in window))
    print(f"  Render API calls for {n} sessions, start to cleanup: {len(calls)} "
          f"(~{len(calls) / n:.0f} per session, excluding the per-session log stream)")


def test_queue_stats(orch: Orchestrator):
    stats = client.beta.environments.work.stats(ENV_ID)
    check("queue check: workers_polling >= 1 while the orchestrator runs", stats.workers_polling >= 1,
          f"depth={stats.depth} pending={stats.pending} workers_polling={stats.workers_polling}")


if __name__ == "__main__":
    wanted = set(sys.argv[1:]) or {"stats", "tools", "allowlist", "memory", "load", "restart"}
    orch = Orchestrator("main")
    orch.wait_for(r"polling environment=")
    try:
        if "stats" in wanted:
            time.sleep(3)
            test_queue_stats(orch)
        if "tools" in wanted:
            test_tools_and_idle(orch)
        if "persist" in wanted:
            test_persist(orch)
        if "creds" in wanted:
            test_creds(orch)
        if "runtime" in wanted:
            test_runtime(orch)
        if "allowlist" in wanted:
            test_allowlist(orch)
        if "memory" in wanted:
            test_memory(orch)
        if "load" in wanted:
            test_load(orch)
    finally:
        orch.stop()
        with open(os.environ.get("VALIDATE_LOG", "/dev/null"), "w") as f:
            f.write(orch.text())
    if "idle" in wanted:
        nopersist = Orchestrator("no-persistence", SESSION_PERSISTENCE="none")
        nopersist.wait_for(r"polling environment=")
        try:
            test_idle(nopersist)
        finally:
            nopersist.stop()
    if "restart" in wanted:
        test_restart()
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)

