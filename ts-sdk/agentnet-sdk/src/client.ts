import {
  type ConnectionOptions,
  JSONCodec,
  type Msg,
  type NatsConnection,
  type Subscription,
  connect,
} from "nats";

import {
  REGISTRY_GOODBYE_SUBJECT,
  REGISTRY_HELLO_SUBJECT,
  REGISTRY_LIST_SUBJECT,
  REGISTRY_MESSAGE_SEARCH_SUBJECT,
  REGISTRY_PROFILE_SUBJECT,
  REGISTRY_REGISTER_SUBJECT,
  REGISTRY_RESOLVE_ACCOUNT_SUBJECT,
  REGISTRY_SEARCH_SUBJECT,
  REGISTRY_THREAD_LIST_SUBJECT,
  REGISTRY_THREAD_MESSAGES_SUBJECT,
  REGISTRY_THREAD_STATUS_SUBJECT,
  accountInboxSubject,
  accountReceiptsSubject,
} from "./subjects.js";
import type {
  AgentInfo,
  AgentMessage,
  AgentNetClientOptions,
  AgentNetErrorData,
  DeliveryReceipt,
  ListThreadsOptions,
  RegisterResponse,
  RequestOptions,
  SearchMessagesOptions,
  SearchProfilesOptions,
  SendOptions,
  ThreadMessagesOptions,
  ThreadStatusOptions,
} from "./types.js";
import { AgentNetError } from "./types.js";
import { buildExpiryIso, clamp, newMessageId, normalizeUsername, nowIso, parseRoutingTarget } from "./utils.js";

type ReceiptWaiter = {
  resolve: (receipt: DeliveryReceipt) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
};

type InboxHandler = (
  message: AgentMessage,
  ctx: { reply: (payload: unknown, options?: SendOptions) => Promise<string>; raw: Msg },
) => Promise<void> | void;

export class AgentNetClient {
  private readonly codec = JSONCodec<Record<string, unknown>>();
  private nc: NatsConnection | null = null;
  private heartbeatTimer: NodeJS.Timeout | null = null;
  private receiptsSub: Subscription | null = null;
  private receiptWaiters = new Map<string, ReceiptWaiter>();
  private inboxSubs: Subscription[] = [];

  private readonly opts: Required<
    Pick<AgentNetClientOptions, "natsUrl" | "defaultTtlMs" | "defaultRequestTimeoutMs" | "defaultSchemaVersion">
  > &
    Omit<AgentNetClientOptions, "natsUrl" | "defaultTtlMs" | "defaultRequestTimeoutMs" | "defaultSchemaVersion">;

  private sessionTag: string | null = null;
  private heartbeatIntervalSeconds = 12;

  constructor(options: AgentNetClientOptions) {
    if (!options.natsUrl?.trim()) {
      throw new Error("natsUrl is required");
    }
    this.opts = {
      natsUrl: options.natsUrl,
      agentId: options.agentId,
      name: options.name,
      username: options.username ? normalizeUsername(options.username) : undefined,
      accountId: options.accountId,
      capabilities: options.capabilities ?? [],
      metadata: options.metadata ?? {},
      defaultTtlMs: options.defaultTtlMs ?? 30_000,
      defaultRequestTimeoutMs: options.defaultRequestTimeoutMs ?? 5_000,
      defaultSchemaVersion: options.defaultSchemaVersion ?? "1.1",
    };
  }

  async connect(): Promise<void> {
    if (this.nc && !this.nc.isClosed()) {
      return;
    }

    const connection: ConnectionOptions = {
      servers: this.opts.natsUrl,
      name: `agentnet-ts-${this.opts.agentId ?? "client"}`,
    };

    // Normalize token-only URL auth (nats://<token>@host:port) into explicit token option.
    // nats.js does not treat URL username as auth token for token-auth servers.
    try {
      const parsed = new URL(this.opts.natsUrl);
      if (parsed.username && !parsed.password) {
        const token = decodeURIComponent(parsed.username);
        parsed.username = "";
        parsed.password = "";
        connection.servers = parsed.toString();
        connection.token = token;
      }
    } catch {
      // Keep raw URL if parsing fails.
    }

    this.nc = await connect(connection);
  }

