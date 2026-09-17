# servedoctor

**Did this benchmark measure the server, or the load generator?**

Point it at the output of an inference-serving load test. It decides whether the
offered load was independent of the server's own speed, whether the sample supports
the quantile printed from it, and whether the numbers can be compared with anything
— and when the answer is no, it says what would have been needed instead.

```console
$ servedoctor audit tests/fixtures/EXAMPLE_closed_loop.csv --rate 6.4 --concurrency 7
source: [info] tests/fixtures/EXAMPLE_closed_loop.csv: per_request_csv, 2000 requests; span 342.42s
evidence: [info] per-request log, 2000 requests. Every rule below is available.
closure: [violation] occupancy sits at 7 for 99.5% of the run, and every send lands a
  median of 0.000 inter-send gaps after a completion -- that is a worker pool of 7, not
  a schedule. Offered load was therefore a function of the server's own speed: when it
  slowed down, this harness sent less. [...]
omission: [unknown] no schedule column: [...] A declared rate of 6.4 req/s over 342.4s
  implies 2191 requests; 2000 are in the file, so 191 were never sent -- and they were
  skipped precisely while the server was slow.
q99: [warn] q99=2.8589s, 90% CI [2.6513, 10.7950] from order statistics 1972-1988 of
  2000 (21 observations in the tail, interval is 285% of the point estimate). Quote the
  interval, not the point.
```

That last line is the whole library in one finding. The run printed a P99 of 2.86
seconds. Its own 90% confidence interval reaches 10.80. **The number is not
slightly uncertain — it is compatible with a value four times larger**, and nothing
in the report it came from says so.

Zero dependencies, Python 3.9+, CPU-only CI.

## Three findings

### ① A closed-loop harness reports a tail 16× shorter for the same server

`examples/coordinated_omission.py` runs on any machine with a Python and no GPU:
a mock server with 8 slots, 80 ms of service, freezing for 1.2 s every 10 s. Two
harnesses, both configured for 80 req/s — one open-loop, one with the 6 workers an
operator derives from `80 × 0.080 s`. Three consecutive runs on this machine:

| | delivered rate | q50 | **q99** | max |
|---|---|---|---|---|
| closed loop, 6 workers | 65.2 req/s (**81%** of configured) | 0.082 s | **0.084 s** | 1.28 s |
| open loop, 80 req/s | 77.5 req/s (97%) | 0.66 s | **1.39 s** | 2.37 s |

**q99 differs by 16.5×. The maxima differ by 1.8×** (16.52× and 1.86× on the
run tabulated above; 1.8× is the round figure across four runs, tabled in
[`docs/closed-vs-open.md`](docs/closed-vs-open.md) — 1.86 / 1.69 / 1.76 / 1.88).

The closed loop *did* see the stall — it is right there in its worst case. It never
had enough requests exposed to one for the stall to reach a percentile: 6 workers ×
2 stalls = 0.80% of 1500 requests, and the P99 lives in the top 1%. The open loop
exposed 96 per stall, 12.8%.

That is a sharper statement than "the tail is truncated", and it has a practical
consequence you can use on someone else's report today: **on a closed-loop run, a
large gap between the maximum and the P99 is itself the warning sign.**

Two more things fall out of the same run. The closed loop silently delivered 81% of
the rate it was configured for, because a closed loop's rate is an *output* of the
server's speed and not an input to it — so it never tested the load it was asked to
test. And the closed run cannot bound its own P99: the 90% interval spans 15×,
because the distribution is two spikes with nothing between them.

Ratios across four runs: 16.52× / 16.63× / 16.53× / 16.48×; the maxima
1.86× / 1.69× / 1.76× / 1.88× apart.

### ② A P99 needs 299 requests before it has an upper bound at all

Not a rule of thumb. The number of observations at or below the true q-quantile is
Binomial(n, q), so a confidence interval for the quantile is a pair of order
statistics from that binomial's tails. The upper rank falls inside the sample only
when

