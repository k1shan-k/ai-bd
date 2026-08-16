"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";

type Lead = { id: string; full_name: string; email: string; telegram: string; company?: string; sponsor_answer: string; state: string; delivery_state: string; automation_status: string };

export default function Leads() {
  const searchParams = useSearchParams();
  const eventId = searchParams.get("event_id");
  const [leads, setLeads] = useState<Lead[]>([]);
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    api<Lead[]>(`/leads${eventId ? `?event_id=${eventId}` : ""}`).then(setLeads).catch((caught) => setError(caught.message));
  }, [eventId]);
  const states = useMemo(() => [...new Set(leads.map((lead) => lead.state))], [leads]);
  const visible = leads.filter((lead) => {
    const matchesState = !filter || lead.state === filter;
    const haystack = `${lead.full_name} ${lead.email} ${lead.telegram} ${lead.company || ""}`.toLowerCase();
    return matchesState && haystack.includes(query.toLowerCase());
  });
  return <>
    <section className="heading"><div><h1>Lead pipeline</h1><p>Eligibility, outreach, engagement, negotiation, and calls.</p></div><div className="filters"><input placeholder="Search leads" value={query} onChange={(event) => setQuery(event.target.value)} /><select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="">All states</option>{states.map((state) => <option key={state}>{state}</option>)}</select></div></section>
    {error && <p className="error">{error}</p>}
    <section className="pipeline-summary">{states.map((state) => <button className={filter === state ? "active" : ""} onClick={() => setFilter(filter === state ? "" : state)} key={state}><span>{state.replaceAll("_", " ")}</span><strong>{leads.filter((lead) => lead.state === state).length}</strong></button>)}</section>
    <section className="card full table-wrap">
      <table><thead><tr><th>Prospect</th><th>Company</th><th>Interest</th><th>Pipeline</th><th>Delivery</th><th>Automation</th></tr></thead>
      <tbody>{visible.map((lead) => <tr key={lead.id}><td><Link href={`/leads/${lead.id}`}><strong>{lead.full_name}</strong><br/><small>{lead.email} · @{lead.telegram}</small></Link></td><td>{lead.company || "—"}</td><td><span className="pill">{lead.sponsor_answer}</span></td><td>{lead.state.replaceAll("_", " ")}</td><td>{lead.delivery_state.replaceAll("_", " ")}</td><td>{lead.automation_status}</td></tr>)}</tbody></table>
      {visible.length === 0 && <p>No leads match this view. Import a CSV from an event workspace.</p>}
    </section>
  </>;
}
