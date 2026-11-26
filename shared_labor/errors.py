class LaborError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message, code: str | None = None, status_code: int | None = None, **details):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details

    def to_dict(self):
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class ValidationError(LaborError):
    status_code = 400
    code = "validation_error"


class NotFoundError(LaborError):
    status_code = 404
    code = "not_found"


class PermissionDeniedError(LaborError):
    status_code = 403
    code = "permission_denied"


class ConflictError(LaborError):
    status_code = 409
    code = "conflict"


class UnauthorizedError(LaborError):
    status_code = 401
    code = "unauthorized"


class GatewayFailure(LaborError):
    status_code = 502
    code = "gateway_failure"
