import { getToken } from "./client";

type SseEvent = { delta?: string; error?: string };

export async function streamSse(
  path: string,
  body: unknown,
  onChunk: (text: string) => void,
  onDone?: () => void,
): Promise<void> {
  const resp = await fetch(`/api${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken() ?? ""}`,
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok || !resp.body) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.message ?? "流式请求失败");
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      for (const line of part.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (!data) continue;
        if (data === "[DONE]") {
          onDone?.();
          return;
        }
        let event: SseEvent;
        try {
          event = JSON.parse(data) as SseEvent;
        } catch {
          onChunk(data);
          continue;
        }
        if (event.error) throw new Error(event.error);
        if (event.delta) onChunk(event.delta);
      }
    }
  }
  onDone?.();
}
