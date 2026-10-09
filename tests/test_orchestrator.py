"""Offline tests for the orchestrator's decisions. No network."""
import asyncio
from types import SimpleNamespace

from orchestrator.config import Settings
from orchestrator.launcher import LaunchError, SessionResult
from orchestrator.main import Orchestrator


def settings(**over):
    base = dict(environment_id="env_1", environment_key="sk-ant-oat01-key", anthropic_base_url=None,
                sandbox_timeout_seconds=600, max_concurrent_sessions=2, snapshot_name=None,
                network_policy="allow-all", allowed_domains=(), sdk_version="1.12.1", worker_max_idle="60s",
                keep_sandboxes=False, forward_environment_key=False)
    base.update(over)
    return Settings(**base)


class FakeWork:
    def __init__(self):
        self.stopped = []

    async def stop(self, work_id, *, environment_id, force=False):
        self.stopped.append((work_id, force))

    def poller(self, **kw):
        self.poller_kwargs = kw
        items = self.items

        async def gen():
            for i in items:
                yield i
            await asyncio.Event().wait()  # then block like a real long-poll
        return gen()


class FakeSessions:
    def __init__(self, metadata=None):
        self.metadata = metadata or {}

    async def retrieve(self, session_id):
        return SimpleNamespace(id=session_id, metadata=self.metadata)


def make(launcher, metadata=None, **over):
    work = FakeWork()
    anthropic = SimpleNamespace(beta=SimpleNamespace(environments=SimpleNamespace(work=work),
                                                     sessions=FakeSessions(metadata)))
    orch = Orchestrator(settings(**over), anthropic, sandboxes=None)
    orch.launcher = launcher
    return orch, work


def item(i, secret="sec"):
    return SimpleNamespace(id=f"work_{i}", data=SimpleNamespace(id=f"sesn_{i}"), secret=secret)


class FakeLauncher:
    def __init__(self, exit_code=0, raise_launch=False, delay=0.0):
        self.calls, self.exit_code, self.raise_launch, self.delay = [], exit_code, raise_launch, delay
        self.active = self.peak = 0
        self.start_snapshots = []

    async def run_session(self, work_id, session_id, secret, environment_key=None, start_snapshot_id=None):
        self.calls.append((work_id, secret, environment_key))
        self.start_snapshots.append(start_snapshot_id)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.raise_launch:
                raise LaunchError("boom")
            return SessionResult("sbx_1", self.exit_code, True)
        finally:
            self.active -= 1

    async def discover(self):
        return []


async def run_handle(orch, w):
    await orch.slots.acquire()
    await orch.handle(w)


async def test_clean_exit_does_not_stop_work_item():
    orch, work = make(FakeLauncher(exit_code=0))
    await run_handle(orch, item(1))
    assert work.stopped == []


async def test_missing_secret_is_refused_without_forwarding():
    launcher = FakeLauncher()
    orch, work = make(launcher)
    await run_handle(orch, item(1, secret=None))
    assert launcher.calls == []
    assert work.stopped == [("work_1", True)]


async def test_environment_key_only_sent_when_opted_in():
    launcher = FakeLauncher()
    orch, _ = make(launcher)
    await run_handle(orch, item(1))
    assert launcher.calls[0][2] is None
    launcher2 = FakeLauncher()
    orch2, _ = make(launcher2, forward_environment_key=True)
    await run_handle(orch2, item(2, secret=None))
    assert launcher2.calls[0] == ("work_2", None, "sk-ant-oat01-key")


async def test_launch_failure_and_crash_force_stop():
    orch, work = make(FakeLauncher(raise_launch=True))
    await run_handle(orch, item(1))
    orch2, work2 = make(FakeLauncher(exit_code=1))
    await run_handle(orch2, item(2))
    assert work.stopped == [("work_1", True)] and work2.stopped == [("work_2", True)]


async def test_concurrency_cap_and_slots_released():
    launcher = FakeLauncher(delay=0.05)
    orch, work = make(launcher, max_concurrent_sessions=2)
    work.items = [item(i) for i in range(6)]
    loop = asyncio.create_task(orch.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if len(launcher.calls) == 6 and launcher.active == 0:
            break
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    assert len(launcher.calls) == 6
    assert launcher.peak == 2
    assert work.poller_kwargs["auto_stop"] is False
    assert orch.slots._value == 2


def test_settings_default_to_an_allow_list(monkeypatch):
    for k, v in {"ANTHROPIC_ENVIRONMENT_ID": "env_1", "ANTHROPIC_ENVIRONMENT_KEY": "k",
                 "RENDER_API_KEY": "r", "RENDER_WORKSPACE_ID": "tea-1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SANDBOX_NETWORK_POLICY", raising=False)
    monkeypatch.delenv("SANDBOX_ALLOWED_DOMAINS", raising=False)
    s = Settings.from_env()
    assert s.network_policy == "allow-list"
    assert s.allowed_domains == ("api.anthropic.com", "pypi.org", "files.pythonhosted.org")
    monkeypatch.setenv("SANDBOX_NETWORK_POLICY", "open")
    import pytest
    with pytest.raises(SystemExit):
        Settings.from_env()


async def test_session_metadata_names_the_starting_snapshot():
    launcher = FakeLauncher()
    orch, _ = make(launcher, metadata={"render_snapshot_id": "snp-task"})
    await run_handle(orch, item(1))
    orch2, _ = make(launcher)
    await run_handle(orch2, item(2))
    assert launcher.start_snapshots == ["snp-task", None]


async def test_allow_list_serializes_current_and_legacy_policy(monkeypatch):
    from orchestrator.launcher import RenderSandboxLauncher
    from render.experimental.sandbox import api
    captured = {}

    async def create(*, client, body):
        captured.update(body.to_dict())
        return SimpleNamespace(status_code=201, parsed=SimpleNamespace())

    monkeypatch.setattr(api.create_sandbox, 'asyncio_detailed', create)
    monkeypatch.setattr(api, '_to_sandbox', lambda value: value)
    launcher = RenderSandboxLauncher(SimpleNamespace(api=SimpleNamespace(client=object())), settings(
        network_policy='allow-list', allowed_domains=('api.anthropic.com', '*.githubusercontent.com')))
    launcher.owner_id = 'tea-test'
    await launcher._create_allow_list({}, None)
    assert captured['networkPolicy'] == {
        'type': 'allow-list', 'default': 'allow-list',
        'rules': [{'domain': 'api.anthropic.com', 'protocol': 'https'},
                  {'domain': '*.githubusercontent.com', 'protocol': 'https'}],
        'allowedDomains': ['api.anthropic.com', '*.githubusercontent.com'],
    }


async def test_transient_sandbox_errors_are_retried():
    from render.client.errors import ServerError
    from render.experimental.sandbox import SandboxNotFoundError

    from orchestrator.launcher import START_CMD, retry_transient
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise SandboxNotFoundError("not ready")
        if len(calls) == 2:
            raise ServerError("503")
        return "ok"

    assert await retry_transient(flaky, "test", delay=0) == "ok" and len(calls) == 3
    assert START_CMD.startswith("if [ -e /run/claude-worker/launched ]; then echo started; exit 0; fi")
