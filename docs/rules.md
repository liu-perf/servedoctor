# The fourteen rules, and where each one came from

Every rule here is a mistake that ships. None was invented by imagining what could
go wrong — that produces a list of things that never happen, while the ones that do
are a small and unobvious subset.

Three rules say **source: this repository**. They are mistakes made while writing
this library, and they sit in the same table as everything else rather than in a
section of their own.

| rule | what it checks | source |
|---|---|---|
| SD001 | evidence level | design |
| SD002 | open vs closed loop | Tene, and `examples/coordinated_omission.py` |
| SD003 | coordinated omission | Tene |
| SD004 | quantile support | order-statistic arithmetic |
| SD005 | cold start in the distribution | tracedoctor TD003 |
| SD006 | ramp and drain | **this repository** |
| SD007 | censoring | arithmetic |
| SD008 | TPOT denominator | vLLM's own definition |
| SD009 | token composition | telemetrydoctor's aggregation contract |
| SD010 | Little's Law | **this repository** |
| SD011 | arrival process | queueing theory's own preconditions |
| SD012 | utilisation vs queueing | Pollaczek–Khinchine |
| SD013 | SLO and goodput | serving practice |
| SD014 | input hygiene | **this repository** |

---

## SD001 — what kind of evidence is this file

**Source: design.** The honesty field of this library, fourth in the series after
regressiondoctor's `noise_basis`, fitdoctor's `basis` and telemetrydoctor's
aggregation contract. Those three answer "where did this number come from" and
"what may I compute with it". This one answers **what may I conclude from this file
at all**.

A summary report is not a small per-request log. It is a different kind of object:
every distributional question has been answered irreversibly by whoever produced
it. Nine of the fourteen rules return `unknown` on one, and `unknown` is not a pass.

## SD002 — open loop or closed loop

**Source: Gil Tene's coordinated omission, plus a measurement in this repo.**

Decided from the log by two independent detectors — occupancy concentration, and
the distance from each send back to the nearest preceding completion. Full
derivation, both detectors, the falsified first version and the 16.5× measurement
are in [closed-vs-open.md](closed-vs-open.md).

Status is `violation` and not `warn` deliberately. A tail statistic from a
closed-loop run is not imprecise; it is about a different system.

## SD003 — the correction, or the refusal to invent one

**Source: Tene.** Latency measured from the time a request was *due* versus from the
time the harness managed to send it. When the harness recorded its schedule the
correction is exact — 2.07× at q99 on `EXAMPLE_open_loop_backlog.csv`, with the
harness late on 85.6% of requests, median 16.8 s late.

When it did not, the rule returns `unknown` rather than an estimate. A "corrected"
number invented from a schedule that was never recorded is worse than the
uncorrected one, because it looks like the problem has been dealt with.

With `--rate` it can still put a floor under the damage: a declared 6.4 req/s over
342.4 s implies 2191 requests; 2000 are in the file; **191 were never sent, and they
were skipped precisely while the server was slow.**

## SD004 — does the sample support this quantile

**Source: the arithmetic of order statistics.** n ≥ 299 for a bounded 90% interval
on a P99. Derivation in [quantiles.md](quantiles.md).

## SD005 — a cold start left in the steady-state distribution

**Source: tracedoctor TD003, from the other side.** There, one 80.45 ms JIT first
call dragged a mean-based launch/execute ratio to a 73× false positive, and the fix
was the median. Here the same phenomenon appears as one sample in a latency
distribution — and on a run shorter than 100 requests, one sample can be the P99
outright, because `ceil(0.99n) = n` below 100.

`EXAMPLE_cold_start.csv` is 60 requests with a 9-second first request. Dropping it
moves q99 by **+252%**. At 500 requests the identical outlier moves it by 0.8%,
which is the reason the fixture is short.

The rule refuses to trim more than 10% of a run: past that, the run is a transient,
not a steady-state measurement with a warm-up attached, and silently trimming would
hide that.

## SD006 — ramp-up and drain

**Source: this repository.** The first version defined the loaded window as the time
spent at or above 80% of the *peak* in-flight count. On `EXAMPLE_open_loop.csv` the
peak is 86 — a queue spike after a stall — against a median of 27. So the "steady
window" came out as 20 seconds of a 318-second run, and the other 94% was reported
as ramp-up.

The arithmetic was right. The reference point was a different quantity than the
name promised, which is the failure mode this entire series is about. Fixed by
referencing the median in-flight count; the peak is still printed, because the gap
between them is itself worth seeing.

## SD007 — censoring

**Source: arithmetic, from one assumption.** An unfinished request is slower than
every finished one — which is what "did not finish" means. Everything else follows
by counting. A 2% timeout rate does not perturb the P99; it deletes it. Derivation
in [quantiles.md](quantiles.md).

## SD008 — the TPOT denominator

**Source: the definition vLLM uses.** Time per output token is decode time divided
by the tokens *decode produced*, which is `m - 1`: the first token is TTFT and
belongs to prefill. Dividing by `m` understates TPOT by exactly `m/(m-1)`.

| median answer length | error |
|---|---|
| 1001 tokens | 0.10% |
| 129 tokens (`EXAMPLE_open_loop.csv`) | 0.79% |
| 4 tokens (`EXAMPLE_short_answers.csv`) | **33.33%** |
| 1 token | undefined, not zero |

