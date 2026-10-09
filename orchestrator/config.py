"""Settings for the orchestrator, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# The worker needs Anthropic's API, and PyPI when the snapshot is missing and it installs the SDK.
DEFAULT_ALLOWED_DOMAINS = "api.anthropic.com,pypi.org,files.pythonhosted.org"


@dataclass(frozen=True)
class Settings:
    environment_id: str
    environment_key: str
    anthropic_base_url: str | None
    sandbox_timeout_seconds: int
    max_concurrent_sessions: int
    snapshot_name: str | None
    network_policy: str
    allowed_domains: tuple[str, ...]
    sdk_version: str
    worker_max_idle: str
    keep_sandboxes: bool
    forward_environment_key: bool
    ready_timeout_seconds: float = 90.0
    session_persistence: bool = True
    persistence_kind: str = "filesystem"
    sandbox_plan: str = "starter"

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [n for n in ("ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_ENVIRONMENT_KEY",
                               "RENDER_API_KEY", "RENDER_WORKSPACE_ID") if not os.environ.get(n)]
        if missing:
            raise SystemExit(f"missing required environment variables: {', '.join(missing)}")
        policy = os.environ.get("SANDBOX_NETWORK_POLICY", "allow-list")
        if policy not in ("allow-all", "allow-list", "deny-all"):
            raise SystemExit("SANDBOX_NETWORK_POLICY must be allow-all, allow-list, or deny-all")
        return cls(
            environment_id=os.environ["ANTHROPIC_ENVIRONMENT_ID"],
            environment_key=os.environ["ANTHROPIC_ENVIRONMENT_KEY"],
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
            sandbox_timeout_seconds=int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", "3600")),
            max_concurrent_sessions=int(os.environ.get("MAX_CONCURRENT_SESSIONS", "20")),
            snapshot_name=os.environ.get("SANDBOX_SNAPSHOT_NAME") or None,
            network_policy=policy,
            allowed_domains=tuple(d.strip() for d in os.environ.get(
                "SANDBOX_ALLOWED_DOMAINS", DEFAULT_ALLOWED_DOMAINS).split(",") if d.strip()),
            sdk_version=os.environ.get("ANTHROPIC_SDK_VERSION", "1.12.1"),
            worker_max_idle=os.environ.get("WORKER_MAX_IDLE", "60s"),
            keep_sandboxes=_bool("KEEP_SANDBOXES"),
            forward_environment_key=_bool("FORWARD_ENVIRONMENT_KEY"),
            session_persistence=os.environ.get("SESSION_PERSISTENCE", "snapshot").lower() != "none",
            persistence_kind="runtime" if os.environ.get("SESSION_PERSISTENCE", "").lower() == "runtime" else "filesystem",
            sandbox_plan=os.environ.get("SANDBOX_PLAN", "starter"),
        )
