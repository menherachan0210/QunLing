class NetworkError(Exception):
    """A fixed, safe message that may be shown to the user or model."""

    def __init__(self, message, *, code="web_operation_failed", retryable=False,
                 next_action="inspect_error", attempts=1):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.next_action = next_action
        self.attempts = attempts

    def result(self):
        return {
            "status": "error", "error": str(self), "code": self.code,
            "retryable": self.retryable, "next_action": self.next_action,
            "attempts": self.attempts,
        }