Which is why it survives review on every long-generation benchmark and biases every
short-generation one — and short generation is classification, routing, ranking and
structured extraction, which is most of what production traffic actually is.

At `m = 1` the tool excludes the request rather than counting a zero. Counting it
would drag the mean TPOT down by however many single-token answers the traffic
contained, which is a property of the traffic, not the server.

## SD009 — two units called "token"

**Source: telemetrydoctor's aggregation contract, moved to a new domain.** The
arithmetic runs, the result is a number, and the number does not answer the question
it appears to answer.

`output_tokens/s` is what the decode loop produced. `(input+output)/s` is what the
request objects contained. On `EXAMPLE_short_answers.csv` — 1024-token prompts,
4-token answers — they are **14.3 tok/s and 3678.4 tok/s, a factor of 256**, and
99.6% of the second one is prompt.

The problem is not that the larger figure is inflated. It is that its value depends
on the prompt-to-output ratio of the traffic it was measured on, so two systems
measured on different traffic cannot be compared with it, and one system measured
before and after a prompt-length change appears to have changed speed.

They are also different resources: prefill is compute-bound and parallel over
sequence length; decode is memory-bandwidth-bound and strictly serial.

## SD010 — Little's Law, including where it is a tautology

**Source: this repository, as a near miss.**

L = λW is the standard sanity check on a load test. On a per-request log it is
**exactly an identity**:

```
lambda = n/T,  W = sum(lat)/n,  so lambda*W = sum(lat)/T = the in-flight integral = L
```

for any log, of any shape, with any amount of queueing, steady state or not.
Computing all three from the timestamps and observing agreement tests that division
works. This library came within one commit of shipping that as a cross-check —
telemetrydoctor had already shipped the same class of bug, where two "independent"
segmentations turned out to be one expression rearranged.

`test_littles_law_is_an_identity_on_per_request_data` asserts the equality to
floating point on three runs of different shape, so the identity cannot quietly come
back as a check.

Where the law does have force:

* **against a declared concurrency.** The harness was configured for N. The measured
  average in flight is a different number from a different source. `EXAMPLE_
  closed_loop.csv` fills its declared 7 at 100%; a client that never filled its pool
  reports a fill below 0.70 and a violation. This is the check that finds the *load
  generator* running out of CPU, and no summary statistic shows it.
* **on a summary report**, where throughput, mean latency and concurrency come from
  three different places in the harness. `EXAMPLE_summary.json` declares
  `max_concurrency: 7` while 6.278 req/s × 4.34 s implies 27 in flight — a value
  carried over from a different configuration, which is a real and common paste
  error and the only thing in a summary that can catch it.

## SD011 — the arrival process

**Source: the preconditions of every queueing result.** Reported as the coefficient
of variation of inter-arrival times: 0 is a fixed-rate generator, 1 is Poisson,
above 1.4 is bursty.

Three things this rule exists to say:

* On a **closed loop** there is no arrival process at all — the send times are the
  completion times shifted. `EXAMPLE_closed_loop.csv` has an inter-send CV of 2.08,
  which describes the server, not any schedule. A `--request-rate` flag on a
  closed-loop harness sets a ceiling, not a rate.
* A **fixed-rate** generator is honest and reproducible, and it under-queues: a
  D/D/1 queue below capacity never waits at all, while Poisson traffic at the same
  mean rate does. Tail numbers from a fixed-rate generator are a lower bound on what
  bursty arrivals produce.
* **Real traffic is often burstier than Poisson.** Fine — just do not then quote a
  Poisson bound, which is what SD012 checks.

## SD012 — utilisation against the delay it implies

**Source: Pollaczek–Khinchine, applied narrowly.** The trade-off table and the one
consistency check are in [queueing.md](queueing.md), together with a plain statement
of what the single-server formulas do *not* model.

## SD013 — attainment as a quantile, and goodput

**Source: serving practice.** Two claims.

An SLO is a statement about a quantile, not a mean. "Mean TTFT was 180 ms against a
200 ms target" is compatible with a quarter of requests missing. The rule reports the
share of requests that met every deadline, and when the mean would have passed while
the required quantile does not, it prints both numbers instead of arguing.

Throughput past saturation keeps rising while every request misses its deadline —
that is what a queue does. Goodput counts only requests that met the SLO. On the
four-rung ladder in `tests/fixtures/`:

```text
ladder_40:  2.38 req/s offered, throughput 2.38, goodput 2.17, attainment 90.8%
ladder_70:  4.41 req/s offered, throughput 4.41, goodput 3.86, attainment 87.4%
ladder_95:  5.76 req/s offered, throughput 5.76, goodput 3.02, attainment 52.4%
ladder_114: 6.17 req/s offered, throughput 6.17, goodput 2.28, attainment 37%
```

Throughput peaks at the last rung. Goodput peaks two rungs earlier. Between them,
throughput rises 40% and goodput falls 41% — and a throughput-only report recommends
the top of that range.

## SD014 — the fields nobody read

**Source: this repository.** Columns the parser did not claim are kept and listed
rather than dropped, because a column nobody parsed is exactly where an unnoticed
unit lives. Same for reconstructed timestamps: a `first_token_s` built from a
`ttft_ms` column inherits whatever rounding the harness applied before writing it,
and the reader should know which of the two they are looking at.

The rule was added after a fixture generated with `start_s, ttft_ms, e2e_ms` parsed
cleanly and silently, with no indication anywhere in the output that two of its
three timestamps had been computed rather than measured.
