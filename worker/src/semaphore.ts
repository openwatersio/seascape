/**
 * A counting semaphore: at most `max` calls run concurrently, the rest queue.
 * The tile Worker uses it to cap overzoom synthesis — each decode+encode holds
 * several MB of libwebp working memory, and a request burst over a detailed
 * region ran enough of them at once to exhaust the isolate (malloc → null →
 * "Decoding error"). Bounding the concurrency keeps peak memory flat.
 *
 * Plain, dependency-free, Node-importable (semaphore.test.mjs).
 */
export function limiter(max: number) {
  let active = 0;
  const waiters: Array<() => void> = [];
  return async function run<T>(fn: () => Promise<T>): Promise<T> {
    // Free slot: take it. Otherwise wait — a releaser hands its slot directly to
    // the next waiter (active unchanged), so active never exceeds max.
    if (active >= max) await new Promise<void>((r) => waiters.push(r));
    else active++;
    try {
      return await fn();
    } finally {
      const next = waiters.shift();
      if (next) next();
      else active--;
    }
  };
}
