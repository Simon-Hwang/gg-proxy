"""``runtime_ctx.credentials`` → ``ClaudeCodeOptions.env`` injection — Plan v3 §A.

Plan v3 closes the bug where the inprocess executor silently dropped
``runtime_ctx.credentials`` while the docker executor honoured them
(see ``DockerExecutor._build_env``). These tests pin the new
``_make_runner_core`` env-merge order:

    1. runtime_ctx.credentials
    2. spec.plugins.extra_env    (wins over credentials — same precedence as docker)
    3. RELAY_TRACE_ID            (explicit set; inprocess-only "system marker wins" convention)
    4. RELAY_INSTALL_DIR_ROOT    (setdefault — extra_env can still override)
    5. extra_args["plugin-dir"]  (additive — host ~/.claude stays visible)

Reasoning behind the picky tests:

* **#1** is the bug-fix proof — ``ANTHROPIC_API_KEY`` from the API body
  finally reaches the SDK subprocess in inprocess mode (previously it
  was silently discarded).
* **#2** matches docker's contract — caller-supplied ``extra_env`` is
  the highest-priority knob, so an operator can hot-swap credentials
  via ``--env-file`` without rewriting the API submission.
* **#3** verifies the safety claim from the Plan v3 SDK env-merge spike:
  empty ``options.env`` keeps the SDK transport falling back to
  ``os.environ`` (existing single-tenant deploys with shell-env
  ``ANTHROPIC_API_KEY`` keep working).
* **#4** pins the inprocess-only "RELAY_TRACE_ID is a system marker"
  convention — a hostile or careless caller cannot smuggle a fake
  trace id via credentials.
* **#5** pins the inprocess config-layering contract: host ``~/.claude`` is
  not replaced; the per-session install root is added via ``--plugin-dir``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_code_sdk import ResultMessage

from gg_relay.session.client import make_sdk_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.plugins.protocol import InstallReport
from gg_relay.session.spec import (
    PluginManifest,
    SessionRuntimeContext,
    SessionSpec,
)
from gg_relay.session.transport.protocol import TransportClosed

pytestmark = pytest.mark.asyncio


class _CapturingStub:
    """SDK stub that records the :class:`ClaudeCodeOptions` it was given.

    Yields a single ``ResultMessage`` so the runner core exits cleanly
    after one loop iteration; that's enough to capture the options
    object the runner built.
    """

    captured: Any = None

    def __init__(self, options: Any) -> None:
        type(self).captured = options

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def receive_messages(self) -> AsyncIterator[Any]:
        msg = ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="x",
            usage={},
            total_cost_usd=0.0,
            result="ok",
            permission_denials=[],
        )
        yield msg


def _spec(tmp_path: Path, *, extra_env: tuple[tuple[str, str], ...] = ()) -> SessionSpec:
    return SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=PluginManifest(
            profile="minimal",
            extra_env=extra_env,
        ),
        executor="inprocess",
    )


def _install_report(install_root: Path) -> InstallReport:
    return InstallReport(
        schema_version="gg.install.v1",
        profile_id="minimal",
        selected_modules=(),
        included_components=(),
        excluded_components=(),
        install_root=install_root,
        installed_at="2026-05-25T00:00:00Z",
        duration_ms=1,
    )


async def _drain_to_end(handle, *, timeout: float = 1.0) -> None:
    """Block until session.end (or transport close) so the runner exits."""
    import asyncio

    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=timeout)
        except (TimeoutError, TransportClosed):
            return
        if f.get("type") == "session.end":
            return


async def _run_once(
    spec: SessionSpec,
    *,
    runtime_ctx: SessionRuntimeContext | None = None,
    install_report: InstallReport | None = None,
) -> None:
    """Spin up the executor + runner one time, drain, then stop."""
    _CapturingStub.captured = None
    coord = HITLCoordinator()
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=_CapturingStub,
        runtime_ctx=runtime_ctx,
        install_report=install_report,
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(spec)
    try:
        await _drain_to_end(handle)
    finally:
        await executor.stop(handle)


class TestCredentialsPassThrough:
    """Plan v3 §A — credentials reach the SDK env in inprocess mode."""

    async def test_runtime_ctx_credentials_reach_sdk_env(
        self, tmp_path: Path
    ) -> None:
        """The bug-fix proof: ``ANTHROPIC_API_KEY`` from the API body now
        lands in ``options.env`` (previously dropped on the floor)."""
        await _run_once(
            _spec(tmp_path),
            runtime_ctx=SessionRuntimeContext(
                credentials={"ANTHROPIC_API_KEY": "sk-from-runtime"},
            ),
        )
        opts = _CapturingStub.captured
        assert opts is not None
        env = dict(opts.env or {})
        assert env.get("ANTHROPIC_API_KEY") == "sk-from-runtime"

    async def test_model_and_subagent_model_reach_sdk_options(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kimi-style model config is passed into SDK session options.

        The provider env vars still flow through ``options.env`` for CLI /
        provider compatibility, but the primary model selection uses
        ``ClaudeCodeOptions.model``. The current Claude Code CLI does not
        support ``--subagent-model``, so the subagent model is injected into
        this session's env as ``CLAUDE_CODE_SUBAGENT_MODEL``.
        """
        monkeypatch.setenv("ANTHROPIC_MODEL", "kimi-k2.5")
        monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "kimi-k2.5")
        _CapturingStub.captured = None
        coord = HITLCoordinator()
        runner = make_sdk_runner(
            policy=DEFAULT_POLICY,
            coordinator=coord,
            sdk_factory=_CapturingStub,
            model="kimi-k2.5",
            subagent_model="kimi-k2.5",
            setting_sources="project,local",
        )
        executor = InProcessExecutor(runner=runner)
        handle = await executor.start(_spec(tmp_path))
        try:
            await _drain_to_end(handle)
        finally:
            await executor.stop(handle)

        opts = _CapturingStub.captured
        assert opts is not None
        assert opts.model == "kimi-k2.5"
        env = dict(opts.env or {})
        assert env.get("ANTHROPIC_MODEL") == "kimi-k2.5"
        assert env.get("ANTHROPIC_DEFAULT_SONNET_MODEL") == "kimi-k2.5"
        assert env.get("CLAUDE_CODE_SUBAGENT_MODEL") == "kimi-k2.5"
        assert opts.extra_args.get("setting-sources") == "project,local"

    async def test_extra_env_overrides_credentials(
        self, tmp_path: Path
    ) -> None:
        """``spec.plugins.extra_env`` is the highest-priority caller knob —
        same precedence as ``DockerExecutor._build_env``."""
        await _run_once(
            _spec(
                tmp_path,
                extra_env=(("ANTHROPIC_API_KEY", "sk-from-extra-env"),),
            ),
            runtime_ctx=SessionRuntimeContext(
                credentials={"ANTHROPIC_API_KEY": "sk-from-runtime"},
            ),
        )
        opts = _CapturingStub.captured
        env = dict(opts.env or {})
        assert env.get("ANTHROPIC_API_KEY") == "sk-from-extra-env"

    async def test_no_credentials_keeps_env_clean(
        self, tmp_path: Path
    ) -> None:
        """Empty credentials + empty extra_env → ``options.env`` is the
        empty/extra_env-only dict (no ``ANTHROPIC_*``). The SDK transport
        merges ``options.env`` on top of ``os.environ``, so this is the
        path that preserves shell-env inheritance for single-tenant
        deployments."""
        await _run_once(
            _spec(tmp_path),  # no extra_env
            runtime_ctx=SessionRuntimeContext(),  # empty credentials, empty trace_id
        )
        opts = _CapturingStub.captured
        env = dict(opts.env or {})
        assert "ANTHROPIC_API_KEY" not in env
        # No leftover system markers either when trace_id is empty.
        assert "RELAY_TRACE_ID" not in env

    async def test_host_auth_env_is_explicitly_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-tenant host auth env should be forwarded explicitly.

        Some local/dev launches rely on shell or ``.env`` auth variables instead
        of per-request credentials. We keep that path working by copying the
        allowlisted host auth env into ``options.env`` before runtime overrides.
        """
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-from-host")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.test")

        await _run_once(
            _spec(tmp_path),
            runtime_ctx=SessionRuntimeContext(),
        )

        opts = _CapturingStub.captured
        env = dict(opts.env or {})
        assert env.get("ANTHROPIC_AUTH_TOKEN") == "tok-from-host"
        assert env.get("ANTHROPIC_BASE_URL") == "https://proxy.example.test"

    async def test_trace_id_wins_over_credentials_attempt_to_set_it(
        self, tmp_path: Path
    ) -> None:
        """A hostile / careless caller cannot smuggle a fake
        ``RELAY_TRACE_ID`` via ``runtime_ctx.credentials`` — the explicit
        system-marker injection always wins. Pins the inprocess
        "system marker overrides credentials" convention from Plan v3 A.3."""
        await _run_once(
            _spec(tmp_path),
            runtime_ctx=SessionRuntimeContext(
                credentials={"RELAY_TRACE_ID": "evil-spoofed-id"},
                trace_id="real-trace",
            ),
        )
        opts = _CapturingStub.captured
        env = dict(opts.env or {})
        assert env.get("RELAY_TRACE_ID") == "real-trace"

    async def test_install_root_is_added_as_plugin_dir(
        self, tmp_path: Path
    ) -> None:
        """The per-session install root is additive in inprocess mode.

        We intentionally do not replace the host's Claude Code home with
        ``CLAUDE_CONFIG_DIR``. Instead the SDK subprocess keeps inheriting
        host ``~/.claude`` and receives the session install root as both a
        relay marker and a CLI ``--plugin-dir`` argument.
        """
        install_root = tmp_path / "install"
        install_root.mkdir()
        await _run_once(
            _spec(tmp_path),
            runtime_ctx=SessionRuntimeContext(),
            install_report=_install_report(install_root),
        )
        opts = _CapturingStub.captured
        env = dict(opts.env or {})
        assert env.get("RELAY_INSTALL_DIR_ROOT") == str(install_root)
        assert "CLAUDE_CONFIG_DIR" not in env
        assert env.get("CLAUDE_ROOT") is None
        assert dict(opts.extra_args or {}).get("plugin-dir") == str(install_root)

    async def test_extra_env_overrides_relay_install_dir_root_marker(
        self, tmp_path: Path
    ) -> None:
        """``RELAY_INSTALL_DIR_ROOT`` is a marker only; caller extra_env can
        override it without changing the actual ``--plugin-dir`` target."""
        install_root = tmp_path / "install"
        install_root.mkdir()
        await _run_once(
            _spec(
                tmp_path,
                extra_env=(("RELAY_INSTALL_DIR_ROOT", "/from/extra/env"),),
            ),
            runtime_ctx=SessionRuntimeContext(),
            install_report=_install_report(install_root),
        )
        opts = _CapturingStub.captured
        env = dict(opts.env or {})
        assert env.get("RELAY_INSTALL_DIR_ROOT") == "/from/extra/env"
        assert dict(opts.extra_args or {}).get("plugin-dir") == str(install_root)
