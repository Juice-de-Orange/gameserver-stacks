/**
 * A response that has been overtaken must not overwrite newer state.
 *
 * Several pages poll in the background while the user can write at the same time. There is no
 * natural ordering between the two: the poll sends its requests, the user saves afterwards, and
 * the poll's response — which read the state BEFORE the save — arrives last and overwrites the
 * value that was just saved.
 *
 * On `CrossServer.tsx` the window is especially wide: `loadAll` waits for three requests with
 * `Promise.all`, so `setSettings` only runs once the slowest of them (the leaderboard) is done.
 * The switch then jumps back shortly after the "saved" toast, and clicking again — the obvious
 * reflex — really does undo the user's own change.
 *
 * The rule: every write of our own invalidates everything older.
 *
 *     const fresh = useRef(newFreshnessGuard()).current;
 *
 *     async function load() {
 *       const mark = fresh.begin();
 *       const data = await apiJson(...);
 *       if (fresh.valid(mark)) setData(data);   // otherwise: overtaken, discard
 *     }
 *
 *     async function save(value) {
 *       fresh.invalidate();                 // requests in flight are stale from now on
 *       const saved = await apiJson(..., { method: "PUT", ... });
 *       fresh.invalidate();                 // and so are those started during the PUT
 *       setData(saved);
 *     }
 *
 * The second `invalidate()` is not overkill: a request that starts DURING the PUT may just as
 * well have read the old value.
 */
export interface FreshnessGuard {
  /** Call before a request and keep the return value until the response arrives. */
  begin(): number;
  /** May the result of a request marked with `begin()` still be written? */
  valid(atBegin: number): boolean;
  /** After every write of our own: everything older counts as overtaken. */
  invalidate(): void;
}

export function newFreshnessGuard(): FreshnessGuard {
  let generation = 0;
  return {
    begin: () => generation,
    valid: (atBegin: number) => atBegin === generation,
    invalidate: () => {
      generation += 1;
    },
  };
}
