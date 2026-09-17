# The same server, two harnesses, a tail 16 times apart

Everything in this file was measured. The script is
`examples/coordinated_omission.py`, it needs nothing but a Python interpreter, and
it takes about 45 seconds. If your machine gives different numbers, keep them — the
disagreement is information, and the script is written so that it can say the claim
was not reproduced.

## What is being tested

A load generator can offer work in one of two ways.

**Closed loop.** N workers; each sends a request, waits for the answer, sends the
next. This is what a thread pool does, what `ab -c 64` does, and what most
hand-rolled harnesses do because it is the shape `for` loops have.

**Open loop.** A schedule that ignores the server. Requests go out at their due
time whether or not the previous ones came back.

The difference is not stylistic. Under a closed loop, **the offered rate is an
output of the server's speed**. When the server slows down, the harness slows down
with it, and the requests that would have piled up during a stall are never sent.
Gil Tene named the consequence coordinated omission.

The usual statement of it is "the tail is truncated". That is true and too vague to
act on. The sharper version, which this experiment measures, is:

> A closed loop exposes exactly N requests to any stall. An open loop at rate λ
> exposes λ·D of them. Whether that difference reaches a given percentile depends
> on whether `N·stalls/n` falls below `1-q` while `λ·D·stalls/n` sits above it.

## The setup

A mock server, pure standard library:

* 8 slots served concurrently, 80 ms of work each;
* the whole server freezes for 1.2 s every 10 s. Pick your cause — a checkpoint
  write, an allocator compaction, a long prefill occupying the model while decodes
  wait, a neighbour on the same host. The shape is what matters, not the cause.

Two harnesses, **both configured for 80 req/s**. The open one sends at 80 req/s.
The closed one gets 6 workers, which is the number an operator derives: a request
takes about 80 ms, so 80 × 0.080 = 6.4 in parallel gives 80 per second.

1500 requests each.

## The result

Three consecutive runs on one machine:

```text
mock server: 8 slots, 80 ms service, freezes 1.2s every 10s
both harnesses configured for 80 req/s; the closed loop gets 6 workers (80 x 0.080s)

results
  closed loop  n=1500  rate= 65.16 req/s  q50= 0.082s  q99= 0.084s  max= 1.279s
               q99 90% CI [0.084, 1.276]  |  detector says: closed_loop
  open loop    n=1500  rate= 77.51 req/s  q50= 0.662s  q99= 1.392s  max= 2.374s
               q99 90% CI [1.376, 1.412]  |  detector says: open_loop

rate actually delivered: closed 65.16 req/s = 81% of the 80 it was configured for;
                         open 77.51 req/s = 97%
q99 ratio open/closed: 16.52x     max ratio: 1.86x
```

| run | q99 ratio | max ratio | closed delivered |
|---|---|---|---|
| 1 | 16.52× | 1.86× | 81% |
| 2 | 16.63× | 1.69× | 81% |
| 3 | 16.53× | 1.76× | 81% |
| 4 | 16.48× | 1.88× | 82% |

## Three things in that table

### The closed loop did see the stall

Its maximum is 1.279 s, and the stall is 1.2 s. The event is in the data. It is not
in the percentile, and the bookkeeping line the script prints says exactly why:

```text
bookkeeping: about 2 stall(s) in the closed run; 6 workers exposed to each, so 0.80%
of its 1500 requests -- the P99 sits at the top 1%, which is why it misses them. The
open run exposed about 96 per stall, 12.8%.
```

0.80% is below 1%. That is the whole mechanism. Not "the harness hid the stall" —
the harness recorded it, once per worker, and 12 samples out of 1500 do not reach
the 99th percentile.

**Consequence you can use on somebody else's report:** on a closed-loop run, a
large gap between the maximum and the P99 is the warning sign. Here it is 15×.

### The closed loop never ran the test it was configured for

81% of 80 req/s. Nothing in its own output says so — a closed-loop harness has no
notion of a rate it failed to achieve, because it never had a rate, only a worker
count. The operator asked "what is the P99 at 80 req/s"; the answer they got is the
P99 at 65 req/s, labelled 80.

