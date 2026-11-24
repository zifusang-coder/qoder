import { afterEach, describe, expect, it, vi } from "vitest";
import { streamSse, streamSseGet, type SseEvent } from "../sse";

const encoder = new TextEncoder();

function bytes(text: string): Uint8Array {
  return encoder.encode(text);
}

function sseResponse(chunks: Uint8Array[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

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

describe("streamSse", () => {
  it("parses events framed with CRLF blank lines", async () => {
    stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([bytes('data: {"delta":"你"}\r\n\r\ndata: {"delta":"好"}\r\n\r\n')]),
      ),
    );

    const chunks: string[] = [];
    const onDone = vi.fn();
    await streamSse("/chat", {}, { onChunk: (text) => chunks.push(text), onDone });

    expect(chunks).toEqual(["你", "好"]);
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("decodes a UTF-8 character split across byte chunks", async () => {
    stubBrowser();
    const all = bytes('data: {"delta":"你"}\n\n');
    // The closing `"}\n\n` is 4 bytes, so cutting 6 bytes from the end lands
    // in the middle of the 3-byte UTF-8 sequence for 你.
    const split = all.length - 6;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => sseResponse([all.slice(0, split), all.slice(split)])),
    );

    const chunks: string[] = [];
    await streamSse("/chat", {}, { onChunk: (text) => chunks.push(text) });

    expect(chunks).toEqual(["你"]);
  });

  it("flushes a final event that has no trailing blank line", async () => {
    stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => sseResponse([bytes('data: {"delta":"末"}')])),
    );

    const chunks: string[] = [];
    const onDone = vi.fn();
    await streamSse("/chat", {}, { onChunk: (text) => chunks.push(text), onDone });

    expect(chunks).toEqual(["末"]);
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("joins multiple data lines with a newline", async () => {
    stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([bytes('data: {\ndata:   "delta": "多行"\ndata: }\n\n')]),
      ),
    );

    const chunks: string[] = [];
    await streamSse("/chat", {}, { onChunk: (text) => chunks.push(text) });

    expect(chunks).toEqual(["多行"]);
  });

  it("stops at [DONE] and calls onDone exactly once", async () => {
    stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          bytes('data: {"delta":"a"}\n\ndata: [DONE]\n\ndata: {"delta":"never"}\n\n'),
        ]),
      ),
    );

    const chunks: string[] = [];
    const onDone = vi.fn();
    await streamSse("/chat", {}, { onChunk: (text) => chunks.push(text), onDone });

    expect(chunks).toEqual(["a"]);
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("delivers parsed events including the done payload", async () => {
    stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          bytes(
            'data: {"delta":"a"}\n\n' +
              'data: {"done":{"reply":"a","conversation_id":"c1"}}\n\n' +
              "data: [DONE]\n\n",
          ),
        ]),
      ),
    );

    const events: SseEvent[] = [];
    const chunks: string[] = [];
    await streamSse("/chat/stream", {}, {
      onChunk: (text) => chunks.push(text),
      onEvent: (event) => events.push(event),
    });

    expect(chunks).toEqual(["a"]);
    expect(events).toEqual([
      { delta: "a" },
      { done: { reply: "a", conversation_id: "c1" } },
    ]);
  });

  it("throws on an in-band error event", async () => {
    stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => sseResponse([bytes('data: {"error":"boom"}\n\n')])),
    );

    await expect(streamSse("/chat", {}, {})).rejects.toThrow("boom");
  });

  it("throws the message from a structured error event", async () => {
    stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          bytes('data: {"error":{"code":"skill_stream_failed","message":"生成失败"}}\n\n'),
        ]),
      ),
    );

    await expect(streamSse("/chat", {}, {})).rejects.toThrow("生成失败");
  });

  it("calls the unauthorized helper on a 401 response", async () => {
    const { store, location } = stubBrowser("stale-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 401 })),
    );

    await expect(streamSse("/chat", {}, {})).rejects.toThrow("流式请求失败");

    expect(store.has("nm_token")).toBe(false);
    expect(location.href).toBe("/login");
  });
});

describe("streamSseGet", () => {
  it("sends a GET with the bearer token and parses done events", async () => {
    const { store } = stubBrowser("live-token");
    const fetchMock = vi.fn(
      async (_url: string, _init: RequestInit) =>
        sseResponse([bytes('data: {"done":{"status":"succeeded"}}\n\ndata: [DONE]\n\n')]),
    );
    vi.stubGlobal("fetch", fetchMock);

    const events: SseEvent[] = [];
    await streamSseGet("/agent/runs/1/stream", {
      onEvent: (event) => events.push(event),
    });

    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe("GET");
    expect((init.headers as Headers).get("Authorization")).toBe(
      "Bearer live-token",
    );
    expect(events).toEqual([{ done: { status: "succeeded" } }]);
    expect(store.get("nm_token")).toBe("live-token");
  });
});
