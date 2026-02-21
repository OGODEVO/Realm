"""Subject names used by AgentNet."""

REGISTRY_REGISTER_SUBJECT = "registry.register"
REGISTRY_HELLO_SUBJECT = "registry.hello"
REGISTRY_GOODBYE_SUBJECT = "registry.goodbye"
REGISTRY_LIST_SUBJECT = "registry.list"


def agent_inbox_subject(agent_id: str) -> str:
    return f"agent.{agent_id}.inbox"


def agent_capability_subject(capability: str) -> str:
    return f"agent.capability.{capability}"
