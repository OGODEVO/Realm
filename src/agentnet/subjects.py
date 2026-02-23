"""Subject names used by AgentNet."""

REGISTRY_REGISTER_SUBJECT = "registry.register"
REGISTRY_HELLO_SUBJECT = "registry.hello"
REGISTRY_GOODBYE_SUBJECT = "registry.goodbye"
REGISTRY_LIST_SUBJECT = "registry.list"
REGISTRY_RESOLVE_ACCOUNT_SUBJECT = "registry.resolve_account"


def account_inbox_subject(account_id: str) -> str:
    return f"account.{account_id}.inbox"


def agent_capability_subject(capability: str) -> str:
    return f"agent.capability.{capability}"