```
q**n <= alpha/2        i.e.   n >= ln(alpha/2) / ln(q)
```

q=0.99 at 90% confidence → **n ≥ 299**. At 95% → 368. For a P99.9 → 2995.

Below that, the largest observation is not an upper bound on the P99; it is simply
the largest thing drawn. So the tool refuses to report the number and says what the
sample *can* carry:

```console
$ servedoctor audit tests/fixtures/EXAMPLE_short_200.csv --rule SD004
q99: [violation] n=200 but a bounded 90% interval on q99 needs n>=299. Only 3
  observation(s) sit at or above the reported 3.0750s, and the upper confidence rank
  falls off the top of the sample [...] This sample can carry q98.51 -- report that, or
  send 99 more requests.
```

No bootstrap, no resampling, no seed: the interval is a deterministic function of
n and q, which is also why the tests can pin it exactly.

### ③ 2% timeouts do not perturb the P99 — they delete it

Requests that failed or were still running when the harness stopped are dropped
before the statistics are computed. They are not a random subset: an unfinished
request is, by definition, slower than every finished one. Drop the top fraction
`f` and the reported q-quantile is really the `q(1-f)`-quantile — and when
`q > 1-f`, the quantile you asked for lies **entirely inside the discarded part**.

```console
$ servedoctor audit tests/fixtures/EXAMPLE_censored.csv --rule SD007
censoring: [violation] 12 of 600 requests (2.00%) did not finish and were dropped.
  Since an unfinished request is slower than every finished one, the top 2.00% of the
  true distribution is exactly the part that was discarded -- and q99 lies inside it.
  The observed q99 of 11.3064s is not an estimate of the true one; it is the q99 of the
  survivors.
```

At q95 on the same file the correction is available and the tool applies it, because
0.95 < 0.98. The refusal is not a policy; it is where the arithmetic runs out.

## Install

```bash
pip install -e .
```

## The fourteen rules

| | |
|---|---|
| **SD001** | what kind of evidence this file is, and therefore what may be concluded from it |
| **SD002** | open loop or closed loop, decided by two independent detectors |
| **SD003** | coordinated omission: latency from the send time vs from the time due |
| **SD004** | does the sample support the quantile — exact interval, refusal below n=299 |
| **SD005** | a cold start left in with the steady-state requests |
| **SD006** | how much of the run was ramp-up and drain rather than steady state |
| **SD007** | failed and unfinished requests dropped from the tail they define |
| **SD008** | TPOT divided by m instead of m−1 |
| **SD009** | prompt tokens and output tokens added into one rate |
| **SD010** | Little's Law against a declared concurrency — and where it is an identity |
| **SD011** | the arrival process, which every queueing claim depends on |
| **SD012** | utilisation against the queueing delay it implies |
| **SD013** | SLO attainment as a quantile, and goodput rather than throughput |
| **SD014** | fields this tool could not read, which are the ones nobody checked units on |

`docs/rules.md` gives each one a provenance. Three of them say **source: this
repository**, because they are mistakes made while writing it.

## Commands

```bash
servedoctor audit     run.csv --ttft-ms 500 --tpot-ms 50 --concurrency 64
servedoctor latency   run.csv          # quantiles with exact confidence intervals
servedoctor closure   run.csv          # open or closed loop, and how it was told
servedoctor slo       run.csv --e2e-ms 2000
servedoctor queueing  run.csv          # the utilisation/latency trade-off table
servedoctor sweep     rate_*.csv --e2e-ms 2000   # where goodput turns over
servedoctor compare   before.csv after.csv
servedoctor rules
```

Output is `{location}: [{status}] {message}`, the same shape as the other six tools
in this series. `--json` for machines. `--fail-on` is a **threshold**, not an
equality, so `--fail-on warn` also trips on `violation`. `--fail-on unknown` is a
reasonable CI setting: a benchmark that cannot be checked has not passed a check.

Exit codes: 0 clean or gate not reached, 1 gate tripped, 2 could not read the input.

