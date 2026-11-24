import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { streamSseGet } from "./sse";

export interface LiveHandlers<T> {
  onSnapshot?: (snapshot: T) => void;
  onSettled?: () => void;
}

/**
 * Subscribe a query-cache entry to a snapshot stream.
 *
 * The server pushes a frame whenever the resource changes and closes the
 * stream once it reaches a terminal status, so pages no longer poll.
 */
export function useLiveSnapshot<T>(
  queryKey: readonly unknown[],
  streamPath: string | null,
  handlers: LiveHandlers<T> = {},
): void {
  const queryClient = useQueryClient();
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const cacheKey = JSON.stringify(queryKey);

  useEffect(() => {
    if (!streamPath) return;
    const controller = new AbortController();
    let active = true;
    const parsedKey = JSON.parse(cacheKey) as unknown[];
    streamSseGet<T>(
      streamPath,
      {
        onEvent: (event) => {
          if (event.done === undefined) return;
          queryClient.setQueryData(parsedKey, event.done);
          handlersRef.current.onSnapshot?.(event.done);
        },
      },
      controller.signal,
    )
      .catch(() => {
        // 断线时保留最后一次快照；后续请求仍会通过普通查询兜底。
      })
      .finally(() => {
        if (active) handlersRef.current.onSettled?.();
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [streamPath, cacheKey, queryClient]);
}
