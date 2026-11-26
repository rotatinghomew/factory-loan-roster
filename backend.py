"""Convenience alias for the domain backend."""

from shared_labor import LaborService, Service, SharedLaborService
from shared_labor.errors import ConflictError, LaborError, NotFoundError, PermissionDeniedError, ValidationError
from shared_labor.server import create_server, route

__all__ = [
    "Service",
    "LaborService",
    "SharedLaborService",
    "ConflictError",
    "LaborError",
    "NotFoundError",
    "PermissionDeniedError",
    "ValidationError",
    "create_server",
    "route",
]
