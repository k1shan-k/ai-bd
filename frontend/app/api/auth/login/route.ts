import { NextRequest, NextResponse } from "next/server";
import { authCookie, createSessionToken, verifyPassword } from "@/lib/server-auth";

type Attempt = { count: number; resetAt: number };
const attempts = new Map<string, Attempt>();
const WINDOW_MS = 15 * 60 * 1000;
const MAX_ATTEMPTS = 5;

function clientKey(request: NextRequest) {
  const forwarded = request.headers.get("x-forwarded-for")?.split(",").at(-1)?.trim();
  return forwarded || "unknown";
}

export async function POST(request: NextRequest) {
  const now = Date.now();
  const key = clientKey(request);
  const existing = attempts.get(key);
  if (existing && existing.resetAt > now && existing.count >= MAX_ATTEMPTS) {
    return NextResponse.json(
      { detail: "Too many login attempts; try again later" },
      { status: 429, headers: { "Retry-After": String(Math.ceil((existing.resetAt - now) / 1000)) } },
    );
  }
  if (existing && existing.resetAt <= now) attempts.delete(key);

  const body = (await request.json().catch(() => ({}))) as { password?: string };
  if (!body.password || !verifyPassword(body.password)) {
    const current = attempts.get(key);
    attempts.set(key, {
      count: (current?.count || 0) + 1,
      resetAt: current?.resetAt || now + WINDOW_MS,
    });
    if (attempts.size > 10_000) {
      for (const [candidate, value] of attempts) {
        if (value.resetAt <= now) attempts.delete(candidate);
      }
    }
    return NextResponse.json({ detail: "Invalid admin password" }, { status: 401 });
  }

  attempts.delete(key);
  const response = NextResponse.json({ authenticated: true });
  response.cookies.set(authCookie.name, createSessionToken(), authCookie.options);
  return response;
}
