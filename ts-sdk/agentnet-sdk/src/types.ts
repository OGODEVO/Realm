export interface AgentInfo {
  agent_id: string;
  name: string;
  account_id?: string;
  username?: string;
  session_tag?: string;
  capabilities?: string[];
  metadata?: Record<string, unknown>;
  last_seen?: string;
}

export interface AgentMessage {
  message_id: string;
  from_agent: string;
  to_agent: string;
  payload: unknown;
  sent_at: string;
  from_account_id?: string;
  to_account_id?: string;
  from_session_tag?: string;
  ttl_ms?: number;
  expires_at?: string;
  trace_id?: string;
  thread_id?: string;
  parent_message_id?: string;
  kind?: string;
  schema_version?: string;
  idempotency_key?: string;
  reply_to?: string;
  auth?: Record<string, unknown>;
}

export interface DeliveryReceipt {
  message_id: string;
  status: string;
  event_at: string;
  from_account_id?: string;
  from_session_tag?: string;
  to_account_id?: string;
  trace_id?: string;
  thread_id?: string;
  parent_message_id?: string;
  code?: string;
  detail?: string;
}

export interface RegisterResponse {
  account_id: string;
  username: string;
  session_tag: string;
  heartbeat_interval?: number;
  ttl_seconds?: number;
  [key: string]: unknown;
}

export interface SendOptions {
  kind?: string;
  ttlMs?: number;
  traceId?: string;
  threadId?: string;
  parentMessageId?: string;
  idempotencyKey?: string;
  requireDeliveryAck?: boolean;
  retryAttempts?: number;
  receiptTimeoutMs?: number;
}

export interface RequestOptions extends SendOptions {
  timeoutMs?: number;
}

export interface SearchProfilesOptions {
  query?: string;
  capability?: string;
  limit?: number;
  onlineOnly?: boolean;
  timeoutMs?: number;
}

export interface ThreadStatusOptions {
  softLimitTokens?: number;
  hardLimitTokens?: number;
  timeoutMs?: number;
}

export interface ListThreadsOptions {
  participantAccountId?: string;
  participantUsername?: string;
  query?: string;
  limit?: number;
  softLimitTokens?: number;
  hardLimitTokens?: number;
  timeoutMs?: number;
}

export interface ThreadMessagesOptions {
  limit?: number;
  cursor?: string;
  timeoutMs?: number;
}

export interface SearchMessagesOptions {
  threadId?: string;
  fromAccountId?: string;
  toAccountId?: string;
  kind?: string;
  fromTs?: string;
  toTs?: string;
  limit?: number;
  cursor?: string;
  timeoutMs?: number;
}

export interface AgentNetClientOptions {
  natsUrl: string;
  agentId?: string;
  name?: string;
  username?: string;
  accountId?: string;
  capabilities?: string[];
  metadata?: Record<string, unknown>;
  defaultTtlMs?: number;
  defaultRequestTimeoutMs?: number;
  defaultSchemaVersion?: string;
}

export interface AgentNetErrorData {
  code: string;
  detail?: string;
}

export class AgentNetError extends Error {
  code: string;
  detail?: string;

  constructor(message: string, data: AgentNetErrorData) {
    super(message);
    this.name = "AgentNetError";
    this.code = data.code;
    this.detail = data.detail;
  }
}
