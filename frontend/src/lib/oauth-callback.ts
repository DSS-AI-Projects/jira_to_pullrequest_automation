// An OAuth authorization code is single-use, and the backend consumes the
// matching `state` row before exchanging it. React StrictMode (dev) runs a
// callback page's effect twice, and the page's own re-renders can re-run it
// too — a second exchange request then fails with "sign-in attempt is
// missing or expired" even though the first one connected the account, and
// that second failure is what the page used to show. Sharing one request per
// provider+state makes every run of the effect see the same, real outcome.
const inflight = new Map<string, Promise<unknown>>();

export function completeOAuthCallbackOnce<T>(
  key: string,
  run: () => Promise<T>,
): Promise<T> {
  const existing = inflight.get(key) as Promise<T> | undefined;
  if (existing) {
    return existing;
  }
  const pending = run();
  inflight.set(key, pending);
  return pending;
}
