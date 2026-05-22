"""Container-side runner pieces (Plan 3).

- :mod:`wire_runner`  — module entry-point that PID 1 (tini) ``exec``s into.
- :mod:`proxy_client` — :class:`WireCoordinatorProxy`, the container-side
  duck-type stand-in for :class:`HITLCoordinator`.
- :mod:`bridge`       — host-side :class:`WireBridge` that routes EventFrames
  to the local coordinator and pushes ControlFrames back.
"""
