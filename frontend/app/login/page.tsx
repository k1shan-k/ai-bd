"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!response.ok) {
        const body = await response.json();
        throw new Error(body.detail || "Login failed");
      }
      const requested = new URLSearchParams(window.location.search).get("returnTo");
      const returnTo = requested?.startsWith("/") && !requested.startsWith("//") ? requested : "/providers";
      router.replace(returnTo);
      router.refresh();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="login-wrap">
      <article className="card login-card">
        <div className="brand-mark">SF</div>
        <h1>Admin access</h1>
        <p>Sign in to manage provider credentials, campaigns, conversations, and operations.</p>
        {error && <pre className="error">{error}</pre>}
        <form className="form" onSubmit={submit}>
          <div><label>Admin password</label><input type="password" autoFocus value={password} onChange={(event) => setPassword(event.target.value)} /></div>
          <button disabled={busy || !password}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
      </article>
    </section>
  );
}