## Input

Two shapes, and the difference between them is recorded rather than smoothed over:

**Per-request CSV** — one row per request with absolute timestamps. Column names
are aliased liberally (`start_s`/`start`/`send_s`/…), and `start_s, ttft_ms, e2e_ms`
is reconstructed into absolute times with the reconstruction reported. This is the
shape almost everything here needs.

```csv
request_id,arrival_s,start_s,first_token_s,end_s,input_tokens,output_tokens,ok
0,0.128,0.128,0.311,1.402,517,131,1
```

`arrival_s` is the time the request was *due* and `start_s` the time it actually
went out. If your harness has a schedule, record it: it is what turns coordinated
omission from an argument into a subtraction.

**Summary JSON** — the shape `vllm bench serve` and friends print at the end. Read,
and then nine of the fourteen rules return `unknown`, because the distributional
questions were answered irreversibly before the file was written. That is not a
defect in the report. It is what a summary is.

## Goodput, and the rung a throughput report recommends

```console
$ servedoctor sweep tests/fixtures/EXAMPLE_ladder_*.csv --e2e-ms 3000
EXAMPLE_ladder_40.csv:  [info] 2.38 req/s offered, throughput 2.38, goodput 2.17, attainment 90.8%
EXAMPLE_ladder_70.csv:  [info] 4.41 req/s offered, throughput 4.41, goodput 3.86, attainment 87.4%
EXAMPLE_ladder_95.csv:  [info] 5.76 req/s offered, throughput 5.76, goodput 3.02, attainment 52.4%
EXAMPLE_ladder_114.csv: [info] 6.17 req/s offered, throughput 6.17, goodput 2.28, attainment 37%
knee: [violation] throughput peaks at EXAMPLE_ladder_114.csv but goodput peaks at
  EXAMPLE_ladder_70.csv. Every rung between them serves more requests and fewer useful
  ones; a throughput-only report recommends the first, and users experience the
  difference.
```

Throughput rises by 40% across those two rungs. Goodput falls by 41%.

## Limits

Written down because a tool that does not say what it cannot do invites you to
assume it did.

1. **A closed-loop log cannot be corrected, only labelled.** With no recorded
   schedule there is nothing to measure the omission against, and this tool will
   not invent one. The bound it offers (`--rate` → requests never sent) is a floor,
   not the damage.
2. **Utilisation needs `--capacity`.** `λ·E[S]` is offered concurrency, not
   utilisation, on any server that batches — and an earlier version of this file
   clamped it and produced a 398-second queueing "floor" for a run whose entire
   tail was 13 seconds. Without a measured saturated throughput, SD012 reports the
   trade-off table and declines to place the run on it.
3. **The queueing formulas are a shape, not a model of a batching server.** M/M/1
   and M/D/1 describe a single server with an admission queue. Continuous batching
   is not that. They are used for the trade-off table and for one consistency check
   whose assumptions the data is tested against — never as a prediction.
4. **Open and closed are not always distinguishable.** A rate-limited harness that
   hit a concurrency cap *is* a closed loop past that point; a perfectly periodic
   schedule against constant service is indistinguishable from one by construction.
   Both come back `inconclusive`, which is a different answer from either.
5. **Nothing here checks whether the *responses were correct*.** A server that
   returns garbage quickly scores beautifully.

## The seventh and last of a series

measure it (**nodebench**) → read the measuring script (**benchdoctor**) → read the
kernel trace (**tracedoctor**) → compare two runs (**regressiondoctor**) → what
should the number have been (**fitdoctor**) → what arithmetic does this monitoring
column support (**telemetrydoctor**) → **did this benchmark measure the server or
the load generator**.

The honesty field is the fourth generation of one idea: `noise_basis` (is this
threshold measured or assumed), `basis` (where did this number come from),
aggregation contract (what may I compute with this column), and here `evidence`
(what may I conclude from this file at all).

## License

MIT.
