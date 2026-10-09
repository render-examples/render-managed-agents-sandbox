"""What can a sandbox do with the per-session secret it receives?

Claims one real work item with the environment key (as the orchestrator does), decodes the
secret, and tries the sessions token against Anthropic's API. Prints only field names and
HTTP status codes, never token values. Releases the work item afterwards.
Needs ANTHROPIC_API_KEY, ANTHROPIC_ENVIRONMENT_ID, ANTHROPIC_ENVIRONMENT_KEY, AGENT_ID,
and OTHER_SESSION_ID (any other session in the same workspace).
"""
import base64
import json
import os
import time

import httpx
from anthropic import Anthropic

API = "https://api.anthropic.com/v1"
HDR = {"anthropic-version": "2023-06-01", "anthropic-beta": "managed-agents-2026-04-01"}
admin = Anthropic()
env_id, env_key = os.environ["ANTHROPIC_ENVIRONMENT_ID"], os.environ["ANTHROPIC_ENVIRONMENT_KEY"]
other = os.environ["OTHER_SESSION_ID"]

session = admin.beta.sessions.create(agent=os.environ["AGENT_ID"], environment_id=env_id, title="audit: credentials")
admin.beta.sessions.events.send(session.id, events=[{"type": "user.message", "content": [{"type": "text", "text": "Use bash to run: echo hi"}]}])

orch = Anthropic(auth_token=env_key)
work = None
for _ in range(30):
    work = orch.beta.environments.work.poll(env_id, block_ms=999)
    if work and work.data.id == session.id:
        break
    time.sleep(1)
assert work and work.data.id == session.id, "did not claim the audit session"
orch.beta.environments.work.ack(work.id, environment_id=env_id)

payload = json.loads(base64.urlsafe_b64decode(work.secret + "=" * (-len(work.secret) % 4)))
print("secret payload fields:", sorted(payload))
def shape(v):
    if isinstance(v, dict): return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list): return [shape(x) for x in v[:2]]
    return type(v).__name__ + (f"(len {len(v)})" if isinstance(v, str) else "")
for field in ("auth", "environment_variables", "sources", "claude_code_args", "use_code_sessions", "api_base_url", "version"):
    print(f"payload.{field}:", json.dumps(shape(payload.get(field)))[:300])
TOKENS = {"sessions_token": payload["sessions_token"], "session_ingress_token": payload.get("session_ingress_token")}
tok = payload["sessions_token"]
try:
    parts = tok.split(".")
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    print("token claim names:", sorted(claims))
    if "exp" in claims:
        print(f"token lifetime remaining: {int(claims['exp'] - time.time())}s")
except Exception:
    print("token is opaque (not a JWT)")

def try_(label, method, path, **kw):
    for scheme in ("bearer", "x-api-key"):
        h = dict(HDR, **({"Authorization": f"Bearer {tok}"} if scheme == "bearer" else {"x-api-key": tok}))
        r = httpx.request(method, API + path, headers=h, timeout=30, **kw)
        if r.status_code < 400 or scheme == "x-api-key":
            print(f"{r.status_code}  {label}  [{scheme}]")
            return r.status_code

try_("read its own session", "GET", f"/sessions/{session.id}")
try_("read its own session's events", "GET", f"/sessions/{session.id}/events")
try_("read a different session", "GET", f"/sessions/{other}")
try_("send a message into a different session", "POST", f"/sessions/{other}/events",
     json={"events": [{"type": "user.message", "content": [{"type": "text", "text": "audit"}]}]})
try_("list all sessions", "GET", "/sessions")
try_("list environments", "GET", "/environments")
try_("claim other work from the queue", "GET", f"/environments/{env_id}/work/poll?block_ms=1")
try_("list agents", "GET", "/agents")
try_("list memory stores", "GET", "/memory_stores")
try_("call the model (Messages API)", "POST", "/messages",
     json={"model": "claude-opus-5-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
try_("create a session", "POST", "/sessions", json={"agent": os.environ["AGENT_ID"], "environment_id": env_id})

for i, a in enumerate(payload.get("auth") or []):
    tok = a.get("token")
    if tok:
        print(f"--- auth[{i}].token (type {a.get('type')})")
        try_("read a different session", "GET", f"/sessions/{other}")
        try_("list environments", "GET", "/environments")
        try_("call the model (Messages API)", "POST", "/messages",
             json={"model": "claude-opus-5-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
        print("    same as sessions_token:", tok == TOKENS["sessions_token"])
tok = TOKENS["sessions_token"]

if TOKENS["session_ingress_token"]:
    tok = TOKENS["session_ingress_token"]
    print("--- session_ingress_token")
    try_("read its own session", "GET", f"/sessions/{session.id}")
    try_("read a different session", "GET", f"/sessions/{other}")
    try_("list environments", "GET", "/environments")
    try_("call the model (Messages API)", "POST", "/messages",
         json={"model": "claude-opus-5-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
    tok = TOKENS["sessions_token"]

orch.beta.environments.work.stop(work.id, environment_id=env_id, force=True)
print("released work item; after release:")
try_("read its own session, after stop", "GET", f"/sessions/{session.id}")
