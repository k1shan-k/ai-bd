"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { api, upload } from "@/lib/api";

type ContextVersion = {
  id: string;
  version: number;
  content_hash: string;
  activated_at: string;
};
type Campaign = {
  id: string;
  name: string;
  status: string;
  context_version_id: string;
  followup_days: number[];
  whatsapp_fallback_day: number;
};
type ImportJob = {
  id: string;
  file_name: string;
  status: string;
  summary: Record<string, number>;
  created_at: string;
};
type LaunchResult = {
  campaign_id: string;
  launched: string[];
  skipped?: Array<{ lead_id: string; reason: string }>;
  cycles?: Array<{ logical_now: string; result: object }>;
};

const defaults: Record<string, string> = {
  "company.md": "---\nname: Example Organization\n---\nWe organize focused industry events.",
  "voice-and-style.md": "---\npersona: sponsorship team\nlanguage: English\nidentity_disclosure: team\n---\nBe concise, helpful, transparent, and never claim to be a named human.",
  "event.md": "---\nname: Example Event\ntimezone: UTC\n---\nDescribe the event, date, location, audience, and proof here.",
  "audience.md": "---\nexpected_attendance: 250\n---\nDescribe the attendees and sponsor fit.",
  "packages.md": "---\npackages:\n  - id: gold\n    name: Gold Partner\n    list_price: 10000\n    min_price: 9000\n    perks: [stage mention, booth, logo]\n  - id: silver\n    name: Silver Partner\n    list_price: 5000\n    min_price: 4500\n    perks: [booth, logo]\n---\nPackage positioning and benefits.",
  "negotiation-policy.md": "---\ncurrency: USD\nmax_discount_percent: 10\nallowed_custom_perks: [newsletter mention]\nforbidden_promises: [guaranteed sales, attendee personal data]\nmandatory_escalation: [legal terms, custom contract]\noffer_expiry_days: 7\n---\nNegotiate only inside these caps.",
  "inventory.md": "---\ninventory:\n  gold: 2\n  silver: 5\n---\nInventory for this event.",
  "faq.md": "---\nowner: sponsorship team\n---\nAdd approved answers to common sponsor questions.",
  "qualification.md": "---\nexplicit_call_request_qualifies: true\ninterest_plus_tier_qualifies: true\n---\nKeep qualification lightweight.",
  "escalation.md": "---\nrules: [low confidence, legal request, complaint, unavailable inventory, outside negotiation caps]\n---\nEscalate rather than inventing an answer.",
};

