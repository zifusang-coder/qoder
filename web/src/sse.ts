import { getToken, handleUnauthorized } from "./client";

type SseError = string | { code: string; message: string };
type SseEvent = { delta?: string; error?: SseError };

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

export async function streamSse(
  path: string,
  body: unknown,
  onChunk: (text: string) => void,
  onDone?: () => void,
): Promise<void> {
  const requestToken = getToken();
  const resp = await fetch(`/api${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${requestToken ?? ""}`,
    },
    body: JSON.stringify(body),
  });
  if (resp.status === 401) handleUnauthorized(requestToken);
  if (!resp.ok || !resp.body) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.message ?? "流式请求失败");
  }

  const reader = resp.body.getReader();
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
    let event: SseEvent;
    try {
      event = JSON.parse(joined) as SseEvent;
    } catch {
      onChunk(joined);
      return false;
    }
    if (event.error) {
      throw new Error(
        typeof event.error === "string" ? event.error : event.error.message,
      );
    }
    if (event.delta) onChunk(event.delta);
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
