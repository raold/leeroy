# Synthetic similarity comparison

From the repository root, with the project's Python environment, supply an actual
pre-candidate `retrieval.py` from the baseline checkout and a new output filename:

```powershell
python mind/tools/compare_similarity.py path/to/baseline/retrieval.py comparison.json --iterations 4
```

The tool loads that baseline and this checkout's candidate explicitly. It uses
seeded synthetic data and reports every paired timing, order, exact ordered-result
checks, and exact finite similarity checks for the pairs called by MMR (plus a
direct pair for the k=0 and k=1 cases). Each timed operation includes factory
snapshot construction. Pairs alternate baseline/candidate and candidate/baseline;
the first observation and all subsequent observations are retained. The output
is created exclusively and contains hashes rather than paths, data, or results.
Run it with an even number of iterations for balanced ordering.

These are model-free kernel measurements, not application latency measurements.
The first observation is not OS-cold: no cache purge is performed. Python version,
machine load, workload, and factory setup affect results; assess real Library and
Recall paths separately before drawing conclusions or accepting a candidate.

One eight-pair comparison found identical ordered results and finite pair values.
For 50, 100, and 270 vectors at 1024 dimensions and k=5, median synthetic times
fell from 13.63 to 6.49 ms, 26.93 to 13.65 ms, and 75.65 to 36.84 ms,
respectively. The k=0, k=1, and text-only cases showed small overheads. In separate
offline whole-path comparisons, a narrow Library workload fell from 29.47 to
25.78 ms and three Recall workloads improved by about 1.52–1.60×. A broad,
SQL-dominated Library workload showed no improvement (548.23 to 563.55 ms).
All whole-path ordered IDs and scores matched. This sample does not establish
application voice latency or tail latency; results depend on the workload and
environment. The candidate adds no native dependency.
