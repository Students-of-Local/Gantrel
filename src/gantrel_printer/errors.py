"""Errors every adapter raises. Each carries a plain-language hint for the person."""


class PrinterError(Exception):
    """Base error. code is a short stable word scripts can match on."""

    code = "printer"

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message}. {self.hint}".strip() if self.hint else self.message


class Unreachable(PrinterError):
    code = "unreachable"


class AuthFailed(PrinterError):
    code = "auth"


class Refused(PrinterError):
    """The printer, or this tool, refused the action (busy, wrong state, bad file)."""

    code = "refused"


class NotFound(Refused):
    """HTTP 404 or the printer's equivalent: the thing asked about does not exist."""

    code = "not_found"


class ProtocolError(PrinterError):
    code = "protocol"


class Unsupported(PrinterError):
    code = "unsupported"


class SafetyError(PrinterError):
    """A safety rule blocked the action. Never retry around one of these."""

    code = "safety"


class ConfigError(PrinterError):
    code = "config"


class NotLocal(Refused):
    """A host that must be on this machine or the local network resolved somewhere else."""

    code = "not_local"
