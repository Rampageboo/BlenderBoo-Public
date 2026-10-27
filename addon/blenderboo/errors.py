"""Public error types used by toolkit tools."""


class ToolkitError(Exception):
    """Base class for errors that callers can act on."""


class InvalidParamsError(ToolkitError):
    """Raised when tool parameters are missing or invalid."""


class InvalidStateError(ToolkitError):
    """Raised when the current Blender state cannot support an operation."""


class StaleStateError(ToolkitError):
    """Raised when a mutator's expected document state is no longer current."""


class LockedError(ToolkitError):
    """Raised when another client holds the active document lease."""

    def __init__(self, message: str, data: dict):
        super().__init__(message)
        self.data = data


class NotFoundError(ToolkitError):
    """Raised when a requested Blender resource does not exist."""


class ConfirmationRequiredError(ToolkitError):
    """Raised when a destructive operation lacks explicit confirmation."""

    def __init__(self, message: str, data: dict | None = None):
        super().__init__(message)
        if data is not None:
            self.data = data


class NameConflictError(ToolkitError):
    """Raised when an exact datablock name is already owned by another item."""

    def __init__(self, message: str, data: dict):
        super().__init__(message)
        self.data = data


class MutationApplyError(ToolkitError):
    """An apply-time mutation failure that retains the partial transaction journal."""

    def __init__(self, message: str, data: dict):
        super().__init__(message)
        self.data = data
