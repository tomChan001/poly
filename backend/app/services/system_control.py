from dataclasses import dataclass


@dataclass(slots=True)
class SystemControl:
    opening_enabled: bool = False
    reason: str = "safe default"

    def disable_opening(self, reason: str) -> None:
        self.opening_enabled = False
        self.reason = reason

