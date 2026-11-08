# Realm job receipts

The bridge's existing `realm_job` event may include `"changed": false` on a
failed body job. It proves that **the entire job** changed no body and made no
cleanup mutation, and that native released its reservation. The `reason`, `guid`
and `value` fields retain their meanings. An absent field means unknown; `true`
also provides no refusal proof. A successful job does not carry the field.

`RealmModule::Tick` combines an executor's explicit `RealmStep.unchanged` proof
with every previously completed member's proof. Before an executor has run,
the step is unchanged only if it has no pending work. Retention, declared scopes,
a continuation, any guild-join attempt, or shared cleanup mutation disqualifies
the receipt. An exception or unproved post-step identity never provides proof.

The ordinary leave executor proves an unchanged refusal only before its saved
`changed` flag or `cleanup_mutated` flag is set. Completed no-op members carry
that proof forward, so a later Wintergrasp refusal can safely end a large leave;
an earlier changed or unproved member prevents it. Rearm's unchanged refusals
precede all weapon swaps; its completed no-op steps also carry explicit proof.
Other executors keep the default (unproved) unless rejected before execution.

The mind requires a positive ack, a correlated failed receipt with zero value,
a member in the reserved manifest, no PvP owner token, and an ordinary root
without children. It settles that root as `step_refused`, holding nobody. An
uncontested stored unknown with these receipts is reconsidered at startup.
Later contradictory evidence still becomes `conflict` and holds the manifest.
WG continuation, ack-only and result roots keep their existing rules.

Old native omits the field, preserving the reason-based rules. Old minds ignore
unknown wire fields and retain their existing settlement behaviour. Rolling the mind back past
this change is different: the new mind stores `changed` in `realm_action` evidence, and an older
mind cannot read such a row (its journal reads fail, so the Realm stops moving bodies until the
key is stripped from those rows). Take a snapshot before deploying and roll back with it. Both a
native rebuild and an updated mind are needed for the new proof to take effect.
