import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, getToken, setToken } from "../client";

function stubBrowser(initialToken: string | null = null, pathname = "/chat") {
  const store = new Map<string, string>();
  if (initialToken) store.set("nm_token", initialToken);
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => store.clear(),
  });
  const locationStub = { pathname, href: "" };
  vi.stubGlobal("location", locationStub);
  return { store, location: locationStub };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("api", () => {
  it("returns undefined for a 204 response", async () => {
    stubBrowser("tok");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 204 })),
    );

    await expect(api("/settings/providers/1")).resolves.toBeUndefined();
  });

  it("maps an error envelope to ApiError with code, message and detail", async () => {
    stubBrowser("tok");
    const envelope = {
      code: "validation_error",
      message: "参数错误",
      detail: [{ msg: "字段 A 无效" }, { msg: "" }],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(envelope), {
            status: 422,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );

    const err = await api("/settings/providers").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(422);
    expect(apiErr.code).toBe("validation_error");
    expect(apiErr.message).toBe("参数错误");
    expect(apiErr.detail).toEqual(envelope.detail);
  });

  it("does not set a JSON content type for FormData uploads", async () => {
    stubBrowser("tok");
    const fetchMock = vi.fn(
      async (_input: string, _init: RequestInit) =>
        new Response(JSON.stringify({ upserted: 0, errors: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const formData = new FormData();
    formData.append("file", new Blob(["content_id,views\n"], { type: "text/csv" }), "stats.csv");

    await api("/analytics/import", { method: "POST", body: formData });

    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init.headers);
    expect(headers.has("Content-Type")).toBe(false);
    expect(headers.get("Authorization")).toBe("Bearer tok");
  });

  it("does not clear a newer token when a stale request gets a 401", async () => {
    const { location } = stubBrowser("old-token");
    let resolveFetch!: (resp: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    vi.stubGlobal("fetch", vi.fn(() => pending));

    const request = api("/chat").catch((e) => e);
    // A fresh login completes while the old request is still in flight.
    setToken("new-token");
    resolveFetch(new Response(null, { status: 401 }));
    const err = await request;

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect(getToken()).toBe("new-token");
    expect(location.href).toBe("");
  });

  it("clears the token and redirects on a matching 401", async () => {
    const { location } = stubBrowser("tok");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 401 })),
    );

    const err = await api("/chat").catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(getToken()).toBeNull();
    expect(location.href).toBe("/login");
  });
});
