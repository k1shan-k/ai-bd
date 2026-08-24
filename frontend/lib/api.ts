export const API = "/api/backend";

type ValidationIssue = { loc?: (string | number)[]; msg?: string };

/** FastAPI returns a string detail for handled errors and an issue array for 422 validation. */
function describe(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = (detail as ValidationIssue[]).map((issue) => {
      const field = (issue.loc || []).filter((part) => part !== "body").join(".");
      const message = issue.msg || "is invalid";
      return field ? `${field}: ${message}` : message;
    });
    if (parts.length) return parts.join("; ");
  }
  return fallback;
}

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
    throw new Error(describe(error.detail, response.statusText || "Request failed"));
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
    throw new Error(describe(error.detail, "Upload failed"));
  }
  return response.json();
}
