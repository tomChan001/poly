from backend.app.services.mappings import MappingReviewService
from backend.app.services.notifications import InMemoryOutbox, NotificationService
from backend.app.services.opportunities import InMemoryOpportunityStore
from backend.app.services.rules import InMemoryRuleStore, RuleService
from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput
from backend.app.services.system_control import SystemControl


class ApplicationContainer:
    """Owns process-local services; durable repositories can replace stores later."""

    def __init__(self) -> None:
        self.rule_store = InMemoryRuleStore()
        self.rules = RuleService(self.rule_store)
        self.mappings = MappingReviewService(self.rule_store)
        self.opportunities = InMemoryOpportunityStore()
        self.risk_policies = InMemoryRiskPolicyStore()
        self.risk_policies.create(RiskPolicyInput.defaults())
        self.outbox = InMemoryOutbox()
        self.notifications = NotificationService(self.outbox)
        self.system_control = SystemControl()
