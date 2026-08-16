"use client";

import { FormEvent, use, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Campaign = { id: string; name: string; status: string };
type Message = { id: string; direction: string; channel: string; body: string; created_at: string; provenance: Record<string, unknown> };
type Timeline = { id: string; type: string; data: Record<string, unknown>; created_at: string };
type Schedule = { id: string; type: string; channel: string; due_at: string; status: string; cancelled_reason?: string };
type Research = { summary: string; confidence: string; facts: Array<{ claim?: string; source_url?: string; confidence?: number }>; fit_angles: string[] };
type Offer = { id: string; package_id: string; list_price: string; offered_price: string; discount_percent: string; perks: string[]; status: string };
type Meeting = { id: string; starts_at: string; timezone: string; status: string; booking_url: string };
type Detail = {
  lead: { id: string; full_name: string; email: string; telegram: string; whatsapp?: string; state: string; automation_status: string; company?: string; role?: string };
  event_id: string;
  campaign_id?: string;
  context_version_id?: string;
  messages: Message[];
  timeline: Timeline[];
  schedules: Schedule[];
  research: Research[];
  offers: Offer[];
  meetings: Meeting[];
};

export default function LeadDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [data, setData] = useState<Detail | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reply, setReply] = useState("");
  const [channel, setChannel] = useState("email");
  const [offer, setOffer] = useState({ package_id: "gold", offered_price: "9000", perks: "booth", rationale: "Within approved cap" });
  const [meeting, setMeeting] = useState({ starts_at: "", timezone: "UTC" });
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const detail = await api<Detail>(`/leads/${id}`);
      setData(detail);
      setCampaigns(await api<Campaign[]>(`/events/${detail.event_id}/campaigns`));
      setError("");
    } catch (caught) {
      setError((caught as Error).message);
    }
  }
  useEffect(() => { void load(); }, [id]);

  async function request(path: string, body: object = {}, method = "POST") {
    setBusy(true);
    setError("");
    try {
      const response = await api<unknown>(`/leads/${id}${path}`, { method, body: JSON.stringify(body) });
      setNotice(JSON.stringify(response, null, 2));
      await load();
      return true;
    } catch (caught) {
      setError((caught as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function manual(event: FormEvent) {
    event.preventDefault();
    if (await request("/manual-reply", { channel, body: reply })) setReply("");
  }
  async function proposeOffer(event: FormEvent) {
    event.preventDefault();
    await request("/offers", {
      package_id: offer.package_id,
      offered_price: offer.offered_price,
      perks: offer.perks.split(",").map((value) => value.trim()).filter(Boolean),
      rationale: offer.rationale,
      send_immediately: true,
    });
  }
  async function bookMeeting(event: FormEvent) {
    event.preventDefault();
    await request("/meetings", { starts_at: new Date(meeting.starts_at).toISOString(), timezone: meeting.timezone, provider: "fake" });
  }

  if (!data) return <p>{error || "Loading…"}</p>;
  const activeCampaign = campaigns.find((campaign) => campaign.status === "active");
  return <>
    <section className="heading">
      <div><h1>{data.lead.full_name}</h1><p>{data.lead.role || "Registrant"} at {data.lead.company || "unknown company"} · {data.lead.email} · @{data.lead.telegram}</p></div>
      <div><span className="pill">{data.lead.state}</span>{" "}<span className="pill gray">{data.lead.automation_status}</span></div>
    </section>
    {error && <pre className="error">{error}</pre>}
    {notice && <pre className="success">{notice}</pre>}
    <section className="grid">
      <article className="card wide"><h2>Unified conversation</h2>
        {data.messages.length === 0 && <p>No messages yet.</p>}
        {data.messages.map((message) => <div className={`message ${message.direction}`} key={message.id}>
          <span className="pill gray">{message.channel} · {message.direction}</span>
          <p>{message.body}</p><small>{new Date(message.created_at).toLocaleString()}</small>
        </div>)}
        <form className="form" onSubmit={manual} style={{ marginTop: 18 }}>
          <div className="form two"><select value={channel} onChange={(event) => setChannel(event.target.value)}><option>email</option><option>telegram</option><option>whatsapp</option></select><input value={reply} onChange={(event) => setReply(event.target.value)} placeholder="Manual team reply" required /></div>
          <button disabled={busy}>Take over and queue reply</button>
        </form>
      </article>

      <article className="card"><h2>Automation controls</h2><div className="form">
        <button disabled={busy} onClick={() => request("/research", { provider: "fake" })}>Run cited research</button>
        {!data.campaign_id && <button disabled={busy || !activeCampaign} onClick={() => activeCampaign && request("/workflow/start", { campaign_id: activeCampaign.id })}>Start active campaign</button>}
        <button className="secondary" disabled={busy} onClick={() => request("", { automation_status: data.lead.automation_status === "paused" ? "active" : "paused" }, "PATCH")}>Pause / resume</button>
        <button className="secondary" disabled={busy} onClick={async () => { setBusy(true); try { setNotice(JSON.stringify(await api("/worker/run-due", { method: "POST", body: JSON.stringify({}) }), null, 2)); await load(); } catch (caught) { setError((caught as Error).message); } finally { setBusy(false); } }}>Process due actions</button>
        <button className="danger" disabled={busy} onClick={() => request("/suppress", { reason: "manual_block" })}>Globally suppress</button>
      </div></article>

      <article className="card wide"><h2>Research and source claims</h2>
        {data.research.length === 0 && <p>Research has not run yet.</p>}
        {data.research.map((report, index) => <div key={index}><p>{report.summary}</p><span className="pill">confidence {report.confidence}</span><h3 style={{ marginTop: 16 }}>Personalization angles</h3>{report.fit_angles.map((angle) => <p key={angle}>• {angle}</p>)}<h3>Claims</h3>{report.facts.map((fact, factIndex) => <div className="row" key={factIndex}><span>{fact.claim || "Claim"}</span><small>{fact.source_url || "No source"}</small></div>)}</div>)}
      </article>
      <article className="card"><h2>Scheduled actions</h2>{data.schedules.length === 0 && <p>No scheduled actions.</p>}{data.schedules.map((schedule) => <div className="row" key={schedule.id}><div><strong>{schedule.type}</strong><p style={{ margin: 0 }}>{schedule.channel} · {new Date(schedule.due_at).toLocaleString()}</p>{schedule.cancelled_reason && <small>{schedule.cancelled_reason}</small>}</div><span className={`pill ${schedule.status === "cancelled" ? "warn" : "gray"}`}>{schedule.status}</span></div>)}</article>

      <article className="card"><h2>Constrained offer</h2><form className="form" onSubmit={proposeOffer}>
        <div><label>Package ID</label><input value={offer.package_id} onChange={(event) => setOffer({ ...offer, package_id: event.target.value })} /></div>
        <div><label>Offered price</label><input type="number" min="0" value={offer.offered_price} onChange={(event) => setOffer({ ...offer, offered_price: event.target.value })} /></div>
        <div><label>Perks, comma separated</label><input value={offer.perks} onChange={(event) => setOffer({ ...offer, perks: event.target.value })} /></div>
        <div><label>Rationale</label><input value={offer.rationale} onChange={(event) => setOffer({ ...offer, rationale: event.target.value })} /></div>
        <button disabled={busy || !data.context_version_id}>Validate, reserve, and queue</button>
      </form>{data.offers.map((item) => <div className="row" key={item.id}><div><strong>{item.package_id}: {item.offered_price}</strong><p style={{ margin: 0 }}>{item.discount_percent}% discount</p></div><span className="pill">{item.status}</span></div>)}</article>

      <article className="card"><h2>Book call</h2><form className="form" onSubmit={bookMeeting}>
        <div><label>Start time</label><input type="datetime-local" value={meeting.starts_at} onChange={(event) => setMeeting({ ...meeting, starts_at: event.target.value })} required /></div>
        <div><label>Timezone</label><input value={meeting.timezone} onChange={(event) => setMeeting({ ...meeting, timezone: event.target.value })} required /></div>
        <button disabled={busy}>Book idempotently</button>
      </form>{data.meetings.map((item) => <div className="row" key={item.id}><div><strong>{new Date(item.starts_at).toLocaleString()}</strong><p style={{ margin: 0 }}>{item.timezone}</p></div><a className="pill" href={item.booking_url}>confirmation</a></div>)}</article>

      <article className="card full"><h2>Audit timeline</h2><div className="timeline">{data.timeline.map((item) => <article key={item.id}><strong>{item.type.replaceAll("_", " ")}</strong><pre>{JSON.stringify(item.data, null, 2)}</pre><time>{new Date(item.created_at).toLocaleString()}</time></article>)}</div></article>
    </section>
  </>;
}
