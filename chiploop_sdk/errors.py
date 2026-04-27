class ChipLoopError(Exception):
    """Base SDK error."""


class ChipLoopConfigError(ChipLoopError):
    """Raised when SDK configuration is missing or invalid."""


class ChipLoopAPIError(ChipLoopError):
    """Raised when the ChipLoop API returns an error response."""

    def __init__(self, message: str, *, status_code: int | None = None, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text
