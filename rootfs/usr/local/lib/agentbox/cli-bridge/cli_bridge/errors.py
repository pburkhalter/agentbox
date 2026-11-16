"""Errors in the OpenAI error format, each with its HTTP status."""

from __future__ import annotations


class BridgeError(Exception):
    """An error that is answered as {"error": {...}} with a fixed status code."""

    def __init__(
        self,
        status: int,
        type_: str,
        code: str,
        message: str,
        param: str | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.type = type_
        self.code = code
        self.message = message
        self.param = param
        self.retry_after = retry_after

    def body(self) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": self.type,
                "param": self.param,
                "code": self.code,
            }
        }

    def __repr__(self) -> str:
        return f"BridgeError({self.status}, {self.code!r}, {self.message!r})"


def invalid_request(message: str, param: str | None = None, code: str = "invalid_request") -> BridgeError:
    return BridgeError(400, "invalid_request_error", code, message, param)


def unsupported(message: str, param: str | None = None) -> BridgeError:
    return BridgeError(400, "invalid_request_error", "unsupported_parameter", message, param)


def model_not_found(message: str) -> BridgeError:
    return BridgeError(404, "invalid_request_error", "model_not_found", message, "model")


def unauthorized() -> BridgeError:
    return BridgeError(401, "invalid_request_error", "invalid_api_key", "Missing or invalid bearer token.")


def not_logged_in(message: str) -> BridgeError:
    return BridgeError(401, "authentication_error", "not_logged_in", message)


def forbidden(message: str, code: str = "plan_not_included") -> BridgeError:
    return BridgeError(403, "permission_error", code, message)


def usage_limit(message: str, retry_after: int | None = None, code: str = "usage_limit_reached") -> BridgeError:
    return BridgeError(429, "rate_limit_error", code, message, retry_after=retry_after)


def queue_full(message: str, code: str = "queue_full") -> BridgeError:
    return BridgeError(429, "rate_limit_error", code, message, retry_after=5)


def overloaded(message: str) -> BridgeError:
    return BridgeError(503, "api_error", "overloaded", message, retry_after=30)


def timeout(message: str) -> BridgeError:
    return BridgeError(504, "timeout_error", "cli_timeout", message)


def cli_failed(message: str, code: str = "cli_failed") -> BridgeError:
    return BridgeError(502, "api_error", code, message)


def isolation_violation(message: str) -> BridgeError:
    return BridgeError(502, "api_error", "isolation_violation", message)


def too_large(message: str) -> BridgeError:
    return BridgeError(413, "invalid_request_error", "request_too_large", message)
