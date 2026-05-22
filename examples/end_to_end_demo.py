"""End-to-end demo: spin up the FastAPI app in-process and drive it.

Run with::

    python examples/end_to_end_demo.py

What it shows:
1. Build :class:`Config` programmatically (no .env required).
2. Boot the app via the lifespan context (no real server).
3. POST to ``/api/v1/sessions`` with a trivial in-process executor
   override so the demo works without docker or the Anthropic SDK.
4. Poll the session detail until ``completed``.
5. List sessions and print the result.

Use this as the smallest reproducible example when filing bugs.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport
from gg_relay.store import create_all_tables, make_async_engine


async def _scripted(transport: SessionTransport, spec: SessionSpec) -> None:
    del spec
    await transport.send(make_msg_chunk(1, {"text": "demo started"}))
    await transport.send(
        make_session_end(2, "completed", tokens={}, cost_usd=0.0)
    )


def _make_config(tmp: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp}/demo.db"
    cfg.api_keys_raw = "demo-key"
    cfg.gg_plugins_home = tmp / "plugins"
    cfg.install_dir_root = tmp / "installs"
    cfg.dashboard_admin_password = SecretStr("admin")
    cfg.dashboard_session_secret = SecretStr("x" * 32)
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = _make_config(tmp)

        # Create the DB schema (production would run `gg-relay migrate`).
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        await eng.dispose()

        app = create_app(cfg)

        def _factory(
            kind: str,
            policy: ToolPolicy,
            coordinator: HITLCoordinator,
            session_id: str,
        ):
            del kind, policy, coordinator, session_id
            return InProcessExecutor(runner=_scripted)

        app.state.executor_factory_override = _factory

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://demo"
        ) as ac, app.router.lifespan_context(app):
            headers = {"X-API-Key": "demo-key"}

            print("→ POST /api/v1/sessions")
            r = await ac.post(
                "/api/v1/sessions",
                json={
                    "spec": {
                        "prompt": "demo",
                        "cwd": str(tmp),
                        "plugins": {"profile": "minimal"},
                        "executor": "inprocess",
                        "timeout_s": 5,
                        "tags": ["demo"],
                    },
                    "credentials": {},
                },
                headers=headers,
            )
            r.raise_for_status()
            sid = r.json()["id"]
            print(f"   submitted: {sid}")

            print("→ GET /api/v1/sessions/{id} (polling)")
            for _ in range(40):
                r = await ac.get(f"/api/v1/sessions/{sid}", headers=headers)
                status = r.json()["status"]
                if status == "completed":
                    print(f"   status: {status}")
                    break
                await asyncio.sleep(0.05)
            else:
                print("   timed out waiting for completion")
                return

            frames = r.json().get("frames", [])
            print(f"   frames persisted: {len(frames)}")
            for f in frames:
                print(f"     - seq={f['seq']} type={f['type']}")

            r = await ac.get("/api/v1/sessions", headers=headers)
            print(f"→ GET /api/v1/sessions → {len(r.json()['sessions'])} session(s)")


if __name__ == "__main__":
    asyncio.run(main())
