import { randomUUID } from "node:crypto";

export const nowIso = (): string => new Date().toISOString();

export const buildExpiryIso = (ttlMs: number): string => new Date(Date.now() + Math.max(1, ttlMs)).toISOString();

export const newMessageId = (): string => randomUUID().replaceAll("-", "");

export const normalizeUsername = (value: string): string => value.trim().toLowerCase().replace(/^@/, "");

export const parseRoutingTarget = (to: string): { kind: "account" | "username"; value: string } => {
  const raw = to.trim();
  if (!raw) {
    throw new Error("routing target is required");
  }
  const lower = raw.toLowerCase();
  if (lower.startsWith("account:")) {
    const value = raw.slice("account:".length).trim();
    if (!value) {
      throw new Error("account target is empty");
    }
    return { kind: "account", value };
  }
  if (raw.startsWith("acct_")) {
    return { kind: "account", value: raw };
  }
  if (lower.startsWith("username:")) {
    const value = normalizeUsername(raw.slice("username:".length));
    if (!value) {
      throw new Error("username target is empty");
    }
    return { kind: "username", value };
  }
  return { kind: "username", value: normalizeUsername(raw) };
};

export const clamp = (value: number, min: number, max: number): number => Math.min(max, Math.max(min, value));
