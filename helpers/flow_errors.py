"""Errors a client's TMS order flow (clients/<customer>/tms_create.py) can raise to the worker."""


class FlowNotReady(Exception):
    """There is no (working) TMS order flow for this customer yet."""


class FlowFailed(Exception):
    """The flow ran but did not finish the job. `result` carries whatever it had managed — most
    importantly any TMS order numbers already created — so the worker can keep them and refuse
    to blindly create the same orders a second time."""

    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result = result or {}
