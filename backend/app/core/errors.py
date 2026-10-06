class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


class LeaseLost(AppError):
    def __init__(self):
        super().__init__("LEASE_LOST", "Worker ownership changed", 409)


class JobCancelled(AppError):
    def __init__(self):
        super().__init__("JOB_CANCELLED", "Research cancelled", 409)
