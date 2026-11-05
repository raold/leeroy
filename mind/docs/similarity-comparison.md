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

The tool uses `VEC_DIM` (512), the size of every stored memory and library
vector. One eight-pair comparison at that size (Python 3.14.10) found identical
ordered results and bit-identical similarity values. For 50, 100, and 270 vectors
at k=5, median synthetic times fell from 6.84 to 3.35 ms, 14.06 to 6.73 ms, and
41.38 to 19.47 ms, respectively (about 2.0–2.1×). The k=0, k=1, and text-only
cases showed no meaningful difference (0.02, 0.05–0.06 and 0.85–0.86 ms). An
earlier run at 1024 dimensions showed the same ratio. In separate
offline whole-path comparisons, a narrow Library workload fell from 29.47 to
25.78 ms and three Recall workloads improved by about 1.52–1.60×. A broad,
SQL-dominated Library workload showed no improvement (548.23 to 563.55 ms).
All whole-path ordered IDs and scores matched. This sample does not establish
application voice latency or tail latency; results depend on the workload and
environment. The candidate adds no native dependency.
