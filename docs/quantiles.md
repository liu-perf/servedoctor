# 299, and the P99 that was deleted

Two arithmetic facts about tail statistics, both of which turn into refusals in this
tool. Neither is a judgement call, which is why both are pinned by tests to the
digit.

## 1. A P99 has no upper bound until n = 299

### The construction

For a sample of size n from any continuous distribution, the number of observations
at or below the true q-quantile is **Binomial(n, q)**. That is the whole model — no
normality, no variance estimate, no assumption about the shape of the latency
distribution, which is fortunate because latency distributions are not shaped like
anything.

So a confidence interval for the q-quantile is a pair of order statistics
`[x_(l), x_(u)]` chosen from that binomial's tails:

```
l = largest r with  P(B <= r-1) <= alpha/2
u = smallest r with P(B <= u-1) >= 1 - alpha/2        B ~ Binomial(n, q)
```

Coverage is then `cdf(u-1) - cdf(l-1) >= 1 - alpha` by construction. It is
conservative, because the binomial is discrete;
`test_coverage_is_conservative_not_exact` asserts that direction rather than
equality, since demanding equality would be asserting a bug.

### Where 299 comes from

At `r = n`, `P(B <= n-1) = 1 - q**n`. So the upper rank falls inside the sample
exactly when

```
1 - q**n >= 1 - alpha/2      i.e.      q**n <= alpha/2      i.e.      n >= ln(alpha/2)/ln(q)
```

| quantile | confidence | minimum n |
|---|---|---|
| q99 | 90% | **299** |
| q99 | 95% | 368 |
| q99 | 99% | 528 |
| q99.9 | 90% | 2995 |
| q95 | 90% | 59 |

Below that line the interval's upper end is off the top of the sample. The largest
observation is not an upper bound on the P99; it is the largest draw. Reporting the
P99 there is not "a bit noisy" — it is a statement with no upper bound of any kind.

`min_n_for` computes it in closed form and
`test_min_n_matches_the_closed_form_it_claims` verifies, for every (q, conf) pair it
uses, that the rank search and the algebra agree — including the boundary, that n-1
fails and n passes.

### The refusal, and what it offers instead

```console
$ servedoctor audit tests/fixtures/EXAMPLE_short_200.csv --rule SD004
q99: [violation] n=200 but a bounded 90% interval on q99 needs n>=299. Only 3
  observation(s) sit at or above the reported 3.0750s, and the upper confidence rank
  falls off the top of the sample -- the largest measurement is not an upper bound on
  this quantile, it is just the largest draw. This sample can carry q98.51 -- report
  that, or send 99 more requests.
```

`q98.51` is the inverse of the same formula: `q <= (alpha/2)**(1/n)`. Saying "you
have a q98.5, not a P99" is more useful than saying no, and it is exactly as
defensible.

### Nearest rank, not interpolation

`rank_for(n, q) = ceil(q*n)`. Linear interpolation invents a value between two
observations and prints it to three decimals, which reads as precision that came
from data. At n = 200 the P99 sits between the 198th and 199th observation; an
interpolated number is a weighted average of exactly two measurements.

One consequence worth knowing, because it decides how long a smoke test has to be:

```
ceil(0.99 * n) == n   for every n < 100
```

**A single cold start can only *be* the P99 of a run shorter than a hundred
requests** — which is exactly the length of run people do as a smoke test and then
quote. That is why `EXAMPLE_cold_start.csv` is 60 requests and not 500: at 500 the
same outlier moves the P99 by 0.8% and disappears. At 60 it *is* the P99, and
dropping it moves the number by **+252%**.

### There is no such thing as an average of two P99s

`percentile.merge_quantiles` exists only to raise. The quantile of a mixture depends
on the whole shape of both components, not on one point from each, and no weighting
repairs it. `test_the_average_of_two_p99s_is_not_the_p99_of_the_union` is a worked
counter-example rather than an assertion: half-slow and all-fast, 100 requests each,
P99s of 100.0 and 1.0, union P99 of 100.0, average of 50.5.

Three routine places this happens: averaging per-shard percentiles, rolling
per-minute percentiles into an hour, and averaging percentiles over repeated runs
"to reduce noise". The last is the worst, because it moves the number toward the
middle and calls the movement stability.

## 2. Censoring deletes the quantile it looks like it perturbs

Requests that failed, timed out, or were still running when the harness stopped are
dropped before the statistics are computed. Every serving benchmark does this and
almost none of them say so.

They are not a random subset. **An unfinished request is, by definition, slower than
every finished one.** That single assumption — which is not a modelling choice, it
is what "did not finish" means — is enough for the rest:

If a fraction `f` is censored from the top, then the population's q-quantile is the
survivors' `q/(1-f)` quantile. And when `q > 1-f`, that is greater than 1: the
quantile asked for lies entirely inside the discarded part, and **is not recoverable
from the file at all**.

```
2% timeouts, want P99   ->  0.99 > 0.98  ->  gone
2% timeouts, want P95   ->  0.95 < 0.98  ->  the survivors' q96.94
5% failures, want P95   ->  0.95 = 0.95  ->  exactly on the boundary
```

So a 2% timeout rate does not add error bars to the P99. It removes the P99 and
leaves a number in its place with the same name.

```console
$ servedoctor audit tests/fixtures/EXAMPLE_censored.csv --rule SD007
censoring: [violation] 12 of 600 requests (2.00%) did not finish and were dropped.
  Since an unfinished request is slower than every finished one, the top 2.00% of the
  true distribution is exactly the part that was discarded -- and q99 lies inside it.
  The observed q99 of 11.3064s is not an estimate of the true one; it is the q99 of
  the survivors.
```

Ask for q95 on the same file and the tool applies the remap instead of refusing,
because the arithmetic is available there. The refusal is not a policy; it is where
the arithmetic runs out.

## Why this is written out instead of imported

Three lines of scipy would do it. They are not here for one reason: the interval is
a deterministic function of n and q, so a reader who wants to check a number in this
README needs no environment, only the file. The same property is what lets
`test_percentile.py` pin 299, 368, 2995 and 59 as literals — a threshold that came
out of a solver is a threshold nobody can argue with, and a threshold nobody can
reproduce is one nobody should believe.
