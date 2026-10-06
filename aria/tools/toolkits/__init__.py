"""ARIA v2 toolkits. Each toolkit module exposes register(registry) and SCHEMAS."""
from aria.tools.toolkits import files, system, memory, web


def register_all_toolkit(registry) -> None:
    """Register every toolkit's tools into the registry."""
    files.register(registry)
    system.register(registry)
    memory.register(registry)
    web.register(registry)


__all__ = ["files", "system", "memory", "web", "register_all_toolkit"]
