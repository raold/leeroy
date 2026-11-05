ALTER TABLE realm_kill ADD COLUMN qualification_json TEXT
  CHECK(qualification_json IS NULL OR
    (fact IS NULL AND typeof(qualification_json) = 'text' AND
     CASE WHEN json_valid(qualification_json) THEN
       json_type(qualification_json) = 'object'
       AND json(qualification_json) = qualification_json
       AND length(CAST(qualification_json AS BLOB)) <= 8192
     ELSE 0 END));