"""InstallShellAssembler — shells out to gg-plugins/install.sh."""
from __future__ import annotations

import asyncio
import json
import os
import time
from asyncio.subprocess import PIPE
from pathlib import Path

from gg_relay.session.plugins.protocol import (
    InstallReport,
    PluginInstallError,
)
from gg_relay.session.spec import SessionSpec


class InstallShellAssembler:
    """Concrete PluginAssembler that runs ``<plugins_home>/install.sh``.

    Lifecycle (per call to ``prepare``):
      1. Build argv from ``spec.plugins.to_install_argv(home_dir=install_dir)``
      2. Spawn ``install.sh`` via ``asyncio.create_subprocess_exec`` with
         the process inheriting PATH plus ``spec.plugins.extra_env`` overrides
      3. On non-zero exit: raise PluginInstallError with captured stderr
      4. On zero exit: parse ``<install_dir>/.claude/gg/install-state.json``;
         if missing, raise PluginInstallError (contract violation)
      5. Return InstallReport with assembler-measured duration_ms

    The PATH inheritance is critical: ``install.sh`` execs ``node`` / ``npm``
    via PATH lookup; an empty env would break the installer immediately.
    """

    def __init__(self, plugins_home: Path) -> None:
        install_sh = plugins_home / "install.sh"
        if not install_sh.is_file():
            raise FileNotFoundError(f"install.sh not found under {plugins_home}")
        self._home = plugins_home

    async def prepare(self, spec: SessionSpec, *, install_dir: Path) -> InstallReport:
        install_dir.mkdir(parents=True, exist_ok=True)
        argv = (
            str(self._home / "install.sh"),
            *spec.plugins.to_install_argv(home_dir=str(install_dir)),
        )

        env = os.environ.copy()
        for k, v in spec.plugins.extra_env:
            env[k] = v

        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=PIPE,
            stderr=PIPE,
            cwd=str(self._home),
            env=env,
        )
        _stdout, stderr_bytes = await proc.communicate()
        duration_ms = int((time.monotonic() - t0) * 1000)

        if proc.returncode != 0:
            raise PluginInstallError(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                argv=argv,
            )

        state_path = install_dir / ".claude" / "gg" / "install-state.json"
        if not state_path.is_file():
            raise PluginInstallError(
                returncode=0,
                stderr=(
                    f"install.sh exit 0 but install-state.json missing at {state_path}"
                ),
                argv=argv,
            )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return InstallReport(
            schema_version=state["schemaVersion"],
            profile_id=state.get("profileId"),
            selected_modules=tuple(state.get("selectedModules", ())),
            included_components=tuple(state.get("includedComponents", ())),
            excluded_components=tuple(state.get("excludedComponents", ())),
            install_root=Path(state["installRoot"]),
            installed_at=state["installedAt"],
            duration_ms=duration_ms,
        )
