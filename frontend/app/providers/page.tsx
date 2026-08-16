"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Field = {
  name: string;
  label: string;
  type?: string;
  placeholder?: string;
  required?: boolean;
  options?: string[];
};

type ProviderConfig = {
  provider: string;
  label: string;
  enabled: boolean;
  revision: number;
  config: Record<string, string | number>;
  secret_fields: Record<string, boolean>;
  config_fields: Field[];
  last_check_status?: string;
  last_check_details: Record<string, unknown>;
  last_checked_at?: string;
};

type Draft = {
  enabled: boolean;
  config: Record<string, string | number>;
  secrets: Record<string, string>;
  clearSecrets: string[];
};

function title(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export default function ProvidersPage() {
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [busy, setBusy] = useState<string>("");
  const [telegramCode, setTelegramCode] = useState("");
  const [telegramPassword, setTelegramPassword] = useState("");
  const [telegramCodeSent, setTelegramCodeSent] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [publicOrigin, setPublicOrigin] = useState("");

  async function load() {
    try {
      const data = await api<ProviderConfig[]>("/admin/providers");
      setProviders(data);
      setDrafts(
        Object.fromEntries(
          data.map((provider) => [
            provider.provider,
            {
              enabled: provider.enabled,
              config: { ...provider.config },
              secrets: {},
              clearSecrets: [],
            },
          ]),
        ),
      );
      setError("");
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  useEffect(() => {
    setPublicOrigin(window.location.origin);
    void load();
  }, []);

  function updateDraft(provider: string, update: Partial<Draft>) {
    setDrafts((current) => ({
      ...current,
      [provider]: { ...current[provider], ...update },
    }));
  }

  async function startTelegramLogin() {
    setBusy("telegram");
    setError("");
    try {
      const result = await api<{ phone: string }>("/admin/providers/telegram/auth/start", {
        method: "POST",
        body: "{}",
      });
      setTelegramCodeSent(true);
      setNotice(`Telegram sent a login code to ${result.phone}.`);
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function confirmTelegramLogin() {
    setBusy("telegram");
    setError("");
    try {
      const result = await api<{ authenticated: boolean; password_required: boolean }>(
        "/admin/providers/telegram/auth/confirm",
        {
          method: "POST",
          body: JSON.stringify({ code: telegramCode, password: telegramPassword || null }),
        },
      );
      if (result.password_required) {
        setNotice("Telegram requires the account's two-step verification password.");
      } else {
        setNotice("Telegram account connected successfully.");
        setTelegramCodeSent(false);
        setTelegramCode("");
        setTelegramPassword("");
        await load();
      }
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function save(item: ProviderConfig) {
    const draft = drafts[item.provider];
    if (!draft) return;
    setBusy(item.provider);
    setError("");
    try {
      await api(`/admin/providers/${item.provider}`, {
        method: "PUT",
        body: JSON.stringify({
          enabled: draft.enabled,
          config: draft.config,
          secrets: draft.secrets,
          clear_secrets: draft.clearSecrets,
          expected_revision: item.revision || null,
        }),
      });
      setNotice(`${item.label} configuration saved. Existing secrets were never returned to the browser.`);
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function check(item: ProviderConfig) {
    setBusy(item.provider);
    setError("");
    try {
      const result = await api<{ configured: boolean; details: Record<string, unknown> }>(
        `/admin/providers/${item.provider}/check`,
        { method: "POST", body: "{}" },
      );
      setNotice(
        result.configured
          ? `${item.label} connection passed.`
          : `${item.label} connection failed: ${String(result.details.reason || "check provider details")}`,
      );
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <section className="heading">
        <div>
          <h1>Connect providers</h1>
          <p>
            Credentials are encrypted by the API and never displayed again. Save each account,
            then run its non-destructive connection check before launching outreach.
          </p>
        </div>
      </section>
      {publicOrigin && (
        <section className="card">
          <h2>Provider callback URLs</h2>
          <p>Use these HTTPS URLs in the provider consoles. They are public signature-verified ingress routes; the management API remains private.</p>
          <div className="form">
            <div><label>Amazon SES / SNS events and inbound receipt-rule email</label><input readOnly value={`${publicOrigin}/api/webhooks/ses/events`} onFocus={(event) => event.currentTarget.select()} /></div>
            <div><label>WhatsApp Cloud webhook</label><input readOnly value={`${publicOrigin}/api/webhooks/whatsapp`} onFocus={(event) => event.currentTarget.select()} /></div>
            <div><label>Cal.com webhook</label><input readOnly value={`${publicOrigin}/api/webhooks/calcom`} onFocus={(event) => event.currentTarget.select()} /></div>
          </div>
        </section>
      )}
      {error && <pre className="error">{error}</pre>}
      {notice && <pre className="success">{notice}</pre>}
      <section className="provider-config-grid">
        {providers.map((item) => {
          const draft = drafts[item.provider];
          if (!draft) return null;
          return (
            <article className="card provider-config-card" key={item.provider}>
              <div className="provider-card-heading">
                <div>
                  <div className="label">{item.provider}</div>
                  <h2>{item.label}</h2>
                </div>
                <label className="toggle-label">
                  <input
                    type="checkbox"
                    checked={draft.enabled}
                    onChange={(event) => updateDraft(item.provider, { enabled: event.target.checked })}
                  />
                  Enabled
                </label>
              </div>
              <div className="connection-state">
                <span className={`status-dot ${item.last_check_status === "ready" ? "" : "off"}`} />
                {item.last_check_status === "ready" ? "Connection ready" : "Not verified"}
                {item.last_checked_at && <small> · {new Date(item.last_checked_at).toLocaleString()}</small>}
              </div>
              <div className="form two">
                {item.config_fields.map((field) => (
                  <div key={field.name}>
                    <label>{field.label}{field.required ? " *" : ""}</label>
                    {field.type === "select" ? (
                      <select
                        value={String(draft.config[field.name] ?? "")}
                        onChange={(event) =>
                          updateDraft(item.provider, {
                            config: { ...draft.config, [field.name]: event.target.value },
                          })
                        }
                      >
                        <option value="">Select</option>
                        {field.options?.map((option) => <option key={option}>{option}</option>)}
                      </select>
                    ) : (
                      <input
                        type={field.type || "text"}
                        value={String(draft.config[field.name] ?? "")}
                        placeholder={field.placeholder}
                        onChange={(event) =>
                          updateDraft(item.provider, {
                            config: { ...draft.config, [field.name]: event.target.value },
                          })
                        }
                      />
                    )}
                  </div>
                ))}
              </div>
              <h3>Credentials</h3>
              <div className="form">
                {Object.entries(item.secret_fields).map(([name, configured]) => (
                  <div key={name}>
                    <label>{title(name)} {configured && <span className="pill">Stored</span>}</label>
                    <input
                      type="password"
                      autoComplete="new-password"
                      value={draft.secrets[name] || ""}
                      placeholder={configured ? "Leave blank to keep stored value" : "Enter credential"}
                      onChange={(event) =>
                        updateDraft(item.provider, {
                          secrets: { ...draft.secrets, [name]: event.target.value },
                        })
                      }
                    />
                    {configured && (
                      <label className="clear-secret">
                        <input
                          type="checkbox"
                          checked={draft.clearSecrets.includes(name)}
                          onChange={(event) =>
                            updateDraft(item.provider, {
                              clearSecrets: event.target.checked
                                ? [...draft.clearSecrets, name]
                                : draft.clearSecrets.filter((value) => value !== name),
                            })
                          }
                        /> Clear stored credential
                      </label>
                    )}
                  </div>
                ))}
              </div>
              {item.provider === "telegram" && (
                <div className="telegram-connect">
                  <h3>Account login</h3>
                  <p>Save the API ID, phone, and API hash first. The login code and optional 2FA password are used once and never stored.</p>
                  <button className="secondary" disabled={busy === "telegram"} onClick={startTelegramLogin}>Send Telegram login code</button>
                  {telegramCodeSent && (
                    <div className="form two telegram-code-form">
                      <div><label>Login code</label><input value={telegramCode} onChange={(event) => setTelegramCode(event.target.value)} /></div>
                      <div><label>2FA password, if enabled</label><input type="password" value={telegramPassword} onChange={(event) => setTelegramPassword(event.target.value)} /></div>
                      <button disabled={busy === "telegram" || !telegramCode} onClick={confirmTelegramLogin}>Connect account</button>
                    </div>
                  )}
                </div>
              )}
              <div className="provider-actions">
                <button disabled={busy === item.provider} onClick={() => save(item)}>Save securely</button>
                <button
                  className="secondary"
                  disabled={busy === item.provider || !item.enabled}
                  onClick={() => check(item)}
                >
                  Test connection
                </button>
              </div>
              {Object.keys(item.last_check_details || {}).length > 0 && (
                <details><summary>Last check details</summary><pre>{JSON.stringify(item.last_check_details, null, 2)}</pre></details>
              )}
            </article>
          );
        })}
      </section>
    </>
  );
}
