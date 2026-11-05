const TOKEN_KEY = "nm_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export function handleUnauthorized(requestToken: string | null) {
  // A newer login may have happened while this request was in flight; only
  // clear the token if it is still the one the failing request was sent with.
  if (getToken() !== requestToken) return;
  setToken(null);
  if (!location.pathname.endsWith("/login")) location.href = "/login";
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await request(path, init);
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export async function fetchBlob(
  path: string,
  init: RequestInit = {},
): Promise<Blob> {
  const resp = await request(path, init);
  return resp.blob();
}

async function request(path: string, init: RequestInit): Promise<Response> {
  const requestToken = getToken();
  const headers = new Headers(init.headers);
  // Let the browser set the multipart boundary for FormData uploads.
  if (
    init.body != null &&
    !(typeof FormData !== "undefined" && init.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  if (requestToken && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${requestToken}`);
  }
  const resp = await fetch(`/api${path}`, {
    ...init,
    headers,
  });
  if (resp.status === 401) {
    handleUnauthorized(requestToken);
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new ApiError(
      resp.status,
      body.code ?? "error",
      body.message ?? "请求失败",
      body.detail,
    );
  }
  return resp;
}
