import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, streamRunEvents } from "../api/client";
import type { EventRecord, StreamControl } from "../api/types";

export type ConnectionState =
  | "idle"
  | "connecting"
  | "live"
  | "reconnecting"
  | "ended"
  | "error";

export interface RunStream {
  events: EventRecord[];
  connection: ConnectionState;
  control: StreamControl | null;
  lastSeq: number;
  error: string | null;
  retryNow: () => void;
}

export function useRunStream(
  runId: string | null,
  apiKey: string,
  enabled = true,
): RunStream {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [control, setControl] = useState<StreamControl | null>(null);
  const [lastSeq, setLastSeq] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
  const seen = useRef<Set<number>>(new Set());
  const cursor = useRef(0);

  useEffect(() => {
    setEvents([]);
    setControl(null);
    setConnection("idle");
    setLastSeq(0);
    setError(null);
    seen.current = new Set();
    cursor.current = 0;
  }, [runId, apiKey]);

  useEffect(() => {
    if (!enabled || !runId || !apiKey) return;
    let stopped = false;
    let attempt = 0;
    let timer: number | undefined;
    let controller: AbortController | null = null;

    const scheduleRetry = () => {
      if (stopped) return;
      attempt += 1;
      const delay = Math.min(8000, 900 * 2 ** Math.min(attempt, 4));
      setConnection("reconnecting");
      timer = window.setTimeout(connect, delay);
    };

    const connect = () => {
      if (stopped) return;
      controller = new AbortController();
      setConnection(attempt === 0 && events.length === 0 ? "connecting" : "reconnecting");
      setError(null);
      streamRunEvents({
        key: apiKey,
        runId,
        afterSeq: cursor.current,
        signal: controller.signal,
        onOpen: () => {
          if (!stopped) {
            setConnection("live");
            attempt = 0;
          }
        },
        onEvent: (event) => {
          if (stopped) return;
          if (seen.current.has(event.seq)) return;
          seen.current.add(event.seq);
          cursor.current = Math.max(cursor.current, event.seq);
          setLastSeq(cursor.current);
          setEvents((previous) => {
            const next = [...previous, event];
            next.sort((a, b) => a.seq - b.seq);
            return next;
          });
          setConnection("live");
          attempt = 0;
        },
        onControl: (message) => {
          if (stopped) return;
          stopped = true;
          setControl(message);
          setConnection("ended");
          controller?.abort();
        },
      })
        .then(() => {
          if (!stopped) scheduleRetry();
        })
        .catch((caught: unknown) => {
          if (stopped || controller?.signal.aborted) return;
          if (caught instanceof ApiError && caught.status === 401) {
            stopped = true;
            setConnection("error");
            setError("API key rejected while opening the event stream.");
            return;
          }
          setError(caught instanceof Error ? caught.message : "Event stream interrupted.");
          scheduleRetry();
        });
    };

    connect();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
      controller?.abort();
    };
    // `events.length` is only used for the initial label; it must not reconnect the
    // stream every time an event arrives. The cursor is kept in a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, runId, apiKey, retryNonce]);

  const retryNow = useCallback(() => {
    setError(null);
    setRetryNonce((value) => value + 1);
  }, []);

  return { events, connection, control, lastSeq, error, retryNow };
}