export default function EventWorkspace({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [documents, setDocuments] = useState(defaults);
  const [contexts, setContexts] = useState<ContextVersion[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [imports, setImports] = useState<ImportJob[]>([]);
  const [tab, setTab] = useState("context");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [contextData, campaignData, importData] = await Promise.all([
        api<ContextVersion[]>(`/events/${id}/contexts`),
        api<Campaign[]>(`/events/${id}/campaigns`),
        api<ImportJob[]>(`/events/${id}/imports`),
      ]);
      setContexts(contextData);
      setCampaigns(campaignData);
      setImports(importData);
      setError("");
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  useEffect(() => { void load(); }, [id]);

  async function perform(work: () => Promise<unknown>, success?: string) {
    setBusy(true);
    setError("");
    try {
      const response = await work();
      setResult(success || JSON.stringify(response, null, 2));
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function validate() {
    return perform(async () => {
      const response = await api<{ valid: boolean; errors: string[] }>(
        `/events/${id}/contexts/validate`,
        { method: "POST", body: JSON.stringify({ documents }) },
      );
      if (!response.valid) throw new Error(response.errors.join("\n"));
      return response;
    }, "Context pack is internally consistent and ready to activate.");
  }

  function activateContext() {
    return perform(
      () => api<ContextVersion>(`/events/${id}/contexts/activate`, {
        method: "POST",
        body: JSON.stringify({ documents }),
      }),
      "Activated a new immutable context snapshot.",
    );
  }

  function createCampaign() {
    if (!contexts[0]) return;
    return perform(async () => {
      const created = await api<Campaign>(`/events/${id}/campaigns`, {
        method: "POST",
        body: JSON.stringify({
          name: "Fast sponsorship sequence",
          context_version_id: contexts[0].id,
          followup_days: [2, 5, 10],
          whatsapp_fallback_day: 5,
        }),
      });
      return api<Campaign>(`/campaigns/${created.id}/activate`, { method: "POST" });
    }, "Campaign activated and pinned to the latest context version.");
  }

  async function importFile(file: File) {
    const data = new FormData();
    data.append("file", file);
    setBusy(true);
    try {
      const response = await upload<Record<string, number | string>>(`/events/${id}/imports`, data);
      setResult(JSON.stringify(response, null, 2));
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function launch(campaign: Campaign, simulate: boolean) {
    return perform(
      () => api<LaunchResult>(`/campaigns/${campaign.id}/${simulate ? "simulate" : "launch"}`, {
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
  }

  const activeCampaign = campaigns.find((campaign) => campaign.status === "active");
  return <>
    <section className="heading">
      <div>
        <h1>Event workspace</h1>
        <p>Activate immutable knowledge, import consented leads, and launch controlled workflows.</p>
      </div>
      <div>
        {activeCampaign && <span className="pill">Campaign active</span>}{" "}
        <Link className="button secondary" href={`/leads?event_id=${id}`}>View leads</Link>
      </div>
    </section>
    <div className="tabs">
      <button className={tab === "context" ? "active" : ""} onClick={() => setTab("context")}>Context pack</button>
      <button className={tab === "import" ? "active" : ""} onClick={() => setTab("import")}>CSV import</button>
      <button className={tab === "launch" ? "active" : ""} onClick={() => setTab("launch")}>Campaign</button>
    </div>
    {error && <pre className="error">{error}</pre>}
    {result && <pre className="success">{result}</pre>}

    {tab === "context" && <section className="grid">
      {Object.entries(documents).map(([name, value]) => <article className="card" key={name}>
        <h3>{name}</h3>
        <textarea value={value} onChange={(event) => setDocuments({ ...documents, [name]: event.target.value })} />
      </article>)}
      <article className="card full actionbar">
        <p>Activation creates an immutable version. Existing conversations never change silently.</p>
        <div><button className="secondary" disabled={busy} onClick={validate}>Validate</button>{" "}<button disabled={busy} onClick={activateContext}>Activate immutable version</button></div>
      </article>
    </section>}

    {tab === "import" && <section className="grid">
      <article className="card wide">
        <h2>Import Luma/registrant CSV</h2>
        <p>Required mapped columns: name, email, Telegram, and sponsor answer. WhatsApp is optional. Only yes/maybe enters outreach; ambiguous identities are quarantined.</p>
        <input disabled={busy} type="file" accept=".csv,text/csv" onChange={(event) => event.target.files?.[0] && importFile(event.target.files[0])} />
      </article>
      <article className="card"><h2>Safety behavior</h2><p>Uploads are file-hash idempotent. Global suppression is checked before lead creation, and every original row remains auditable.</p></article>
      <article className="card full"><h2>Import history</h2>
        {imports.length === 0 && <p>No imports yet.</p>}
        {imports.map((job) => <div className="row" key={job.id}>
          <div><strong>{job.file_name}</strong><p style={{ margin: 0 }}>{new Date(job.created_at).toLocaleString()}</p></div>
          <div className="counts">{Object.entries(job.summary).map(([name, count]) => <span className="pill gray" key={name}>{name}: {count}</span>)}</div>
        </div>)}
      </article>
    </section>}

    {tab === "launch" && <section className="grid">
      <article className="card wide"><h2>Campaigns</h2>
        {campaigns.length === 0 && <p>No campaign created yet.</p>}
        {campaigns.map((campaign) => <div className="row" key={campaign.id}>
          <div><strong>{campaign.name}</strong><p style={{ margin: 0 }}>Days {campaign.followup_days.join(", ")} · WhatsApp fallback day {campaign.whatsapp_fallback_day}</p></div>
          <div><span className="pill">{campaign.status}</span>{" "}<button className="secondary" disabled={busy || campaign.status !== "active"} onClick={() => launch(campaign, true)}>Simulate</button>{" "}<button disabled={busy || campaign.status !== "active"} onClick={() => launch(campaign, false)}>Launch eligible leads</button></div>
        </div>)}
      </article>
      <article className="card"><h2>Fast sequence</h2><p>Day 0 email + Telegram, then days 2, 5, and 10. Telegram admits only 20 new contacts per account day; WhatsApp is a silent-lead fallback.</p><button disabled={busy || !contexts.length || Boolean(activeCampaign)} onClick={createCampaign}>{activeCampaign ? "Campaign active" : "Create and activate"}</button></article>
      <article className="card full"><h2>Context versions</h2>{contexts.map((context) => <div className="row" key={context.id}><div><strong>Version {context.version}</strong><p style={{ margin: 0 }}>{context.content_hash.slice(0, 20)}… · {new Date(context.activated_at).toLocaleString()}</p></div><span className="pill">immutable</span></div>)}</article>
    </section>}
  </>;
}
