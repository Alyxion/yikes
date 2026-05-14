class YikesError(Exception):
    """Base error for yikes runtime failures."""


class BackendUnavailable(YikesError):
    """The requested backend binary or auth context is not available."""


class DriverUnavailable(YikesError):
    """The requested driver cannot run in this environment."""


class BackendRunError(YikesError):
    """The backend process failed."""

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
