import { NextResponse } from "next/server";
import { authCookie } from "@/lib/server-auth";

export async function POST() {
  const response = NextResponse.json({ authenticated: false });
  response.cookies.set(authCookie.name, "", { ...authCookie.options, maxAge: 0 });
  return response;
}
