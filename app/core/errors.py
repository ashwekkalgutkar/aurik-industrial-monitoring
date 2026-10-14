"""Custom application error hierarchy with standardized API error payloads."""

from typing import Any


class AppError(Exception):
    """Base exception for all domain and application errors."""

    def __init__(
        self,
        message: str,
        code: str = "INTERNAL_ERROR",
        status_code: int = 500,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Format the exception into the canonical API error envelope."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class AuthenticationError(AppError):
    """Raised when authentication credentials are missing or invalid (HTTP 401)."""

    def __init__(self, message: str = "Missing or invalid API key.", details: Any = None) -> None:
        super().__init__(
            message=message,
            code="UNAUTHENTICATED",
            status_code=401,
            details=details,
        )


class AuthorizationError(AppError):
    """Raised when API key scope is insufficient for the requested resource (HTTP 403)."""

    def __init__(
        self,
        message: str = "Forbidden: Insufficient API key scope.",
        details: Any = None,
    ) -> None:
        super().__init__(
            message=message,
            code="FORBIDDEN",
            status_code=403,
            details=details,
        )


class NotFoundError(AppError):
    """Raised when a requested resource does not exist (HTTP 404)."""

    def __init__(self, message: str = "Resource not found.", details: Any = None) -> None:
        super().__init__(
            message=message,
            code="NOT_FOUND",
            status_code=404,
            details=details,
        )


class PayloadValidationError(AppError):
    """Raised when request structure violates envelope schemas or type constraints (HTTP 422)."""

    def __init__(self, message: str, details: Any = None, status_code: int = 422) -> None:
        super().__init__(
            message=message,
            code="VALIDATION_ERROR",
            status_code=status_code,
            details=details,
        )


class PayloadTooLargeError(AppError):
    """Raised when incoming payload exceeds MAX_PAYLOAD_BYTES (HTTP 413)."""

    def __init__(
        self, message: str = "Payload exceeds maximum allowed size.", details: Any = None
    ) -> None:
        super().__init__(
            message=message,
            code="PAYLOAD_TOO_LARGE",
            status_code=413,
            details=details,
        )


class ConflictError(AppError):
    """Raised when an operation conflicts with current state (HTTP 409)."""

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(
            message=message,
            code="CONFLICT",
            status_code=409,
            details=details,
        )
