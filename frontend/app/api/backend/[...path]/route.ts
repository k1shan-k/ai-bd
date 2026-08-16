import { NextRequest, NextResponse } from "next/server";
import { authCookie, verifySessionToken } from "@/lib/server-auth";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  if (!verifySessionToken(request.cookies.get(authCookie.name)?.value)) {
    return NextResponse.json({ detail: "Admin login required" }, { status: 401 });
  }
  const adminKey = process.env.SPONSORFLOW_ADMIN_API_KEY;
  if (!adminKey) {
    return NextResponse.json({ detail: "Server admin API key is not configured" }, { status: 503 });
  }
  const { path } = await context.params;
  const base = process.env.INTERNAL_API_URL || "http://api:8000/api/v1";
  const target = new URL(`${base.replace(/\/$/, "")}/${path.join("/")}`);
  request.nextUrl.searchParams.forEach((value, key) => target.searchParams.append(key, value));
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);
  headers.set("X-API-Key", adminKey);
  headers.set("X-Actor", "web-admin");
  const method = request.method;
  const response = await fetch(target, {
    method,
    headers,
    body: method === "GET" || method === "HEAD" ? undefined : await request.arrayBuffer(),
    cache: "no-store",
  });
  return new NextResponse(response.body, {
    status: response.status,
    headers: { "Content-Type": response.headers.get("content-type") || "application/json" },
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
