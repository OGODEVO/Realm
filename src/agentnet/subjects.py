"""Subject names used by AgentNet."""

REGISTRY_REGISTER_SUBJECT = "registry.register"
REGISTRY_HELLO_SUBJECT = "registry.hello"
REGISTRY_GOODBYE_SUBJECT = "registry.goodbye"
REGISTRY_LIST_SUBJECT = "registry.list"
REGISTRY_RESOLVE_ACCOUNT_SUBJECT = "registry.resolve_account"
REGISTRY_RESOLVE_KEY_SUBJECT = "registry.resolve_key"
REGISTRY_SEARCH_SUBJECT = "registry.search"
REGISTRY_PROFILE_SUBJECT = "registry.profile"
REGISTRY_THREAD_STATUS_SUBJECT = "registry.thread_status"
REGISTRY_THREAD_LIST_SUBJECT = "registry.thread_list"
REGISTRY_THREAD_MESSAGES_SUBJECT = "registry.thread_messages"
REGISTRY_MESSAGE_SEARCH_SUBJECT = "registry.message_search"
REGISTRY_METRICS_SUBJECT = "registry.metrics"


def account_inbox_subject(account_id: str) -> str:
    return f"account.{account_id}.inbox"


def account_receipts_subject(account_id: str) -> str:
    return f"account.{account_id}.receipts"


def agent_capability_subject(capability: str) -> str:
    return f"agent.capability.{capability}"
