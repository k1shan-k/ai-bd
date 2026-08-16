export const API = "/api/backend";

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "X-Actor": "crm-operator",
      "X-Role": "admin",
      ...(init?.headers || {}),
    },
  });
  if (!response.ok) {
    if (response.status === 401 && typeof window !== "undefined") window.location.href = "/login";
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail));
  }
  return response.json();
}

export async function upload<T>(path: string, data: FormData): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    body: data,
    headers: { "X-Actor": "crm-operator", "X-Role": "admin" },
  });
  if (!response.ok) {
    if (response.status === 401 && typeof window !== "undefined") window.location.href = "/login";
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || "Upload failed");
  }
  return response.json();
}
