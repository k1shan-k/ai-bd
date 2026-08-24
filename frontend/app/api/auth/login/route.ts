import { createHash, randomUUID } from "node:crypto";
import { isIP } from "node:net";
import { NextRequest, NextResponse } from "next/server";
import { authCookie, createSessionToken, verifyPassword } from "@/lib/server-auth";

type Attempt = { count: number; resetAt: number };
const attempts = new Map<string, Attempt>();
let globalAttempt: Attempt | undefined;
const WINDOW_MS = 15 * 60 * 1000;
const MAX_ATTEMPTS = 5;
const MAX_GLOBAL_ATTEMPTS = 100;
const MAX_TRACKED_CLIENTS = 10_000;

function trustsProxy() {
  return process.env.SPONSORFLOW_TRUST_PROXY?.trim().toLowerCase() === "true";
}

function clientKey(request: NextRequest) {
  if (!trustsProxy()) return "direct-shared";
  const realIp = request.headers.get("x-real-ip")?.trim();
  return realIp && isIP(realIp) ? `ip:${realIp}` : "proxy-unknown";
}

function activeAttempt(attempt: Attempt | undefined, now: number) {
  return attempt && attempt.resetAt > now ? attempt : undefined;
}

function increment(attempt: Attempt | undefined, now: number) {
  const current = activeAttempt(attempt, now);
  return { count: (current?.count || 0) + 1, resetAt: current?.resetAt || now + WINDOW_MS };
}

function clientFingerprint(key: string) {
  return createHash("sha256").update(key).digest("hex").slice(0, 12);
}

function logAuth(
  outcome: "success" | "invalid" | "throttled" | "configuration_error",
  requestId: string,
  key: string,
) {
  const entry = JSON.stringify({
    timestamp: new Date().toISOString(),
    event: "admin_login",
    outcome,
    request_id: requestId,
    client: clientFingerprint(key),
    trusted_proxy: trustsProxy(),
  });
  if (outcome === "success") console.info(entry);
  else console.warn(entry);
}

function jsonResponse(body: object, status: number, requestId: string, headers?: HeadersInit) {
  return NextResponse.json(body, {
    status,
    headers: { "X-Request-ID": requestId, ...headers },
  });
}

export async function POST(request: NextRequest) {
  const now = Date.now();
  const requestId = randomUUID();
  const key = clientKey(request);
  const existing = activeAttempt(attempts.get(key), now);
  const globalExisting = activeAttempt(globalAttempt, now);
  const limited = existing && existing.count >= MAX_ATTEMPTS ? existing :
    globalExisting && globalExisting.count >= MAX_GLOBAL_ATTEMPTS ? globalExisting : undefined;

  if (limited) {
    logAuth("throttled", requestId, key);
    return jsonResponse(
      { detail: "Too many login attempts; try again later" },
      429,
      requestId,
      { "Retry-After": String(Math.ceil((limited.resetAt - now) / 1000)) },
    );
  }
  if (!existing) attempts.delete(key);
  if (!globalExisting) globalAttempt = undefined;

  const body = (await request.json().catch(() => ({}))) as { password?: string };
  let valid = false;
  try {
    valid = Boolean(body.password) && verifyPassword(body.password || "");
  } catch {
    logAuth("configuration_error", requestId, key);
    return jsonResponse({ detail: "Admin authentication is unavailable" }, 503, requestId);
  }

  if (!valid) {
    attempts.set(key, increment(existing, now));
    globalAttempt = increment(globalExisting, now);
    if (attempts.size > MAX_TRACKED_CLIENTS) {
      for (const [candidate, value] of attempts) {
        if (value.resetAt <= now) attempts.delete(candidate);
      }
    }
    logAuth("invalid", requestId, key);
    return jsonResponse({ detail: "Invalid admin password" }, 401, requestId);
  }

  attempts.delete(key);
  logAuth("success", requestId, key);
  const response = jsonResponse({ authenticated: true }, 200, requestId);
  response.cookies.set(authCookie.name, createSessionToken(), authCookie.options);
  return response;
}
