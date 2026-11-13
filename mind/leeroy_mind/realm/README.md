# Recall holds and operator release

## Lost ACKs and native restarts

A correlated terminal job takes precedence over a lost ACK. Once its transport
settles, a complete `bot_do` receipt or a done `realm_job` completes the action
even if the ACK timed out. Arrival order does not change the result. Failed jobs
retain their existing rules: proved no-change refusals free the manifest;
unproved failures stay `job_failed`, holding whatever native has not released.
Skipped members retain their existing 600-second backoff. Mind startup also
rechecks older unknown rows carrying terminal evidence.

The timeout's `transport_unknown` callback is a non-final notice: Recall waits
for the job's real result, including across its periodic ticks. A terminal result
then advances the same rearm, leave or defaults chain in either arrival order.
A done WG cleanup job also completes its WG root after the cleanup transport
settles, even when its ACK was lost, in either arrival order.

Every new dispatch records the adopted world's `started_ms` in its evidence.
A hello with a different, later process start proves that the old process's
in-memory reservations are gone, even if the wall clock stepped backward after
that process started. Settled unknown roots and positively acked dispatched roots
without terminal evidence qualify for ordinary rearm, ordinary leave, or
Recall's exact defaults command.
There must be no children, continuation, declared scope or conflicting evidence;
WG, ACK/result commands, guild growth and other kinds remain excluded. The hello
must match the current guild and owner. A hello from the same native process
does nothing. If the new hello arrives while a transport is still in flight,
its proof is rechecked when the transport actually settles.

Such a root becomes `completed` with result `restart_released`, **not** successful.
Its permanent `realm:action:restart_released:<id>` receipt stores the hello start,
release time and complete prior row. Neither this row nor its receipt is pruned.
Health and Ops no longer count it as a hold, and `realm release` explains that it
is no longer unknown. No command is resent by this recovery: Recall decides its
next fresh command under its normal gates. A half-done rearm/defaults batch is
safe to reapply; a half-done leave must leave again under the existing recall
rules. This is the same process-start proof used by training's lab-clear.
The old unknown's generic backoff does not survive this proof; specific native
skip backoffs still do. A later contradictory receipt remains a conflict hold.

This recovery change needs only a mind release and restart, no migration or
native rebuild. Older native without `started_ms` retains conservative holds.
Unresolved rows written before dispatch process identity was recorded also stay
held: a dispatch wall-clock time alone cannot identify their native process.
An unknown legacy row can still be reviewed and released by the operator;
`realm release` continues to refuse dispatched rows. Health names a worldserver
restart for an eligible acked dispatched root and the release command for an
unknown root. Previously recorded restart releases remain readable.
Rollback to a mind version without these new evidence keys requires restoring
the pre-release database snapshot, as for the newer leave evidence below.

An ordinary `leave_pvp` can finish unknown while holding only part of its original manifest.
Native's failed `realm_job.released` is release proof for distinct members of that exact root.
The journal keeps the full original manifest and payload; its holder projection subtracts only
validated released members. Other policies/kinds, out-of-manifest values, duplicates, negative
ACKs and conflicting evidence cannot free bodies through this field. No stored root is replayed.

Native final readback waits up to 60 seconds for teleport or pending exit restoration. It then
skips a member only when the job provably never changed that body. Its own completed or pending
step, shared cleanup, scopes and continuations all count as possible changes. Changed members
remain held on failure. `realm_action_unknown` and `realm-holds` name `readback_guid` and a fixed
`check` code when supplied. A cleanup recheck fault the readback codes do not see (a Wintergrasp
queue, a training fence, an LFG proposal) is named by its own reason's code (`queue`, `owner`,
`lfg`, `offline`); native never sends a readback without one of the nine codes. The existing job
`guid` still names the step, not necessarily the member that failed readback. Logs contain ids,
codes and counts, never character names or text.

Release proof shows only that native no longer holds a body, not that it left PvP: native also
releases the members after the failing step, whose steps never ran. So a failed leave never leads
to rearm or defaults. Its released members that it did not skip, when they are fewer than its
manifest, get a new leave as a new command, as a named ack refusal's rest does (each retry strictly
smaller; a no-op for those already out), and only that leave's success rearms them and restores
their defaults. A whole-job refusal, or a failure that released everyone, ends the chain. The end
of that leave's hold hands none of its members back to defaults either (the recall's release
callback keeps them out for 60 s), nor a member a done leave skipped or an ack refusal named. The
failed root's 600 s backoff covers its held and skipped members, not those released ones. Skipped
members back off for 600 seconds even when the leave ends unknown. A failed leave without new
release evidence retains the full manifest, as before.

The Ops Realm panel and the awareness card count released members as free, as the mind's holds
do. `realm-holds` treats an unknown leave whose release proof covers every member as a note (it
holds nobody), naming its root and release command; anything else it found still fails.

## Operator procedure

Inspect the native situation before releasing a stale hold. With the mind stopped, use:

```text
python -m leeroy_mind realm release <root-id> --reason reviewed_stale_hold
```

The CLI displays the root and requires its exact id again. The Ops console's Realm release (with
`[control] ops_actions = true`) never releases while the mind runs: it stops the mind cleanly,
runs this checkout's `realm release --yes`, then starts the mind again, which then sends
`realm_forget`. Existing admission rules still refuse
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
| `running` | No mutation; the job is still in flight. Asked again every 60 s. |
| `kind` | No mutation; kind/policy, scopes, continuation or guild joins require native recovery. |
| `busy:teleport` | No mutation; asked again after 5 s, doubling to 60 s. |

`released` and `not_held` write `realm:action:forgotten:<root-id>` with outcome, member count and
time. Success requires both the matching positive ACK and result. Otherwise at most one request
per root per connection is sent, except those timed retries; `kind` and unanswered requests wait
for a new connection. Roots already attempted on a connection are left out of the bounded
discovery query, so old refusals never hide a newer root. An exact unknown-command reply for this
fresh envelope records `unsupported` once. Both sides log
`realm_forget id=<root-id> outcome=<code> members=<count>`. A failure reading the journal or meta
logs `realm_forget_failed: <exception class>` and sends nothing; the Realm loop goes on.

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
off, and the other 107 get a new, smaller leave (never the original replayed), then rearm and
defaults once it succeeds. Native pure tests
separately exercise every check code, wait expiration, partial current-step failures, scoped and
continuation protection, and mutation-free forget refusals.
