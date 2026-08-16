import { NextRequest, NextResponse } from "next/server";

const COOKIE_NAME = "sponsorflow_admin";

function decodeBase64Url(value: string) {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  return Uint8Array.from(atob(base64), (character) => character.charCodeAt(0));
}

async function validSession(token?: string) {
  const secret = process.env.SPONSORFLOW_WEB_SESSION_SECRET;
  if (!token || !secret) return false;
  const [payload, supplied] = token.split(".");
  if (!payload || !supplied) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["verify"],
    );
    const authentic = await crypto.subtle.verify(
      "HMAC",
      key,
      decodeBase64Url(supplied),
      new TextEncoder().encode(payload),
    );
    if (!authentic) return false;
    const data = JSON.parse(new TextDecoder().decode(decodeBase64Url(payload))) as { exp?: number };
    return typeof data.exp === "number" && data.exp > Math.floor(Date.now() / 1000);
  } catch {
    return false;
  }
}

export async function middleware(request: NextRequest) {
  const authenticated = await validSession(request.cookies.get(COOKIE_NAME)?.value);
  if (authenticated) return NextResponse.next();
  const login = new URL("/login", request.url);
  login.searchParams.set("returnTo", `${request.nextUrl.pathname}${request.nextUrl.search}`);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: ["/((?!api/|login|_next/static|_next/image|favicon.ico).*)"],
};
