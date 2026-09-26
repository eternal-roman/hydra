"""Companion chat personas for Hydra.

Phase 1 wires up chat. Phase 0 spec lives in docs/COMPANION_SPEC.md.
On by default; HYDRA_COMPANION_DISABLED=1 turns it off (see config.py).
"""

__all__ = ["CompanionCoordinator", "mount_companion_routes", "load_all_souls"]

# Lazy to avoid import cost when the subsystem is disabled.
def __getattr__(name):
    if name == "CompanionCoordinator":
        from hydra_companions.coordinator import CompanionCoordinator
        return CompanionCoordinator
    if name == "mount_companion_routes":
        from hydra_companions.ws_handlers import mount_companion_routes
        return mount_companion_routes
    if name == "load_all_souls":
        from hydra_companions.compiler import load_all_souls
        return load_all_souls
    raise AttributeError(name)
