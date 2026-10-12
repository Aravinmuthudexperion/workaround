"""Domain errors, translated to HTTP responses by the web layer."""


class ServiceError(Exception):
    status_code = 400


class NotFound(ServiceError):
    status_code = 404


class PermissionDenied(ServiceError):
    status_code = 403


class ValidationFailed(ServiceError):
    status_code = 422