This is worth more than the tail number in an argument, because it does not depend
on anybody agreeing about percentiles.

### The closed run cannot bound its own P99

```text
q99 90% CI [0.084, 1.276]
```

A factor of 15 between the ends. Not because the sample is small — 1500 requests is
comfortably above the 299 the interval needs — but because the distribution is two
spikes with nothing between them: 99.2% of requests at 82 ms, 0.8% at 1.28 s. The
99th percentile sits on the boundary, and which spike it lands on is a coin flip.

The open run's interval, on the same number of requests, is [1.376, 1.412] — 2.6%
wide. Same sample size, same statistic, and one of them is a usable number.

## The same thing without a clock

`tests/fixtures/EXAMPLE_closed_loop.csv` and `EXAMPLE_open_loop.csv` are generated
by a deterministic discrete-event simulation of the same idea: same server, same
stalls at the same instants, both harnesses configured for 6.4 req/s.

| | delivered | q99 | max |
|---|---|---|---|
| closed loop, 7 workers | 5.841 req/s (91%) | **2.859 s** | 12.502 s |
| open loop, 6.4 req/s | 6.278 req/s (98%) | **13.482 s** | 14.695 s |

4.72× on the tail, 1.18× on the maximum. Smaller than the wall-clock experiment
because the stall is a smaller multiple of the service time; the mechanism and the
sign are identical, and it runs in CI where the timing experiment cannot.

## How the tool decides, and the version that was wrong

Two detectors, kept independent on purpose.

**A — occupancy.** Sweep the start/end events and measure the fraction of wall time
spent at the maximum number in flight. A closed loop is pinned at N; the fixture
sits at 7 for 99.5% of the run.

**B — coincidence.** Every send in a closed loop happens at the instant some earlier
request completed, so the distance from each send back to the nearest preceding
completion is zero. Normalise by the median inter-send gap and the statistic is
scale-free: closed → 0, open → about 1, because completions occur at the same
average rate as sends, so a send lands about half an inter-completion gap after one.

On the fixtures: closed 0.000, open 1.092. The predicted open value is 1.0.

**The first version of B was wrong**, and it is worth saying how. It computed
`start[i] - end[i-N]` scanned over candidate N — which looks like the same idea and
is not. With variable service times the i-th send is not the same worker's
(i-N)-th, the index lag decorrelates, and on a file that is a closed loop *by
construction* it scored 0.211 — inside the inconclusive band. The tool returned
"I cannot tell" about a file whose generator has `run_closed` in the name.

`test_the_index_lag_form_would_have_failed_on_the_closed_fixture` keeps both
computations side by side so the old one cannot come back.

Detector A never looks at which request is which, only at how many are open;
detector B never looks at how many are open, only at the ordering of two event
streams. `test_the_two_detectors_can_disagree` holds a run where they do — a closed
loop with client-side think time is capped but not coincident — and the tool
returns `inconclusive` rather than picking one.

## What this does not settle

* **A rate-limited harness that hit a concurrency cap really is a closed loop** past
  that point, and the tool says so about `EXAMPLE_open_loop_backlog.csv`. That is
  not a false positive; it is the correct reading. What distinguishes it is the
  recorded schedule, which lets SD003 give the exact omission factor — 2.07× on
  that file — instead of an argument.
* **A perfectly periodic schedule against constant service is indistinguishable**
  from a closed loop. No statistic separates them, because there is nothing to
  separate.
* **This says nothing about whether the server is good.** It says whether the
  measurement is about the server.

## What to do about it

1. **Record the intended send time.** One extra column turns coordinated omission
   from a debate into a subtraction. Every conclusion in SD003 comes from
   `end - arrival` versus `end - start`.
2. **If you must run closed-loop, report throughput and delete the percentiles.**
   The throughput number is real. The tail is about your thread pool.
3. **Check the max/P99 gap** on any tail number you are handed. It costs nothing and
   it is the one diagnostic that works on a report you did not produce.
