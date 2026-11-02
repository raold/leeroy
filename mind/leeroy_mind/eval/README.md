# Secret-probe diagnostics

`run_secret_probes` and `record_probe` remain the only evaluation and persistence
path for a complete secret-probe gate receipt. An empty answer still aborts that
evaluation. `(nothing)` remains an explicit non-leaking answer under its existing
scoring rules. Prompts, production options, cleanup and gate policy are unchanged.

For a controlled investigation, call
`await diagnose_secret_probes(client, live_model, identity)` from
`leeroy_mind.eval.probe_diagnostics`. Pass an already admitted client under the
existing model/scheduler ownership rules. This helper does not acquire GPU leases,
load a different model, change stop strings, retry samples, or persist anything.
It runs each of the 36 scheduled samples once, retaining technical failures so a
later sample can still be observed. Cancellation or an unexpected programming
error propagates and returns no report.

The result is explicitly diagnostic-only and never certifies the gate. `complete`
means the diagnostic schedule finished, including failures; `speech` is an output
shape, not a privacy pass. `returned` counts calls that returned a result, even if
its cleaned text was malformed. Only exact cleaned `(nothing)` is classified as
explicit abstention. No privacy score is computed and no `ProbeResult` is created.

Each sample contains prompt/request hashes and optional content-free transport
metadata from `ChatResult.diagnostics` or `LLMError.diagnostics`. Raw missing,
null, invalid, empty, whitespace-only and nonempty fields remain distinguishable
from cleaned output. Metadata contains fixed labels, lengths, completion state
and strictly validated counters/timings, never response or thinking text,
exception bodies, or arbitrary provider fields. Unknown completion reasons map
to `other`. Existing fake/custom clients may omit diagnostics.

The report hashes the configured model name and exact suite/options; it cannot
identify the running model weights through this client interface. `model_digest`
is therefore explicitly unavailable. A controlled real-model run must separately
pin and verify model residency/digest before and after the schedule. Keep any
reports private. Do not relabel a diagnostic report, selectively retry empty
samples, or change a prompt/options arm to obtain a production gate receipt.
