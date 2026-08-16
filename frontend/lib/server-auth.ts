import { createHmac, timingSafeEqual } from "node:crypto";

const COOKIE_NAME = "sponsorflow_admin";
const SESSION_SECONDS = 12 * 60 * 60;

function secret() {
  const value = process.env.SPONSORFLOW_WEB_SESSION_SECRET;
  if (!value) throw new Error("SPONSORFLOW_WEB_SESSION_SECRET is not configured");
  return value;
}

function signature(payload: string) {
  return createHmac("sha256", secret()).update(payload).digest("base64url");
}

export function createSessionToken() {
  const payload = Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + SESSION_SECONDS })).toString("base64url");
  return `${payload}.${signature(payload)}`;
}

export function verifySessionToken(token?: string) {
  if (!token) return false;
  const [payload, supplied] = token.split(".");
  if (!payload || !supplied) return false;
  const expected = signature(payload);
  const left = Buffer.from(supplied);
  const right = Buffer.from(expected);
  if (left.length !== right.length || !timingSafeEqual(left, right)) return false;
  try {
    const parsed = JSON.parse(Buffer.from(payload, "base64url").toString()) as { exp: number };
    return parsed.exp > Math.floor(Date.now() / 1000);
  } catch {
    return false;
  }
}

export function verifyPassword(password: string) {
  const expected = process.env.SPONSORFLOW_WEB_ADMIN_PASSWORD;
  if (!expected) throw new Error("SPONSORFLOW_WEB_ADMIN_PASSWORD is not configured");
  const left = Buffer.from(password);
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

export const authCookie = {
  name: COOKIE_NAME,
  options: {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict" as const,
    path: "/",
    maxAge: SESSION_SECONDS,
  },
};
