"""PluginAssembler — bridge from PluginManifest to gg-plugins/install.sh.

Public surface:
  - PluginAssembler (Protocol)
  - InstallShellAssembler (concrete impl)
  - InstallReport (frozen dataclass — parsed from install-state.json)
  - PluginInstallError (raised by InstallShellAssembler on subprocess failure)
"""
from gg_relay.session.plugins.install_shell import InstallShellAssembler
from gg_relay.session.plugins.protocol import (
    InstallReport,
    PluginAssembler,
    PluginInstallError,
)

__all__ = [
    "InstallReport",
    "InstallShellAssembler",
    "PluginAssembler",
    "PluginInstallError",
]
