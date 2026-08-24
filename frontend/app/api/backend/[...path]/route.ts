import { randomUUID } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { authCookie, verifySessionToken } from "@/lib/server-auth";

const MAX_BODY_BYTES = 25 * 1024 * 1024;

function logProxy(
  method: string,
  path: string,
  status: number,
  startedAt: number,
  requestId: string,
  error?: string,
) {
  const entry = JSON.stringify({
    timestamp: new Date().toISOString(),
    event: "bff_proxy",
    method,
    path,
    status,
    duration_ms: Date.now() - startedAt,
    request_id: requestId,
    ...(error ? { error } : {}),
  });
  if (status >= 500) console.error(entry);
  else if (status >= 400) console.warn(entry);
  else console.info(entry);
}

function errorResponse(detail: string, status: number, requestId: string) {
  return NextResponse.json(
    { detail },
    { status, headers: { "X-Request-ID": requestId } },
  );
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const startedAt = Date.now();
  const requestId = randomUUID();
  const { path } = await context.params;
  const safePath = `/${path.join("/")}`;
  const method = request.method;

  if (!verifySessionToken(request.cookies.get(authCookie.name)?.value)) {
    logProxy(method, safePath, 401, startedAt, requestId);
    return errorResponse("Admin login required", 401, requestId);
  }
  const adminKey = process.env.SPONSORFLOW_ADMIN_API_KEY;
  if (!adminKey) {
    logProxy(method, safePath, 503, startedAt, requestId, "AdminKeyUnavailable");
    return errorResponse("Server admin API key is not configured", 503, requestId);
  }

  const declaredLength = Number(request.headers.get("content-length") || 0);
  if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) {
    logProxy(method, safePath, 413, startedAt, requestId);
    return errorResponse("Request body is too large", 413, requestId);
  }

  try {
    const base = process.env.INTERNAL_API_URL || "http://api:8000/api/v1";
    const target = new URL(`${base.replace(/\/$/, "")}/${path.join("/")}`);
    request.nextUrl.searchParams.forEach((value, key) => target.searchParams.append(key, value));
    const headers = new Headers();
    const contentType = request.headers.get("content-type");
    if (contentType) headers.set("Content-Type", contentType);
    headers.set("X-API-Key", adminKey);
    headers.set("X-Actor", "web-admin");
    headers.set("X-Request-ID", requestId);

    let body: ArrayBuffer | undefined;
    if (method !== "GET" && method !== "HEAD") {
      body = await request.arrayBuffer();
      if (body.byteLength > MAX_BODY_BYTES) {
        logProxy(method, safePath, 413, startedAt, requestId);
        return errorResponse("Request body is too large", 413, requestId);
      }
    }

    const response = await fetch(target, { method, headers, body, cache: "no-store" });
    logProxy(method, safePath, response.status, startedAt, requestId);
    return new NextResponse(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("content-type") || "application/json",
        "X-Request-ID": requestId,
      },
    });
  } catch (caught) {
    const error = caught instanceof Error ? caught.name : "UnknownError";
    logProxy(method, safePath, 502, startedAt, requestId, error);
    return errorResponse("Backend service is unavailable", 502, requestId);
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