  async start(): Promise<RegisterResponse> {
    await this.connect();
    const result = await this.register();
    this.startHeartbeat();
    await this.ensureReceiptSubscription();
    return result;
  }

  async close(): Promise<void> {
    this.stopHeartbeat();
    if (this.nc && !this.nc.isClosed()) {
      try {
        if (this.sessionTag) {
          await this.publishGoodbye();
        }
      } catch {
        // Ignore goodbye failures on shutdown.
      }
      await this.nc.drain();
    }
    this.nc = null;
    this.sessionTag = null;
    for (const sub of this.inboxSubs) {
      sub.unsubscribe();
    }
    this.inboxSubs = [];
  }

  getAccountId(): string | null {
    return this.opts.accountId ?? null;
  }

  getSessionTag(): string | null {
    return this.sessionTag;
  }

  async register(): Promise<RegisterResponse> {
    const nc = this.requireConnection();
    const payload: Record<string, unknown> = {
      agent_id: this.requireAgentId(),
      name: this.requireName(),
      capabilities: this.opts.capabilities,
      metadata: this.opts.metadata,
    };
    if (this.opts.accountId) {
      payload.account_id = this.opts.accountId;
    }
    if (this.opts.username) {
      payload.username = this.opts.username;
    }

    const data = await this.requestJson(REGISTRY_REGISTER_SUBJECT, payload, this.opts.defaultRequestTimeoutMs);
    const register = this.parseOrThrow<RegisterResponse>(data, "register_failed");
    if (!register.session_tag || !register.account_id) {
      throw new AgentNetError("registry.register missing account_id/session_tag", { code: "register_failed" });
    }
    this.sessionTag = register.session_tag;
    this.opts.accountId = register.account_id;
    this.opts.username = register.username ? normalizeUsername(register.username) : this.opts.username;
    if (typeof register.heartbeat_interval === "number" && Number.isFinite(register.heartbeat_interval)) {
      this.heartbeatIntervalSeconds = Math.max(1, register.heartbeat_interval);
    }
    return register;
  }

  async publishHello(): Promise<void> {
    const nc = this.requireConnection();
    if (!this.sessionTag) {
      return;
    }
    const payload: AgentInfo = {
      agent_id: this.requireAgentId(),
      name: this.requireName(),
      account_id: this.requireAccountId(),
      username: this.opts.username,
      session_tag: this.sessionTag,
      capabilities: this.opts.capabilities,
      metadata: this.opts.metadata,
      last_seen: nowIso(),
    };
    nc.publish(REGISTRY_HELLO_SUBJECT, this.codec.encode(payload as unknown as Record<string, unknown>));
  }

  async publishGoodbye(): Promise<void> {
    const nc = this.requireConnection();
    if (!this.sessionTag) {
      return;
    }
    nc.publish(
      REGISTRY_GOODBYE_SUBJECT,
      this.codec.encode({
        agent_id: this.requireAgentId(),
        session_tag: this.sessionTag,
        seen_at: nowIso(),
      }),
    );
  }

  async listOnlineAgents(timeoutMs = this.opts.defaultRequestTimeoutMs): Promise<AgentInfo[]> {
    const data = await this.requestJson(REGISTRY_LIST_SUBJECT, {}, timeoutMs);
    const agents = (this.parseOrThrow<Record<string, unknown>>(data, "registry_list_failed").agents ?? []) as unknown;
    if (!Array.isArray(agents)) {
      return [];
    }
    return agents.filter((item): item is AgentInfo => typeof item === "object" && item !== null) as AgentInfo[];
  }

