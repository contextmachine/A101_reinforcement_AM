"""Alternative whole-field solver: the A101_reinforcement experiments (canonical lattice pool +
CP-SAT set cover on a 100 mm raster) behind the v2 task protocol.

The experiment code is vendored under ``vendor/`` unchanged; this package adapts the stored scene
(resolved polygon rows) to its ``Mosaic``/``Engine`` model, runs one N as a set-cover selection,
converts the chosen boxes to wire zones and hands them to the production bar layout and gap
filling. ``rebar_service.alt_worker`` runs it on its own queue as a KEDA ScaledJob.
"""

from .engine import AltSolution, SceneEngine, build_engine, solve_n

__all__ = ["AltSolution", "SceneEngine", "build_engine", "solve_n"]
