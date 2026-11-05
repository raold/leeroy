# Turbo worker measurement

This optional tool measures the checkout's production Turbo `load()` and `speak()` in an owned isolated worker. It does not play or record audio, alter the service, compile a model, download dependencies, or claim a speedup. Use only synthetic references and the built-in text fixtures. Run the controller in the voice environment with its existing dependencies (including psutil), and point `--python` at the separate Turbo environment's interpreter. The adapter always comes from this checkout.

Run it only while the mind and the voice service are both stopped. A measured model then competes with neither the live chat model nor a live voice worker for the GPU or the CPU, and the measurement cannot evict or slow a live one.

From the checkout's `voice` directory, for example:

```powershell
python tools/measure_turbo.py --python <turbo-python> --output <new-run-directory> --device cuda
python tools/measure_turbo.py --mode baseline --python <turbo-python> --output <new-baseline-directory> --device cuda --model <local-model-directory> --reference <synthetic-reference.wav>
python tools/measure_turbo.py --mode phases --python <turbo-python> --output <new-phases-directory> --device cuda --model <local-model-directory> --reference <synthetic-reference.wav>
```

The default `capabilities` mode loads no model. It records package and device observations, a CUDA memory admission result, whether Triton and a compiler executable were detected, and listed backend names. Detection does not establish that a backend runs or that a complete compiler toolchain is configured. Compiler execution is not attempted. Missing Triton is reported for CUDA Inductor only; other backends have independent requirements.

Full-model CUDA runs require at least 8 GiB free at admission and immediately before loading. The tool takes the lower of PyTorch's CUDA free-memory reading and the lowest physical free-memory reading reported by a five-second-bounded `nvidia-smi` query across all physical GPUs. This conservative multi-GPU rule can refuse a run because an unrelated GPU is busy. An unavailable device, missing or failed query, or malformed result refuses the model; free memory is not reserved against other applications. CPU is an explicit separate device, not a CUDA fallback. The reference must already exist, and the caller is responsible for selecting a synthetic reference. Offline environment flags are set for the child and the production loader uses local files.

Runs have 1–20 measured calls, 0–2 warmups, 1–32 threads, and a finite per-start/per-call timeout up to 300 seconds. A new output directory is required; existing runs are never overwritten. Child cache locations are redirected there, and CPU runs hide CUDA devices from the child. The controller writes `report.json` as work proceeds, and the child writes `capabilities.json`, `load.json`, and bounded `call-NNN.json` receipts. A failed run may leave partial receipts. Reports contain fixture IDs, seeds, timing, audio dimensions and a SHA-256 digest of native waveform bytes, but no text, reference paths, waveform samples or exception messages. Child stderr is discarded. Only the owned worker tree is stopped, and incomplete cleanup fails the run.

`baseline` records call-boundary synchronized wall time in the worker and separate controller round-trip time. `phases` also wraps conditioning, generation, autoregression, flow, vocoder and watermarking; unavailable and uncalled phases are explicit. CUDA phase boundaries synchronize the device, which perturbs scheduling. Generation encloses other phases, so phase durations must not be added. Warmup calls are retained separately and excluded from the summary; with zero warmups the first measured call includes conditioning. Audio is validated as nonempty finite mono and discarded after measurement. Neither duration is first-audible latency.

For comparisons use the same fixture, synthetic reference, seed, device, thread count and sampling settings. Resetting seeds for each call does not prove waveform equivalence across execution regimes. A future optimization needs paired whole-model timing and native waveform/quality and listening checks before adoption. This tool does not include a replacement or such an acceptance result.