  async resolveAccountByUsername(username: string, timeoutMs = this.opts.defaultRequestTimeoutMs): Promise<{ accountId: string; username: string }> {
    const target = normalizeUsername(username);
    if (!target) {
      throw new Error("username is required");
    }
    const data = await this.requestJson(REGISTRY_RESOLVE_ACCOUNT_SUBJECT, { username: target }, timeoutMs);
    const parsed = this.parseOrThrow<Record<string, unknown>>(data, "resolve_account_failed");
    const accountId = String(parsed.account_id ?? "");
    if (!accountId) {
      throw new AgentNetError("registry.resolve_account missing account_id", { code: "resolve_account_failed" });
    }
    const resolvedUsername = String(parsed.username ?? target);
    return { accountId, username: resolvedUsername };
  }

  async searchProfiles(options: SearchProfilesOptions = {}): Promise<Record<string, unknown>[]> {
    const payload = {
      query: String(options.query ?? "").trim(),
      capability: options.capability ? String(options.capability).trim() : null,
      limit: clamp(options.limit ?? 20, 1, 100),
      online_only: Boolean(options.onlineOnly ?? false),
    };
    const data = await this.requestJson(REGISTRY_SEARCH_SUBJECT, payload, options.timeoutMs ?? this.opts.defaultRequestTimeoutMs);
    const parsed = this.parseOrThrow<Record<string, unknown>>(data, "search_failed");
    const results = parsed.results;
    if (!Array.isArray(results)) {
      return [];
    }
    return results.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null);
  }

  async getProfile(input: { accountId?: string; username?: string }, timeoutMs = this.opts.defaultRequestTimeoutMs): Promise<Record<string, unknown>> {
    const payload: Record<string, unknown> = {};
    if (input.accountId?.trim()) {
      payload.account_id = input.accountId.trim();
    }
    if (input.username?.trim()) {
      payload.username = normalizeUsername(input.username);
    }
    if (!payload.account_id && !payload.username) {
      throw new Error("accountId or username is required");
    }
    const data = await this.requestJson(REGISTRY_PROFILE_SUBJECT, payload, timeoutMs);
    const parsed = this.parseOrThrow<Record<string, unknown>>(data, "profile_failed");
    const profile = parsed.profile;
    if (!profile || typeof profile !== "object") {
      throw new AgentNetError("registry.profile missing profile", { code: "profile_failed" });
    }
    return profile as Record<string, unknown>;
  }

  async threadStatus(threadId: string, options: ThreadStatusOptions = {}): Promise<Record<string, unknown>> {
    if (!threadId.trim()) {
      throw new Error("threadId is required");
    }
    const payload: Record<string, unknown> = { thread_id: threadId.trim() };
    if (options.softLimitTokens !== undefined) {
      payload.soft_limit_tokens = Math.max(1, options.softLimitTokens);
    }
    if (options.hardLimitTokens !== undefined) {
      payload.hard_limit_tokens = Math.max(1, options.hardLimitTokens);
    }
    const data = await this.requestJson(
      REGISTRY_THREAD_STATUS_SUBJECT,
      payload,
      options.timeoutMs ?? this.opts.defaultRequestTimeoutMs,
    );
    return this.parseOrThrow<Record<string, unknown>>(data, "thread_status_failed");
  }

  async listThreads(options: ListThreadsOptions = {}): Promise<Record<string, unknown>[]> {
    const payload: Record<string, unknown> = {
      query: String(options.query ?? "").trim(),
      limit: clamp(options.limit ?? 20, 1, 100),
    };
    if (options.participantAccountId?.trim()) {
      payload.participant_account_id = options.participantAccountId.trim();
    }
    if (options.participantUsername?.trim()) {
      payload.participant_username = normalizeUsername(options.participantUsername);
    }
    if (options.softLimitTokens !== undefined) {
      payload.soft_limit_tokens = Math.max(1, options.softLimitTokens);
    }
    if (options.hardLimitTokens !== undefined) {
      payload.hard_limit_tokens = Math.max(1, options.hardLimitTokens);
    }
    const data = await this.requestJson(
      REGISTRY_THREAD_LIST_SUBJECT,
      payload,
      options.timeoutMs ?? this.opts.defaultRequestTimeoutMs,
    );
    const parsed = this.parseOrThrow<Record<string, unknown>>(data, "thread_list_failed");
    const results = parsed.results;
    if (!Array.isArray(results)) {
      return [];
    }
    return results.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null);
  }

  async getThreadMessages(threadId: string, options: ThreadMessagesOptions = {}): Promise<Record<string, unknown>> {
    if (!threadId.trim()) {
      throw new Error("threadId is required");
    }
    const payload: Record<string, unknown> = {
      thread_id: threadId.trim(),
      limit: clamp(options.limit ?? 50, 1, 200),
    };
    if (options.cursor?.trim()) {
      payload.cursor = options.cursor.trim();
    }
    const data = await this.requestJson(
      REGISTRY_THREAD_MESSAGES_SUBJECT,
      payload,
      options.timeoutMs ?? this.opts.defaultRequestTimeoutMs,
    );
    return this.parseOrThrow<Record<string, unknown>>(data, "thread_messages_failed");
  }

  async searchMessages(options: SearchMessagesOptions = {}): Promise<Record<string, unknown>> {
    const payload: Record<string, unknown> = {
      limit: clamp(options.limit ?? 50, 1, 200),
    };
    if (options.threadId?.trim()) {
      payload.thread_id = options.threadId.trim();
    }
    if (options.fromAccountId?.trim()) {
      payload.from_account_id = options.fromAccountId.trim();
    }
    if (options.toAccountId?.trim()) {
      payload.to_account_id = options.toAccountId.trim();
    }
    if (options.kind?.trim()) {
      payload.kind = options.kind.trim();
    }
    if (options.fromTs?.trim()) {
      payload.from_ts = options.fromTs.trim();
    }
    if (options.toTs?.trim()) {
      payload.to_ts = options.toTs.trim();
    }
    if (options.cursor?.trim()) {
      payload.cursor = options.cursor.trim();
    }
    const data = await this.requestJson(
      REGISTRY_MESSAGE_SEARCH_SUBJECT,
      payload,
      options.timeoutMs ?? this.opts.defaultRequestTimeoutMs,
    );
    return this.parseOrThrow<Record<string, unknown>>(data, "message_search_failed");
  }

  async send(to: string, payload: unknown, options: SendOptions = {}): Promise<string> {
    const target = parseRoutingTarget(to);
    if (target.kind === "account") {
      return this.sendToAccount(target.value, payload, options);
    }
    return this.sendToUsername(target.value, payload, options);
  }

  async sendToUsername(username: string, payload: unknown, options: SendOptions = {}): Promise<string> {
    const resolved = await this.resolveAccountByUsername(username, this.opts.defaultRequestTimeoutMs);
    return this.sendToAccount(resolved.accountId, payload, options);
  }

  async sendToAccount(toAccountId: string, payload: unknown, options: SendOptions = {}): Promise<string> {
    const nc = this.requireConnection();
    const accountId = toAccountId.trim();
    if (!accountId) {
      throw new Error("toAccountId is required");
    }
    const envelope = this.buildOutboundEnvelope(accountId, payload, options, options.kind ?? "direct");
    const subject = accountInboxSubject(accountId);
    const requireAck = options.requireDeliveryAck ?? false;
    if (!requireAck) {
      nc.publish(subject, this.codec.encode(envelope as unknown as Record<string, unknown>));
      return envelope.message_id;
    }

    await this.ensureReceiptSubscription();
    const attempts = Math.max(0, options.retryAttempts ?? 2) + 1;
    const timeoutMs = Math.max(200, options.receiptTimeoutMs ?? 1500);

    for (let i = 1; i <= attempts; i += 1) {
      nc.publish(subject, this.codec.encode(envelope as unknown as Record<string, unknown>));
      const receipt = await this.waitForReceipt(envelope.message_id, timeoutMs);
      const status = receipt.status.toLowerCase();
      if (status === "accepted" || status === "processed") {
        return envelope.message_id;
      }
      if (status === "rejected") {
        if ((receipt.code ?? "").toLowerCase() === "duplicate") {
          return envelope.message_id;
        }
        throw new AgentNetError(`delivery rejected: ${receipt.code ?? "rejected"}`, {
          code: "delivery_rejected",
          detail: receipt.detail,
        });
      }
      if (i >= attempts) {
        throw new AgentNetError(`delivery receipt unusable status=${status || "unknown"}`, {
          code: "delivery_ack_unusable",
        });
      }
    }
    throw new AgentNetError("delivery acknowledgement timed out", { code: "delivery_ack_timeout" });
  }

  async request(to: string, payload: unknown, options: RequestOptions = {}): Promise<AgentMessage> {
    const target = parseRoutingTarget(to);
    if (target.kind === "account") {
      return this.requestAccount(target.value, payload, options);
    }
    return this.requestUsername(target.value, payload, options);
  }

  async requestUsername(username: string, payload: unknown, options: RequestOptions = {}): Promise<AgentMessage> {
    const resolved = await this.resolveAccountByUsername(username, this.opts.defaultRequestTimeoutMs);
    return this.requestAccount(resolved.accountId, payload, options);
  }

  async requestAccount(toAccountId: string, payload: unknown, options: RequestOptions = {}): Promise<AgentMessage> {
    const nc = this.requireConnection();
    const accountId = toAccountId.trim();
    if (!accountId) {
      throw new Error("toAccountId is required");
    }
    const envelope = this.buildOutboundEnvelope(accountId, payload, options, options.kind ?? "request");
    const reply = await nc.request(accountInboxSubject(accountId), this.codec.encode(envelope as unknown as Record<string, unknown>), {
      timeout: options.timeoutMs ?? this.opts.defaultRequestTimeoutMs,
    });
    return this.parseIncomingMessage(reply);
  }

  async subscribeInbox(handler: InboxHandler, queueGroup?: string): Promise<void> {
    const nc = this.requireConnection();
    const accountId = this.requireAccountId();
    const sub = nc.subscribe(accountInboxSubject(accountId), queueGroup ? { queue: queueGroup } : undefined);
    this.inboxSubs.push(sub);
    (async () => {
      for await (const msg of sub) {
        const incoming = this.parseIncomingMessage(msg);
        await handler(incoming, {
          raw: msg,
          reply: async (payload: unknown, options: SendOptions = {}): Promise<string> => {
            if (!msg.reply) {
              throw new AgentNetError("incoming message has no reply subject", { code: "missing_reply_to" });
            }
            const replyEnvelope = this.buildOutboundEnvelope(
              incoming.from_account_id ?? incoming.from_agent,
              payload,
              {
                ...options,
                traceId: options.traceId ?? incoming.trace_id ?? incoming.message_id,
                threadId: options.threadId ?? incoming.thread_id,
                parentMessageId: options.parentMessageId ?? incoming.message_id,
              },
              options.kind ?? "reply",
            );
            nc.publish(msg.reply, this.codec.encode(replyEnvelope as unknown as Record<string, unknown>));
            return replyEnvelope.message_id;
          },
        });
      }
    })().catch(() => {
      // Keep subscriber failure isolated from caller.
    });
  }

  private async requestJson(subject: string, payload: Record<string, unknown>, timeoutMs: number): Promise<Record<string, unknown>> {
    const nc = this.requireConnection();
    const reply = await nc.request(subject, this.codec.encode(payload), { timeout: timeoutMs });
    const parsed = this.codec.decode(reply.data) as unknown;
    if (!parsed || typeof parsed !== "object") {
      throw new AgentNetError(`invalid JSON response from ${subject}`, { code: "invalid_response" });
    }
    return parsed as Record<string, unknown>;
  }

  private parseOrThrow<T extends Record<string, unknown>>(data: Record<string, unknown>, defaultCode: string): T {
    const err = data.error;
    if (typeof err === "string" && err.trim()) {
      throw new AgentNetError(err, { code: defaultCode });
    }
    return data as T;
  }

  private parseIncomingMessage(msg: Msg): AgentMessage {
    const parsed = this.codec.decode(msg.data) as unknown;
    if (!parsed || typeof parsed !== "object") {
      throw new AgentNetError("incoming message is not an object", { code: "invalid_message" });
    }
    const envelope = parsed as AgentMessage;
    if (msg.reply) {
      envelope.reply_to = msg.reply;
    }
    return envelope;
  }

  private buildOutboundEnvelope(
    to: string,
    payload: unknown,
    options: SendOptions,
    kind: string,
  ): AgentMessage {
    const messageId = newMessageId();
    const sentAt = nowIso();
    const traceId = options.traceId ?? messageId;
    const threadId = options.threadId ?? `thread_${traceId.toLowerCase()}`;
    const ttlMs = Math.max(1, options.ttlMs ?? this.opts.defaultTtlMs);
    const toAccountId = to.startsWith("acct_") ? to : undefined;
    return {
      message_id: messageId,
      from_agent: this.requireAgentId(),
      to_agent: to,
      payload,
      sent_at: sentAt,
      from_account_id: this.requireAccountId(),
      to_account_id: toAccountId,
      from_session_tag: this.requireSessionTag(),
      ttl_ms: ttlMs,
      expires_at: buildExpiryIso(ttlMs),
      trace_id: traceId,
      thread_id: threadId,
      parent_message_id: options.parentMessageId,
      kind,
      schema_version: this.opts.defaultSchemaVersion,
      idempotency_key: options.idempotencyKey,
    };
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      this.publishHello().catch(() => {
        // keep heartbeat best-effort
      });
    }, this.heartbeatIntervalSeconds * 1000);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private async ensureReceiptSubscription(): Promise<void> {
    if (this.receiptsSub) {
      return;
    }
    const nc = this.requireConnection();
    const accountId = this.requireAccountId();
    const sub = nc.subscribe(accountReceiptsSubject(accountId), { queue: `agentnet-ts-receipts-${accountId}` });
    this.receiptsSub = sub;
    (async () => {
      for await (const msg of sub) {
        const parsed = this.codec.decode(msg.data) as unknown;
        if (!parsed || typeof parsed !== "object") {
          continue;
        }
        const receipt = parsed as DeliveryReceipt;
        if (!receipt.message_id) {
          continue;
        }
        const waiter = this.receiptWaiters.get(receipt.message_id);
        if (!waiter) {
          continue;
        }
        clearTimeout(waiter.timer);
        this.receiptWaiters.delete(receipt.message_id);
        waiter.resolve(receipt);
      }
    })().catch(() => {
      // keep receipt subscriber failures isolated from caller
    });
  }

  private waitForReceipt(messageId: string, timeoutMs: number): Promise<DeliveryReceipt> {
    return new Promise<DeliveryReceipt>((resolve, reject) => {
      const existing = this.receiptWaiters.get(messageId);
      if (existing) {
        clearTimeout(existing.timer);
        this.receiptWaiters.delete(messageId);
      }
      const timer = setTimeout(() => {
        this.receiptWaiters.delete(messageId);
        reject(new AgentNetError(`delivery ack timeout message_id=${messageId}`, { code: "delivery_ack_timeout" }));
      }, timeoutMs);
      this.receiptWaiters.set(messageId, { resolve, reject, timer });
    });
  }

  private requireConnection(): NatsConnection {
    if (!this.nc || this.nc.isClosed()) {
      throw new AgentNetError("not connected", { code: "not_connected" });
    }
    return this.nc;
  }

  private requireSessionTag(): string {
    if (!this.sessionTag) {
      throw new AgentNetError("session not registered", { code: "not_registered" });
    }
    return this.sessionTag;
  }

  private requireAccountId(): string {
    if (!this.opts.accountId) {
      throw new AgentNetError("account not registered", { code: "not_registered" });
    }
    return this.opts.accountId;
  }

  private requireAgentId(): string {
    if (!this.opts.agentId?.trim()) {
      throw new AgentNetError("agentId is required for messaging", { code: "missing_agent_id" });
    }
    return this.opts.agentId;
  }

  private requireName(): string {
    if (!this.opts.name?.trim()) {
      throw new AgentNetError("name is required for registration", { code: "missing_name" });
    }
    return this.opts.name;
  }
}
