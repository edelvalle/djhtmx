# Review of PR #77: merge a flush into the stored session

Pull request: [edelvalle/djhtmx#77](https://github.com/edelvalle/djhtmx/pull/77), branch `fix/overlapping-requests-lose-changes`.  Its commits are 275f454102510c506b8e0def42f89cacacec6ddd (the fix in `src/djhtmx/repo.py`), 6f7311e1fb7eb88795b16cb0cce77f63f6587c3c (the CHANGELOG entry) and e0e2e79ff98e2edd5577f890bdf3fc5896cd6636 (the tests in `src/tests/test_session.py`).

## Status

The fix is sound: two overlapping requests of one page no longer lose each other's changes.  `make test` (196 tests), `make lint` and `make typecheck` pass on the branch.

What remains: simplify the three `known_*` attributes, decide about two edge cases of the merge (neither is a regression), replace the tests with one that drives real concurrent requests, and a few smaller points.  Each section below is one piece of work.

## 1. The three `known_*` attributes

`Session.known_states`, `known_subscriptions` and `known_children` (`src/djhtmx/repo.py:510-513`) hold the base that a flush diffs against.

1. **One snapshot instead of three.**  Three parallel copies force a manual copy of every set (`set(signals)` in `_ensure_read` and in `_remember_what_is_known`).  If one copy is forgotten, the snapshot aliases the live set, the diff sees no change, and the update is silently dropped.  A single `dict[str, bytes]`, holding exactly what `hgetall` returned (and, after a flush, what this session wrote), removes the copying.  `__subs__` and `__children__` would be decoded only at flush time, the one place that needs them.
2. **They are constructor arguments.**  `Session` is a dataclass, so `Session(id, known_states=...)` is accepted.  Declare them with `Field(default_factory=..., init=False, repr=False)`.  Minor: `read`, `is_dirty` and `states` are constructor arguments too.
3. **The comment is wrong after a flush.**  It says they hold what the session "last read from, or wrote to, Redis".  `_remember_what_is_known` copies the request's own `states`, not the merged value that was actually written.  That is the right base for the next diff, but the comment must say so.  `self.states` also keeps the un-merged copy, so whatever reads the session later in the same request sees the old view; `main` behaves the same, so this is not a regression.
4. **Open question for the maintainer: is `known_subscriptions` needed at all?**  `store()` updates subscriptions together with the state.  If `HtmxComponent.subscriptions` depends only on the state, the flush can write `__subs__` just for the components whose state changed, plus the unregistered ones.  It is an overridable property, so it may depend on more than the state.  Ask before removing it.

## 2. Edge cases of the merge

Both were reproduced with two `Session`s on a real Redis hash.  Neither is a regression: `master` gets both wrong too.  Whether they are fixed in this PR or in a follow-up is the maintainer's call; ask before doing it.

- **A destroyed component comes back as a half-component.**  Request A destroys `photo` while request B edits its caption, and A flushes first.  In `_merge_state` (`src/djhtmx/repo.py:646`), `stored is None` makes B write its whole state back.  The hash ends with `photo`'s state, no `__subs__` entry and no parent.  It is still found by `get_components_by_names`, so it still reacts to `Emit` until the TTL expires.  On `master` it came back whole, also wrong.  Fix: when `known` is set but `stored is None`, another request deleted the component, so skip it and drop its subscriptions.
- **A child is left without a parent.**  Request B mounts `thumb` under `photo`, then request A destroys `draft`.  A cascades over its own, older, children map, which lacks `thumb`.  The hash ends with `thumb`'s state and subscriptions and `__children__ == {}`: an orphan that still receives signals.  Fix: cascade the removal over the merged children map built in `_merge_children`, not over the local one.

- **A caveat for the first fix.**  "`known` is set but `stored is None`" also describes a hash that expired between the read and the flush.  Skipping the component then brings the session back with only what this request created, which is worse than today.

The CHANGELOG entry promises more than the PR delivers in these two cases.

One more case is a regression, though an unlikely one: **a component destroyed and re-created with the same id in one request is lost.**  `store()` never takes the id out of `unregistered`.  If the new state equals the old one, the id is not in `changed`, so the flush runs the `HDEL` and no `HSET`.  If the state differs, the state survives, but `_merge_subscriptions` and `_merge_children` drop every unregistered id at the end, so the component loses its subscriptions and children.  Reproduced on a real Redis with identical states: the hash ended as `{'__subs__': '{}', '__children__': '{}'}`.  On `main` the flush writes the whole `states` and `subscriptions` after the `HDEL`, so the component survives.

## 3. Tests

`TestOverlappingRequests` (`src/tests/test_session.py:570`) builds the overlap by hand: it calls `_ensure_read`, writes JSON into `session.states` and sets `is_dirty`.  No component, handler or request is involved.  The project's convention is to test from the surface, through `djhtmx.testing.Htmx` and a Django `Client` with real components.

Replace it with a test where two clients post to the htmx endpoint at the same time.  The prototype below fails on `master` (the name is lost) and passed eight runs out of eight on the PR branch.

- Both handlers wait on a `threading.Barrier(2, timeout=5)`, so each request has read the session before either flushes.  The timeout keeps a broken run from hanging.
- The test must be a `TransactionTestCase`: the requests run in threads, and the test settings use SQLite with `ATOMIC_REQUESTS`.
- `htmx.get_component_by_type(Draft)` reads the session fresh from Redis, after both requests ended.

Component, appended to `src/tests/fision/todo/htmx.py` (plus `import threading` at the top):

```python
# Both handlers wait here, so the two requests have each read the session before either flushes.
overlapping_requests = threading.Barrier(2, timeout=5)


class Draft(HtmxComponent):
    _template_name = "todo/Draft.html"
    name: str = ""
    photos: tuple[str, ...] = ()

    def attach_photo(self, photo: str):
        overlapping_requests.wait()
        self.photos = (*self.photos, photo)

    def rename(self, name: str):
        overlapping_requests.wait()
        self.name = name
```

Template `src/tests/fision/todo/templates/todo/Draft.html`:

```html
{% load htmx %}
<form {% hx-tag %}>
  <input name="name" value="{{ this.name }}" {% on "change" "rename" %}>
  <button name="photo" value="photo-cid" {% on "attach_photo" %}>Attach</button>
  <ul>{% for photo in photos %}<li>{{ photo }}</li>{% endfor %}</ul>
</form>
```

Page `src/tests/fision/todo/templates/draft.html`, with a `draft` view in `views.py` rendering it and `path("draft", views.draft, name="draft")` in `urls.py`:

```html
{% load htmx %}
<!DOCTYPE html>
<html><head>{% htmx-headers %}</head><body>{% htmx 'Draft' %}</body></html>
```

Test:

```python
from concurrent.futures import ThreadPoolExecutor

from django.test import Client, TransactionTestCase

from djhtmx import json
from djhtmx.testing import Htmx

from .htmx import Draft


class TestOverlappingRequests(TransactionTestCase):
    def test_two_requests_of_one_page_keep_both_changes(self):
        htmx = Htmx(Client())
        htmx.navigate_to("/draft")
        [form] = htmx.select('[hx-name="Draft"]')
        headers = json.loads(form.attrib["hx-headers"])
        [rename_url] = [e.attrib["hx-post"] for e in htmx.select('input[name="name"]')]
        [attach_url] = [e.attrib["hx-post"] for e in htmx.select('button[name="photo"]')]

        def post(url, data):
            return Client().post(url, data, headers=headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            uploading = pool.submit(post, attach_url, {"photo": "photo-cid"})
            typing = pool.submit(post, rename_url, {"name": "Damper is stuck"})
            self.assertEqual(uploading.result().status_code, 200)
            self.assertEqual(typing.result().status_code, 200)

        draft = htmx.get_component_by_type(Draft)
        self.assertEqual((draft.name, draft.photos), ("Damper is stuck", ("photo-cid",)))
```

This is a prototype: rename and tidy it to match `src/tests/fision/todo/tests.py`.  The PR's second scenario (a child mounted while the other request flushes) can be written the same way, with a handler that yields `BuildAndRender`.  If the edge cases of section 2 are fixed, they can be covered the same way too, but only if the maintainer asks for those tests.

`test_flush_persists_children_mapping` (`src/tests/test_session.py:128`) now takes the callback out of `mock_conn.transaction.call_args` and runs it against a `MagicMock` pipe.  That ties the test to how `flush` is implemented.  These tests already have a real Redis, so either use it or remove the test; ask the maintainer which.

## 4. Smaller points

- **CHANGELOG (`CHANGELOG.md:13`).**  The entry explains the cause and the mechanism ("a flush wrote back…", "merged into what Redis holds, under `WATCH`").  The changelog names only what changed for an app author, with no internals.  Suggested: "**Two requests of one page no longer undo each other's changes**, unless both change the same field of one component; then the last one wins."
- **Comment in `flush` (`src/djhtmx/repo.py:592-596`).**  Five lines describing the mechanism; one line naming the why is enough.
- **Two round trips in `_write_changes` (`src/djhtmx/repo.py:620`).**  Read everything with one `pipe.hmget(key, [*changed, "__subs__", "__children__"])`.  Also annotate `pipe` (a redis-py `Pipeline`).
- **The field merge skips validation.**  It can store a mix of two states that were each valid alone but not together, e.g. fields that depend on each other.  Say so where an app author sees the behaviour (the docstring of `flush`, or the CHANGELOG line about "same field, last wins"), not on the private `_merge_state`.
- **Extra retries from SSE.**  The SSE liveness refresh (`src/djhtmx/sse.py:419`) runs `EXPIRE` on the same key, which cancels the flush's `WATCH`, so pages with an open SSE connection retry more often.  Harmless, but `conn.transaction` retries without a bound.

## Project rules that apply

Read `AGENTS.md` at the repository root before changing anything.  In short: no new tests unless asked, no `continue`, `break` or non-trivial `return` in loops, `match` with `assert_never` instead of `_`, one-line comments that give the why, and no commits unless asked.
