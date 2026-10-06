class AskHumanError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HumanTimeout(AskHumanError):
    def __init__(self, request_id: str, *, expired: bool = False):
        self.request_id = request_id
        self.expired = expired
        super().__init__(f"Request {request_id} {'expired' if expired else 'is still pending'}")


class HumanCancelled(AskHumanError):
    def __init__(self, request_id: str):
        self.request_id = request_id
        super().__init__(f"Request {request_id} was cancelled")


class HumanDeliveryError(AskHumanError):
    def __init__(self, request_id: str, detail: str):
        self.request_id = request_id
        super().__init__(f"Request {request_id}: {detail}")


class ChannelConfigurationError(AskHumanError):
    """Safe setup diagnostics, without provider payloads or credentials."""
