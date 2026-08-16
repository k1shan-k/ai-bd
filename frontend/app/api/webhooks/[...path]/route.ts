import { NextRequest, NextResponse } from "next/server";

const ALLOWED_WEBHOOKS = new Set(["ses/events", "whatsapp", "calcom"]);

async function proxyWebhook(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  const webhookPath = path.join("/");
  if (!ALLOWED_WEBHOOKS.has(webhookPath)) {
    return NextResponse.json({ detail: "Unknown provider webhook" }, { status: 404 });
  }

  const base = (process.env.INTERNAL_API_ORIGIN || "http://api:8000").replace(/\/$/, "");
  const target = new URL(`${base}/webhooks/${webhookPath}`);
  request.nextUrl.searchParams.forEach((value, key) => target.searchParams.append(key, value));

  const headers = new Headers();
  for (const name of ["content-type", "x-amz-sns-message-type", "x-hub-signature-256", "x-cal-signature-256"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const method = request.method;
  try {
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
  } catch {
    return NextResponse.json({ detail: "Provider webhook service unavailable" }, { status: 503 });
  }
}

export const GET = proxyWebhook;
export const POST = proxyWebhook;
