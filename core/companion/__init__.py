"""Companion bridge package: private_companion compatibility surface.

Submodules:
- bridge: CompanionBridge, the duck-typed method surface the companion probes.
- composer: PackageComposer, shared budgeted memory-package packing.
- contracts: byte-identical copies of cross-plugin contract files (MC 1.10.5).
"""

from .bridge import CompanionBridge

__all__ = ["CompanionBridge"]
