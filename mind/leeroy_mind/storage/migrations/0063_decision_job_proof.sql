-- Evidence bound to the immutable command id and payload hash in realm_direct.
CREATE TABLE decision_job_proof (
  command_id TEXT PRIMARY KEY REFERENCES realm_direct(command_id) ON DELETE CASCADE,
  actor INTEGER NOT NULL CHECK(actor BETWEEN 1 AND 4294967295),
  expected_count INTEGER CHECK(expected_count > 0),  -- NULL means the first attachment item GUID on confirmed mail
  recovered_reason TEXT
);

-- Older withdrawal reconciliation named both refs before mail admission.
UPDATE guild_decision SET effect_ref=json_set(effect_ref,'$.refs',json_array('decision:'||id||':withdraw'))
 WHERE kind='bank' AND json_extract(effect_ref,'$.realm_bank') IS NOT NULL
   AND json_extract(effect_ref,'$.refs[1]')='decision:'||id||':mail'
   AND NOT EXISTS (SELECT 1 FROM realm_direct r WHERE r.owner='decision' AND r.kind='mail_send'
                   AND r.ref='decision:'||guild_decision.id||':mail' AND r.dispatched_ms IS NOT NULL);
