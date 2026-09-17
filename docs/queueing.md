# "Keep the GPUs busy" and "keep the tail short" are one dial

Every capacity conversation eventually contains two sentences that cannot both be
satisfied. They are not competing preferences. They are one quantity read from two
ends, and the relationship is arithmetic.

## The table

For a queue fed by arrivals it does not control, mean waiting time grows as
`1/(1-rho)`. In units of one service time:

| utilisation ρ | M/M/1 wait | M/D/1 wait (the floor) |
|---|---|---|
| 0.50 | 1.0× | 0.5× |
| 0.80 | 4.0× | 2.0× |
| 0.90 | 9.0× | 4.5× |
| 0.95 | 19.0× | 9.5× |
| 0.99 | 99.0× | 49.5× |

Going from 80% to 95% utilisation buys 19% more throughput and costs **4.75× the
queueing delay**. That trade is the whole of capacity planning.

The inverse is more useful in a planning conversation, and `headroom_for` computes
it: `rho = r / (1 + r)`. Holding queueing to one service time means running at 50%.
Holding it to a tenth of one means running at 9%. This is the number that makes
"we will add capacity when we need it" expensive, and it does not depend on the
hardware.

`servedoctor queueing run.csv` prints the table against the run's own measured
service time, so the conversation happens in milliseconds rather than in ratios.

## The counterweight nobody mentions

The `1/(1-rho)` shape is a *single-server* result, and quoting it alone is
scaremongering. At the same utilisation, more servers wait less — `erlang_c_servers`
computes how many replicas hold a target wait, and the answer is usually much closer
to the raw offered load than the single-server table suggests.

Which means consolidation onto fewer, larger replicas is a **latency** decision and
not only a cost one, in the direction people usually do not expect: splitting one
big pool into several small ones raises the tail at unchanged utilisation.

## What this tool does not claim

An LLM server with continuous batching is **not an M/M/1 queue**. It admits many
requests at once, they share the accelerator, and the service rate of any one of
them depends on how many others are resident. The formulas above describe a single
server with an admission queue. They are a shape, not a model of the server, and
this tool uses them for exactly two things.

**One: the table.** Printed against the run's measured service time so a capacity
decision is made against numbers.

**Two: one consistency triangle.** Under Poisson arrivals at utilisation ρ, some
queueing is unavoidable — the M/D/1 value is a floor, because deterministic service
minimises `E[S²]` at fixed `E[S]` and therefore minimises the wait among all M/G/1
systems. A report that claims high utilisation, Poisson arrivals *and* queueing
below that floor is claiming three things that cannot all be true.

The tool does not say which one is false. It says which to check first:

On the synthetic run in `test_sd012_reports_and_can_warn` — Poisson arrivals at
6.0 req/s against a stated 6.3 req/s capacity, with a near-constant service time and
no real contention:

```text
queueing: [warn] arrivals look Poisson (CV 0.96), utilisation is 94% against the 6.3
  req/s capacity you gave, and the mean wait above the q5 service time is 22.2 ms --
  below the 7088.8 ms M/D/1 floor those two imply. One of the three is false, and this
  tool does not say which. Check the arrival process first: a harness that waits for
  each response before sending is not Poisson, whatever its --request-rate flag says.
```

22 ms against a 7-second floor. In that constructed case the false claim is the
capacity — the run was never really at 94% of anything — which is the point: the
triangle finds an inconsistency and hands it back, it does not diagnose.

Usually it is the arrival process, and usually the reason is that the harness was
closed-loop — which SD002 can check independently, from a completely different
statistic. A bound whose assumptions are testable against the same data, and then
tested, is the difference between a queueing model and a queueing anecdote.

## Utilisation is not λ·E[S], and the version that thought it was

The tool refuses to estimate utilisation without `--capacity`, and that refusal
replaced a bug worth describing.

`λ·E[S]` is the **offered concurrency**. On a server that batches it routinely
exceeds 1 with nothing wrong: `EXAMPLE_open_loop.csv` offers 5.01, which is the
correct reading of a deployment holding about five requests at once.

The first version computed `rho = min(0.999, lam * service)` and called it
utilisation. On that file it produced ρ = 100% and, from it, an M/D/1 floor of
**398 seconds** — on a run whose entire tail was 13 seconds. Not a number that is
slightly wrong. A number about a different system, printed with three significant
figures next to correct ones.

The `min(0.999, ...)` is the tell. A clamp is what you write when a formula is
producing values outside its domain, and a formula producing values outside its
domain is a formula being applied to the wrong quantity. The fix was not a better
estimator. It was to stop estimating and say so:

```text
queueing: [info] service time proxy 797.5 ms (q5 of end-to-end; 1900 requests are
  slower than it); lambda 6.28 req/s; offered concurrency lambda*E[S] = 5.01, so this
  deployment serves at least 5 requests at once; observed mean above the service proxy
  is 3544.1 ms. Utilisation is not computed: it needs the saturated throughput
  (--capacity), and lambda*E[S] is not it on a server that batches.
```

## The service-time proxy

Everything above needs `E[S]`, and a log of a loaded system does not contain it —
every latency in it is service plus queueing. The proxy used here is the **5th
percentile of end-to-end latency**: the requests that met the least queueing are the
fastest ones.

Not the minimum, because one lucky request is not an estimate. And labelled as a
proxy everywhere it appears, because it is one: on a run with no idle moments it is
biased upward, which makes the M/D/1 floor conservative — the direction that avoids
manufacturing a violation. The `FLOOR_SLACK = 0.80` in the triangle exists for the
same reason.
