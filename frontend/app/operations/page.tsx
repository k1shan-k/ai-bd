"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Analytics = {
  pipeline: Record<string, number>;
  messages: Record<string, number>;
  rates: { engaged_or_better: number; total_leads: number; engagement_percent: number };
  telegram_quota: { date: string; reserved: number; limit: number };
  pending_actions: number;
  pending_outbox: number;
  suppressed_identities: number;
};
type Provider = { provider: string; configured: boolean; mode: string; details: Record<string, unknown> };
type Suppression = { id: string; contact_id?: string; identity_type: string; identity_value: string; scope: string; reason: string; source: string; created_at: string };
type Action = { id: string; lead_id: string; type: string; channel: string; due_at: string; status: string; cancelled_reason?: string };
type Audit = { id: string; actor_type: string; actor_id: string; action: string; resource_type: string; resource_id: string; data: object; created_at: string };

export default function Operations() {
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [suppressions, setSuppressions] = useState<Suppression[]>([]);
  const [actions, setActions] = useState<Action[]>([]);
  const [audits, setAudits] = useState<Audit[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [metricData, providerData, suppressionData, actionData, auditData] = await Promise.all([
        api<Analytics>("/analytics/overview"),
        api<Provider[]>("/providers/checks"),
        api<Suppression[]>("/operations/suppressions"),
        api<Action[]>("/operations/actions?limit=50"),
        api<Audit[]>("/operations/audit?limit=50"),
      ]);
      setAnalytics(metricData); setProviders(providerData); setSuppressions(suppressionData);
      setActions(actionData); setAudits(auditData); setError("");
    } catch (caught) { setError((caught as Error).message); }
  }
  useEffect(() => { void load(); }, []);

  async function runWorker() {
    setBusy(true);
    try {
      setNotice(JSON.stringify(await api("/worker/run-due", { method: "POST", body: JSON.stringify({ limit: 100 }) }), null, 2));
      await load();
    } catch (caught) { setError((caught as Error).message); } finally { setBusy(false); }
  }
  async function unsuppress(contactId?: string) {
    if (!contactId) return;
    setBusy(true);
    try {
      setNotice(JSON.stringify(await api(`/operations/suppressions/contact/${contactId}`, { method: "DELETE" }), null, 2));
      await load();
    } catch (caught) { setError((caught as Error).message); } finally { setBusy(false); }
  }

  return <>
    <section className="heading"><div><h1>Operations control room</h1><p>Provider readiness, queues, quota, suppression, policy activity, and audit records.</p></div><button disabled={busy} onClick={runWorker}>Process due actions</button></section>
    {error && <pre className="error">{error}</pre>}{notice && <pre className="success">{notice}</pre>}
    <section className="grid">
      <article className="card"><div className="label">Engagement</div><div className="metric">{analytics?.rates.engagement_percent || 0}%</div><p>{analytics?.rates.engaged_or_better || 0} engaged or better from {analytics?.rates.total_leads || 0} leads.</p></article>
      <article className="card"><div className="label">Telegram new contacts</div><div className="metric">{analytics?.telegram_quota.reserved || 0}<small> / {analytics?.telegram_quota.limit || 20}</small></div><p>Account-wide quota ledger for {analytics?.telegram_quota.date || "today"}.</p></article>
      <article className="card"><div className="label">Pending work</div><div className="metric">{analytics?.pending_actions || 0}</div><p>{analytics?.pending_outbox || 0} provider sends currently in the outbox.</p></article>

      <article className="card full"><h2>Provider validation</h2><div className="provider-grid">{providers.map((provider) => <div key={provider.provider}><div className="label">{provider.provider}</div><p><span className={`status-dot ${provider.configured ? "" : "off"}`} />{provider.configured ? "Ready" : "Disabled"} · {provider.mode}</p><small>{String(provider.details.reason || provider.details.transport || "")}</small></div>)}</div></article>

      <article className="card wide"><h2>Scheduled actions</h2>{actions.length === 0 && <p>No scheduled actions.</p>}{actions.map((action) => <div className="row" key={action.id}><div><Link href={`/leads/${action.lead_id}`}><strong>{action.type}</strong></Link><p style={{ margin: 0 }}>{action.channel} · {new Date(action.due_at).toLocaleString()}</p>{action.cancelled_reason && <small>{action.cancelled_reason}</small>}</div><span className={`pill ${action.status === "cancelled" ? "warn" : "gray"}`}>{action.status}</span></div>)}</article>
      <article className="card"><h2>Pipeline</h2>{Object.entries(analytics?.pipeline || {}).map(([state, count]) => <div className="row" key={state}><span>{state.replaceAll("_", " ")}</span><strong>{count}</strong></div>)}</article>

      <article className="card full"><h2>Global suppression ledger</h2><p>Removing suppression is admin-only and does not silently restart automation.</p>{suppressions.length === 0 && <p>No suppressed identities.</p>}
        <table><thead><tr><th>Identity</th><th>Reason</th><th>Source</th><th>Date</th><th /></tr></thead><tbody>{suppressions.map((entry) => <tr key={entry.id}><td><strong>{entry.identity_type}</strong><br/><small>{entry.identity_value}</small></td><td>{entry.reason}</td><td>{entry.source}</td><td>{new Date(entry.created_at).toLocaleString()}</td><td><button className="danger" disabled={busy || !entry.contact_id} onClick={() => unsuppress(entry.contact_id)}>Remove contact suppression</button></td></tr>)}</tbody></table>
      </article>

      <article className="card full"><h2>Immutable operator audit</h2><table><thead><tr><th>Action</th><th>Actor</th><th>Resource</th><th>Details</th><th>Time</th></tr></thead><tbody>{audits.map((entry) => <tr key={entry.id}><td><strong>{entry.action}</strong></td><td>{entry.actor_id}<br/><small>{entry.actor_type}</small></td><td>{entry.resource_type}<br/><small>{entry.resource_id}</small></td><td><code>{JSON.stringify(entry.data)}</code></td><td>{new Date(entry.created_at).toLocaleString()}</td></tr>)}</tbody></table></article>
    </section>
  </>;
}
