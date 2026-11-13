# Recall holds and operator release

An ordinary `leave_pvp` can finish unknown while holding only part of its original manifest.
Native's failed `realm_job.released` is release proof for distinct members of that exact root.
The journal keeps the full original manifest and payload; its holder projection subtracts only
validated released members. Other policies/kinds, out-of-manifest values, duplicates, negative
ACKs and conflicting evidence cannot free bodies through this field. No stored root is replayed.

Native final readback waits up to 60 seconds for teleport or pending exit restoration. It then
skips a member only when the job provably never changed that body. Its own completed or pending
step, shared cleanup, scopes and continuations all count as possible changes. Changed members
remain held on failure. `realm_action_unknown` and `realm-holds` name `readback_guid` and a fixed
`check` code when supplied. The existing job `guid` still names the step, not necessarily the
member that failed readback. Logs contain ids, codes and counts, never character names or text.

Skipped members back off for 600 seconds even when the leave ends unknown. Recall rearms and
restores defaults only for released members that were not skipped. A failed leave without new
release evidence retains the full manifest, as before.

## Operator procedure

Inspect the native situation before releasing a stale hold. With the mind stopped, use:

```text
python -m leeroy_mind realm release <root-id> --reason reviewed_stale_hold
```

The CLI displays the root and requires its exact id again. The Ops console's Realm release uses
the same abandonment journal while the mind is running. Existing admission rules still refuse
WG, ACK/result roots, children, unsettled transport, and conflicts. A later contradictory original
receipt restores a conflict hold; abandonment cannot erase native evidence.

The abandonment meta receipt is `realm:action:abandoned:<root-id>`. On its next tick (or after
restart), the mind asks native to forget an eligible retained ordinary leave/rearm. This cleanup
works even with Realm behavior disabled. It uses a new `realm_forget` envelope id and a separate
`origin_id` target, so a late original ACK/job cannot settle the forget command or vice versa.
The receiver checks the current connection, guild/human identity, exact abandonment receipt and
still-abandoned root before sending or accepting success. It never enrolls forget in ActionQueue.

Native answers with a correlated `realm_forgotten` event and ACK:

| Outcome | Effect |
| --- | --- |
| `released` | Native ends every eligible lease/holder; includes member count. |
| `not_held` | Native has no reservation, including after a worldserver restart. |
| `running` | No mutation; the job is still in flight. |
| `kind` | No mutation; kind/policy, scopes, continuation or guild joins require native recovery. |
| `busy:teleport` | No mutation; retry on the next mind tick. |

`released` and `not_held` write `realm:action:forgotten:<root-id>` with outcome, member count and
time. Success requires both the matching positive ACK and result. Otherwise at most one request
per root per connection is sent, except explicit teleport-busy retries. An exact unknown-command
reply for this fresh envelope records `unsupported` once. Both sides log
`realm_forget id=<root-id> outcome=<code> members=<count>`.

## Deployment and rollback

This needs a native rebuild. Claude releases native and mind together with the owner offline.
There is no schema migration; migration 0040 is untouched.

* **New mind / old worldserver:** absent release/readback fields preserve conservative full holds.
  Unknown `realm_forget` settles `unsupported`, avoiding repeated requests. Reviewed operator
  release frees the mind journal only; native fences still need native recovery or restart.
* **Old mind / new worldserver:** old mind ignores the new wire fields and may retain the whole
  failed manifest; it cannot request native forget. Reviewed restart plus operator release remains
  available. Do not expect the new narrower mind holds until both components are updated.
* **Mind rollback after new evidence was stored:** an old strict journal reader does not understand
  `released`, `readback_guid` or `check`. Restore the pre-deployment snapshot with the rollback;
  do not edit or strip exact receipts to make them readable.

The 109-member rehearsal uses invented identities and an actual FakeBridge/MindApp connection:
the last step waits for untouched member 101's teleport, skips it after 60 s, then finds changed
member 102 owned elsewhere. The unknown root holds only 102; 108 members are released, 101 backs
off, and the other 107 proceed to rearm/defaults without replaying the leave. Native pure tests
separately exercise every check code, wait expiration, partial current-step failures, scoped and
continuation protection, and mutation-free forget refusals.
