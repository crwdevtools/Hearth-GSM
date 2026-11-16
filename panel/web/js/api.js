// Thin JSON client for the panel API.

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn;
}

export async function api(path, { method = "GET", body, query, signal } = {}) {
  let url = `/api${path}`;
  if (query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== "") params.set(k, v);
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }
  let res;
  try {
    res = await fetch(url, {
      method,
      signal,
      credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError("Can't reach the panel. Check your connection.", 0);
  }
  if (res.status === 204) return null;
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text.slice(0, 200) };
    }
  }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/auth/")) onUnauthorized();
    const detail = data?.detail;
    throw new ApiError(typeof detail === "string" ? detail : `Request failed (${res.status}).`, res.status, data);
  }
  return data;
}

export const get = (path, query, opts) => api(path, { ...opts, query });
export const post = (path, body) => api(path, { method: "POST", body: body ?? {} });
export const put = (path, body) => api(path, { method: "PUT", body: body ?? {} });
export const patch = (path, body) => api(path, { method: "PATCH", body: body ?? {} });
export const del = (path, query) => api(path, { method: "DELETE", query });

export function socket(path) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(`${proto}://${location.host}/api${path}`);
}
