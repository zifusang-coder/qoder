import { getToken, handleUnauthorized } from "./client";

export type SseError = string | { code: string; message: string };

export interface SseEvent {
  delta?: string;
  done?: unknown;
  error?: SseError;
}

export interface SseHandlers<T = unknown> {
  onChunk?: (text: string) => void;
  onEvent?: (event: SseEvent & { done?: T }) => void;
  onDone?: () => void;
}

function eventData(block: string): string | null {
  const dataLines: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (!line.startsWith("data:")) continue;
    let data = line.slice(5);
    if (data.startsWith(" ")) data = data.slice(1);
    dataLines.push(data);
  }
  if (dataLines.length === 0) return null;
  return dataLines.join("\n");
}

async function openStream(path: string, init: RequestInit): Promise<Response> {
  const requestToken = getToken();
  const headers = new Headers(init.headers);
  if (requestToken) headers.set("Authorization", `Bearer ${requestToken}`);
  const resp = await fetch(`/api${path}`, { ...init, headers });
  if (resp.status === 401) handleUnauthorized(requestToken);
  if (!resp.ok || !resp.body) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.message ?? "流式请求失败");
  }
  return resp;
}

export async function streamSse<T = unknown>(
  path: string,
  body: unknown,
  handlers: SseHandlers<T>,
): Promise<void> {
  const resp = await openStream(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return consume(resp, handlers);
}

export async function streamSseGet<T = unknown>(
  path: string,
  handlers: SseHandlers<T>,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await openStream(path, { method: "GET", signal });
  return consume(resp, handlers);
}

async function consume<T>(resp: Response, handlers: SseHandlers<T>) {
  const { onChunk, onEvent, onDone } = handlers;
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let doneCalled = false;

  const finish = () => {
    if (doneCalled) return;
    doneCalled = true;
    onDone?.();
  };

  // Returns true when [DONE] was seen and streaming should stop.
  const handleBlock = (block: string): boolean => {
    const joined = eventData(block);
    if (joined === null || joined === "") return false;
    if (joined === "[DONE]") {
      finish();
      return true;
    }
    let event: SseEvent & { done?: T };
    try {
      event = JSON.parse(joined) as SseEvent & { done?: T };
    } catch {
      onChunk?.(joined);
      return false;
    }
    if (event.error) {
      throw new Error(
        typeof event.error === "string" ? event.error : event.error.message,
      );
    }
    onEvent?.(event);
    if (event.delta) onChunk?.(event.delta);
    return false;
  };

  // Splits on blank lines (LF or CRLF). When `final` is set the remaining
  // buffer is treated as a complete event even without a trailing blank line.
  const drain = (final: boolean): boolean => {
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = final ? "" : (blocks.pop() ?? "");
    for (const block of blocks) {
      if (handleBlock(block)) return true;
    }
    return false;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (drain(false)) {
        await reader.cancel().catch(() => {});
        return;
      }
    }
    // Flush any UTF-8 sequence split across the last chunk.
    buffer += decoder.decode();
    if (drain(true)) {
      await reader.cancel().catch(() => {});
      return;
    }
    finish();
  } catch (err) {
    await reader.cancel().catch(() => {});
    throw err;
  }
}
