# Plan: a log of changes for the Session

Status: proposed, not started.  Two decisions (section 4) must be taken before the work begins.

Until this plan is settled, nobody changes how `Session` (`src/djhtmx/repo.py`) reads, merges or writes the page's state.  A comment above the class says so.

## 1. Why

`Session` holds the state of every component of a page.  Each request (and each SSE render, see `src/djhtmx/sse.py`) builds its own `Session`, reads the page's Redis hash once, changes it in memory, and flushes it.  Two requests of one page overlap whenever the user acts while an earlier request is still running, e.g. typing while a photo uploads.

### History

- 7014686f52b2d252a3bfe99b641916b613afec92 (2024-09-27) stored the page in Redis, one component at a time.  `get_state` ran an `HGET` per component; `store` ran an `HSET` plus an `SADD` per signal, right away; `unregister_component` ran `KEYS {id}:subs:*` and an `SREM` per key.  A handler that hydrated N components paid N round trips each way.
- a8e9002bf93c473e497f7ed3bf679f5c30fa13b9 (2025-01-22, "session: Reduce IO Redis operations") solved that cost: one lazy `HGETALL`, everything in memory, one `flush` that writes back every state plus a JSON blob `__subs__`.  That flush is also where overlapping requests started to lose each other's changes: the request that flushed last wrote back every state it had read, stale ones included.  Before it, writes were per field, so only a component that both requests saved could clash.
- 07670698995c9bd742cc3858a321db80fa0b8705 (2025-07-29) added the `__children__` blob, for the recursive destroy.
- c1a19ecd33449950d866cabcbe89fadad4755504 (2026-05-24) made the flush one `MULTI`/`EXEC`, so a reader never sees half a flush.
- [PR #77](https://github.com/edelvalle/djhtmx/pull/77) (275f454102510c506b8e0def42f89cacacec6ddd) merges a flush into what Redis holds, under `WATCH`.

### What PR #77 leaves wrong

PR #77 keeps a snapshot of what the request read (`known_states`, `known_subscriptions`, `known_children`) and, at flush time, diffs it against the request's current view to guess what the request did.  A diff of two snapshots cannot tell these apart:

- "this request destroyed it" from "this request never had it";
- "another request destroyed it" from "the hash expired";
- a component destroyed and re-created with the same id in one request (the PR deletes it, or strips its subscriptions and children; `main` kept it);
- a cascade of `Destroy` computed over this request's old children map, which misses a child another request mounted, leaving an orphan that still receives signals.

Section 2 of [the review of PR #77](pr-77-review.md) has the reproductions.  The fix is not more diffing: record what the request did, when it does it.

## 2. Goal

- Two overlapping requests of one page keep each other's changes, including additions, destructions and their cascades.  Two requests that change the same field of one component still end with the last value.
- Keep what a8e9002bf93c473e497f7ed3bf679f5c30fa13b9 bought: one `HGETALL` to read, one transaction to write.
- No snapshots of what was read (`known_*` go away).

## 3. Design

### The log

The `Session` keeps, in memory, an ordered list of what the request did.  It is not stored in Redis: it lives for one request and is replayed by its flush.

| Entry | Recorded by | Carries |
|-------|-------------|---------|
| `Added(id)` | `store()` of a component the session did not hold when it was read | the whole state and the subscriptions |
| `Updated(id)` | `store()` of a component the session read | the fields whose value changed, the fields removed, and the subscriptions if they changed |
| `Destroyed(id)` | `unregister_component()` | the id only; the cascade is resolved at flush time |
| `ChildAdded(parent, child)` | `register_child()` | both ids |

`Updated` must carry fields, not the whole state: the motivating case (a name and a photo on one draft) changes two fields of the same component, and a log of whole states would bring the bug back.  `store()` computes the changed fields by comparing the component's state with the one it had when the session read it; that is local to one component, not a snapshot of the page.

Consecutive entries for the same id can be compacted (e.g. two `Updated` merge; `Destroyed` after `Added` in the same request cancels both), but order across ids matters and is kept.

The callers do not change: `store()`, `register_child()` and `unregister_component()` are already called from `CommandProcessor` (`src/djhtmx/command_processor.py`), `Repository.render_html` and `Repository.build`.  Reads (`get_state`, `get_all_states`, `get_component_ids_subscribed_to`) keep answering from the in-memory view, which the entries also update.

### The flush

`flush` runs more than once per request (after each command `CommandProcessor._run_command` processes, and in the middleware).  Each flush replays the entries recorded since the previous one, then clears them.

1. `WATCH` the hash.
2. One `HMGET` of every field the entries touch, plus `__subs__` and `__children__` (or the per-component fields, see 4.2).
3. Replay the entries in order on what Redis returned:
   - `Added` writes the state and subscriptions.
   - `Updated` applies its fields to the stored state.  If the component is not stored any more, see decision 4.1.
   - `Destroyed` removes the component, then its children, walking the **stored** children map (merged with this request's `ChildAdded`), so a child mounted by another request goes too.
   - `ChildAdded` adds the link, unless the parent is gone.
4. `MULTI`, the `HSET`s and `HDEL`s that result, `EXPIRE`, `EXEC`.  On `WatchError`, start again from 1.

The SSE liveness refresh (`refresh_sse_session_liveness` in `src/djhtmx/sse.py`) runs `EXPIRE` on the same key, which cancels the `WATCH`.  Retries are harmless but `conn.transaction` retries without a bound; bound them (and log when the bound is hit).

## 4. Decisions to take first

### 4.1. An update to a component that is gone

When an `Updated` entry finds no stored state, either another request destroyed the component, or the whole hash expired between the read and the flush.

- **Skip it.**  Right for a destroy.  For an expiry, the session comes back with only the components this request added, and the page stops working.
- **Write it back whole.**  Right for an expiry.  For a destroy, the component comes back (what `main` does today).
- **Tell them apart.**  E.g. the hash is gone entirely (no `__children__`) means an expiry; the hash is there without the component means a destroy.  Costs one more field in the `HMGET`, and needs a rule for the in-between.

### 4.2. The storage layout

Today subscriptions and children are two JSON blobs, `__subs__` and `__children__`, that every flush reads, merges and rewrites; they are what concurrent flushes fight over.

- **Keep the blobs.**  No migration; the replay merges them.
- **One field per component**, e.g. `subs:{id}` and `children:{id}` next to the state field `{id}`.  Most of the merge becomes plain `HSET`/`HDEL` done by Redis; `WATCH` is then only needed for the cascade and for two requests changing one component.  The read is still one `HGETALL`.  Hashes written in the old layout must stay readable until they expire (`SESSION_TTL`), the way a8e9002bf93c473e497f7ed3bf679f5c30fa13b9 kept reading `{id}:subs`.

Also confirm while deciding: do a component's stored subscriptions change only when `store()` runs?  `HtmxComponent.subscriptions` is an overridable property, so it may depend on more than the state; the design above assumes `store()` is the only place they are recomputed, which is true of the current code.

## 5. Steps

1. Take decisions 4.1 and 4.2; record them in this file.
2. If 4.2 picks one field per component: change the layout, with the read of the old layout.
3. Add the log and record it in `store()`, `register_child()` and `unregister_component()`.
4. Rewrite `flush` as the replay of section 3; bound the retries.
5. Remove `known_states`, `known_subscriptions`, `known_children` and the `_merge_*` helpers.
6. Remove the comment above `Session` that points to this plan, and mark this plan done.
7. CHANGELOG: one **Fixed** entry, naming what changed for an app author only.

## 6. Tests

The surface tests in `src/testbed/tests.py` (`TestOverlappingRequests`) drive two real requests of one page in a fixed order, and do not depend on how the session stores or merges.  They must pass before and after every step.

- Tests for the cases of section 1 (destroyed elsewhere, orphaned child, destroyed and re-created) are written only if the maintainer asks for them; if so, in `src/testbed`, through `Htmx` and real components.
- `test_flush_persists_children_mapping` (`src/tests/test_session.py`) runs the `conn.transaction` callback against a mock pipe and will break with the new flush.  Ask whether to remove it or rewrite it against the real Redis.
- The other tests in `src/tests/test_session.py` poke `Session` internals (`states`, `unregistered`, `children`); expect some to need the same question.

Done means `make test`, `make lint` and `make typecheck` pass.
