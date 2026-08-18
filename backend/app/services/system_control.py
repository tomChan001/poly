from dataclasses import dataclass


@dataclass(slots=True)
class SystemControl:
    opening_enabled: bool = False
    reason: str = "safe default"

    def disable_opening(self, reason: str) -> None:
        self.opening_enabled = False
        self.reason = reason

    def set_opening(self, enabled: bool, reason: str) -> None:
        self.opening_enabled = enabled
        self.reason = reason
