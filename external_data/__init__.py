"""External public-data ingestion for SolarGrid AI.

Adapters provide provenance-rich context only; they never convert public
weather/reference data into invented grid MW telemetry. Runtime service imports
are lazy to keep quality/adaptor unit tests independent from Flask setup.
"""

__all__ = ["ExternalDataService"]


def __getattr__(name):
    if name == "ExternalDataService":
        from .service import ExternalDataService
        return ExternalDataService
    raise AttributeError(name)
