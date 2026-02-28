export const REGISTRY_REGISTER_SUBJECT = "registry.register";
export const REGISTRY_HELLO_SUBJECT = "registry.hello";
export const REGISTRY_GOODBYE_SUBJECT = "registry.goodbye";
export const REGISTRY_LIST_SUBJECT = "registry.list";
export const REGISTRY_RESOLVE_ACCOUNT_SUBJECT = "registry.resolve_account";
export const REGISTRY_SEARCH_SUBJECT = "registry.search";
export const REGISTRY_PROFILE_SUBJECT = "registry.profile";
export const REGISTRY_THREAD_STATUS_SUBJECT = "registry.thread_status";
export const REGISTRY_THREAD_LIST_SUBJECT = "registry.thread_list";
export const REGISTRY_THREAD_MESSAGES_SUBJECT = "registry.thread_messages";
export const REGISTRY_MESSAGE_SEARCH_SUBJECT = "registry.message_search";

export const accountInboxSubject = (accountId: string): string => `account.${accountId}.inbox`;
export const accountReceiptsSubject = (accountId: string): string => `account.${accountId}.receipts`;
export const agentCapabilitySubject = (capability: string): string => `agent.capability.${capability}`;
