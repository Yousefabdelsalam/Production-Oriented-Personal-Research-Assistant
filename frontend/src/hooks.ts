import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export interface AsyncState<T> {
  data: T | undefined;
  error: ApiError | Error | undefined;
  loading: boolean;
  reload: () => void;
  setData: (d: T | undefined) => void;
}

/**
 * Fetch-on-mount/deps with optional polling. `poll(data)` returns the delay in ms until the next refresh, or null to stop
 * (e.g. keep polling only while a source is still processing).
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], poll?: (d: T | undefined) => number | null): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<ApiError | Error | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const pollRef = useRef(poll);
  pollRef.current = poll;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const run = async (first: boolean) => {
      if (first) setLoading(true);
      try {
        const d = await fn();
        if (cancelled) return;
        setData(d);
        setError(undefined);
        const next = pollRef.current?.(d);
        if (next) timer = setTimeout(() => run(false), next);
      } catch (e) {
        if (cancelled) return;
        setError(e as Error);
        const next = pollRef.current?.(undefined);
        if (next) timer = setTimeout(() => run(false), Math.max(next, 5000));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    run(true);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload, setData };
}

/** Run a mutation with busy/error state. */
export function useAction<A extends unknown[], R>(fn: (...a: A) => Promise<R>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const run = useCallback(
    async (...a: A): Promise<R | undefined> => {
      setBusy(true);
      setError(undefined);
      try {
        return await fn(...a);
      } catch (e) {
        setError((e as Error).message);
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [fn],
  );
  return { run, busy, error, clearError: () => setError(undefined) };
}
