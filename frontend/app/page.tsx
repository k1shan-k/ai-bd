"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";

type EventRecord = { id: string; slug: string; name: string; status: string; starts_at?: string };
type Analytics = {
  pipeline: Record<string, number>;
  messages: Record<string, number>;
  telegram_quota: { reserved: number; limit: number; date: string };
  pending_actions: number;
  suppressed_identities: number;
};
type Provider = { provider: string; configured: boolean; mode: string; details: Record<string, unknown> };

export default function Overview() {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");

  async function refresh() {
    try {
      const [eventData, analyticsData, providerData] = await Promise.all([
        api<EventRecord[]>("/events"), api<Analytics>("/analytics/overview"), api<Provider[]>("/providers/checks"),
      ]);
      setEvents(eventData); setAnalytics(analyticsData); setProviders(providerData); setError("");
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { void refresh(); }, []);

  async function createEvent(event: FormEvent) {
    event.preventDefault();
    try {
      await api("/events", { method: "POST", body: JSON.stringify({ name, slug, timezone: "UTC" }) });
      setName(""); setSlug(""); await refresh();
    } catch (e) { setError((e as Error).message); }
  }

  const total = analytics ? Object.values(analytics.pipeline).reduce((a, b) => a + b, 0) : 0;
  const qualified = (analytics?.pipeline.qualified || 0) + (analytics?.pipeline.call_booked || 0);
  return <>
    <section className="heading">
      <div><h1>Sponsorship operations</h1><p>One controlled timeline from registration to booked call.</p></div>
      <Link className="button" href="/leads">Open pipeline</Link>
    </section>
    {error && <p className="error">{error}</p>}
    <section className="grid">
      <article className="card"><div className="label">Eligible leads</div><div className="metric">{total}</div><p>Across all active and draft events</p></article>
      <article className="card"><div className="label">Qualified / booked</div><div className="metric">{qualified}</div><p>Ready for a sponsorship conversation</p></article>
      <article className="card"><div className="label">Telegram today</div><div className="metric">{analytics?.telegram_quota.reserved || 0}<small> / {analytics?.telegram_quota.limit || 20}</small></div><p>Transactionally reserved new contacts</p></article>

      <article className="card wide">
        <h2>Events</h2>
        {events.length === 0 && <p>No events yet. Create the first workspace.</p>}
        {events.map(item => <Link className="row" href={`/events/${item.id}`} key={item.id}>
          <div><strong>{item.name}</strong><p style={{ margin: "3px 0 0" }}>{item.slug}</p></div><span className="pill">{item.status}</span>
        </Link>)}
      </article>
      <article className="card">
        <h2>Create event</h2>
        <form className="form" onSubmit={createEvent}>
          <div><label>Event name</label><input value={name} onChange={e => { setName(e.target.value); if (!slug) setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")); }} required /></div>
          <div><label>Slug</label><input value={slug} onChange={e => setSlug(e.target.value)} required /></div>
          <button>Create workspace</button>
        </form>
      </article>
      <article className="card full">
        <h2>Provider validation harness</h2>
        <div className="grid">
          {providers.map(provider => <div key={provider.provider} style={{ gridColumn: "span 3" }}>
            <div className="label">{provider.provider}</div><p><span className="status-dot" />{provider.configured ? "Configured" : "Needs configuration"} · {provider.mode}</p>
          </div>)}
        </div>
      </article>
    </section>
  </>;
}
