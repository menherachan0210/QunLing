class VisionError(Exception):
    """Safe, user-readable diagnostics without private source details."""


class ImageReadError(VisionError):
    """Structured diagnostics containing only fixed codes and opaque image refs."""

    def __init__(
        self, message, *, stage, code, failed_refs=(), retryable=False, failures=()
    ):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.failed_refs = tuple(failed_refs)
        self.retryable = retryable
        self.failures = tuple(failures)

    def result(self):
        result = {
            "status": "error",
            "error": str(self),
            "stage": self.stage,
            "code": self.code,
            "failed_refs": list(self.failed_refs),
            "retryable": self.retryable,
        }
        if self.failures:
            result["failures"] = list(self.failures)
        return result
