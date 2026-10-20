"""Errors crossing the API boundary contain safe, stable codes, never provider bodies."""


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


class ProviderError(Exception):
    def __init__(self, code: str, retryable: bool, uncertain_charge: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.uncertain_charge = uncertain_charge
