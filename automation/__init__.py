"""Event-driven monitoring for SolarGrid AI.

Imports are lazy so pure event-detection utilities remain usable in lightweight
verification environments before the Flask runtime dependencies are installed.
"""

__all__ = ["SolarGridMonitorService", "get_monitor_service"]


def __getattr__(name):
    if name in __all__:
        from .monitor_service import SolarGridMonitorService, get_monitor_service
        return {"SolarGridMonitorService": SolarGridMonitorService, "get_monitor_service": get_monitor_service}[name]
    raise AttributeError(name)
