import type { AgentMessage } from "./types.js";

export interface CompactionRequiredEvent {
  thread_id: string;
  status: string;
  message_count: number;
  pending_messages: number;
  byte_count: number;
  approx_tokens: number;
  soft_limit_tokens: number;
  hard_limit_tokens: number;
  keep_tail_messages: number;
  latest_checkpoint_end: number;
  requested_at?: string;
  reason?: string;
}

const asInt = (value: unknown, fallback = 0): number => {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.trunc(parsed);
};

const extractPayload = (source: AgentMessage | Record<string, unknown> | unknown): Record<string, unknown> | null => {
  if (!source || typeof source !== "object") {
    return null;
  }
  const map = source as Record<string, unknown>;
  const payload = map.payload;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    return payload as Record<string, unknown>;
  }
  return map;
};

export const isCompactionRequired = (source: AgentMessage | Record<string, unknown> | unknown): boolean => {
  const payload = extractPayload(source);
  if (!payload) {
    return false;
  }
  return String(payload.type ?? "").trim().toLowerCase() === "compaction_required";
};

export const parseCompactionRequired = (
  source: AgentMessage | Record<string, unknown> | unknown,
): CompactionRequiredEvent | null => {
  const payload = extractPayload(source);
  if (!payload || String(payload.type ?? "").trim().toLowerCase() !== "compaction_required") {
    return null;
  }
  const threadId = String(payload.thread_id ?? "").trim();
  if (!threadId) {
    return null;
  }
  return {
    thread_id: threadId,
    status: String(payload.status ?? "needs_compaction"),
    message_count: Math.max(0, asInt(payload.message_count)),
    pending_messages: Math.max(0, asInt(payload.pending_messages)),
    byte_count: Math.max(0, asInt(payload.byte_count)),
    approx_tokens: Math.max(0, asInt(payload.approx_tokens)),
    soft_limit_tokens: Math.max(1, asInt(payload.soft_limit_tokens, 1)),
    hard_limit_tokens: Math.max(1, asInt(payload.hard_limit_tokens, 1)),
    keep_tail_messages: Math.max(0, asInt(payload.keep_tail_messages)),
    latest_checkpoint_end: Math.max(0, asInt(payload.latest_checkpoint_end)),
    requested_at: payload.requested_at ? String(payload.requested_at) : undefined,
    reason: payload.reason ? String(payload.reason) : undefined,
  };
};
