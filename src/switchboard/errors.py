"""Safe errors: messages never contain backend bodies or credentials."""


class CapabilityError(Exception):
    def __init__(self, code: str, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint

    def result(self) -> dict:
        from .domains.common import error_result

        result = error_result(self.code, str(self))
        if self.hint:
            result["error"]["hint"] = self.hint
        return result
