from backend.app.services.mappings import MappingReviewService
from backend.app.services.rules import InMemoryRuleStore, RuleService


class ApplicationContainer:
    """Owns process-local services; durable repositories can replace stores later."""

    def __init__(self) -> None:
        self.rule_store = InMemoryRuleStore()
        self.rules = RuleService(self.rule_store)
        self.mappings = MappingReviewService(self.rule_store)

