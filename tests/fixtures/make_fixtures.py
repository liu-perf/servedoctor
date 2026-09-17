"""Generate every fixture in this directory, deterministically, from one model.

    python tests/fixtures/make_fixtures.py

CI re-runs this and then `git diff --exit-code`, so a fixture that has drifted from
its generator fails the build. A generated file nobody can regenerate is a file
nobody can check, and the whole subject of this repository is numbers nobody
checked.

**One server, two harnesses, one configured load.** The pair
`EXAMPLE_closed_loop.csv` / `EXAMPLE_open_loop.csv` comes out of the *same*
simulated server, with the same stalls at the same instants, and both harnesses are
configured for the same `TARGET_RATE`. The open-loop one sends at that rate. The
closed-loop one is given the worker count an operator would derive from it -- and
delivers less, because a closed loop's rate is an output of the server's speed, not
an input to it. So every difference between the two files is attributable to the
harness and to nothing else, which is a property a real pair of runs can never have
and is the whole reason to build a fixture.

**Why the stalls are rare and long rather than frequent and short.** The closed loop
exposes exactly `CLOSED_WORKERS` requests to each stall; the open loop exposes
`rate * STALL_LEN_S` of them. Whether that difference reaches the P99 depends on
whether `CLOSED_WORKERS * stalls / n` falls below 1% while `rate * STALL_LEN_S *
stalls / n` sits above it. With these constants the first is about 0.7% and the
second about 6%, so the closed run's P99 misses the stall entirely and the open
run's is made of it: 2.859s against 13.482s, a factor of 4.72.

Note what barely differs: the two **maxima**, 12.502s and 14.695s, 18% apart. The
closed loop does record the stall -- it is right there in the worst case. It simply
never has enough requests in flight for the stall to reach a percentile. That is a
more precise statement of coordinated omission than "the tail is truncated", and it
has a practical consequence: on a closed-loop run the gap between the maximum and
the P99 is itself the warning sign.

The server model is deliberately crude and stated rather than hidden:

  * `SLOTS` requests are served concurrently; the rest queue FIFO;
  * a request's work is `PREFILL_S_PER_TOKEN * input + DECODE_S_PER_TOKEN * output`,
    the first part ending at the first token;
  * every `STALL_PERIOD_S` the whole server freezes for `STALL_LEN_S`. Real causes
    include a CUDA context sync, an allocator compaction, a preemption/recompute
    cycle, or a neighbour on the same host. The cause does not matter here; the
    shape does.

It is not a model of continuous batching and does not claim to be. Everything this
library asserts about the *harness* holds regardless of what the server does, and
the fixtures exist to exercise the harness-side arithmetic.

**Randomness.** A 31-bit LCG written out in full, because "deterministic" has to
survive a Python version change and `random` does not promise that across releases.

**Realised values do not exactly match targets, and are not adjusted to.** The
declared rate in each file's header is what the generator was asked for; the rate
in the data is what a finite draw produced. Sanding that off would remove the very
thing SD004 is about.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- server model
SLOTS = 8                        # concurrent requests in service
PREFILL_S_PER_TOKEN = 2.0e-5     # 512-token prompt -> 10.2 ms
DECODE_S_PER_TOKEN = 8.0e-3      # 128-token answer -> 1.02 s
# A long, infrequent freeze rather than a short frequent one, because that is the
# shape that separates the two harnesses. Real causes: a checkpoint write, an
# allocator compaction, a large prefill occupying the model while decodes wait, a
# neighbour on the same host. See the note on CLOSED_WORKERS below for why the
# spacing matters more than the depth.
STALL_PERIOD_S = 150.0
STALL_LEN_S = 10.0
STALL_FIRST_S = 45.0

# The load both harnesses are configured for. The closed-loop run does not get to
# choose it -- its worker count is derived from it below, which is what an operator
# does when they reason "one request takes about a second, so seven in parallel is
# about seven per second". The whole point of the pair is that only one of the two
# harnesses actually delivers this.
TARGET_RATE = 6.4
NOMINAL_SERVICE_S = 512 * PREFILL_S_PER_TOKEN + 128 * DECODE_S_PER_TOKEN
CLOSED_WORKERS = max(1, int(round(TARGET_RATE * NOMINAL_SERVICE_S)))
N_PAIR = 2000


class LCG:
    """The one in Numerical Recipes. Written out so the fixtures outlive a release."""

    def __init__(self, seed):
        self.x = seed & 0x7FFFFFFF

    def next(self):
        self.x = (1103515245 * self.x + 12345) & 0x7FFFFFFF
        return self.x

    def u(self):
        return self.next() / 2147483648.0

    def expo(self, mean):
        u = self.u()
        if u <= 0.0:
            u = 1e-12
        return -mean * _ln(u)

    def lognormal_int(self, median, sigma, lo, hi):
        """Token counts are long-tailed; a normal draw of the log is the cheap way."""
        z = 0.0
        for _ in range(6):
            z += self.u()
        z = (z - 3.0) * 1.4142135623730951      # crude but stable normal
        v = median * _exp(sigma * z)
        return int(max(lo, min(hi, round(v))))


def _ln(x):
    import math
    return math.log(x)


def _exp(x):
    import math
    return math.exp(x)


def advance(t, work):
    """Wall-clock time after doing `work` seconds of service, stalls included."""
    if work <= 0:
        return t
    remaining = work
    while remaining > 1e-12:
        k = int((t - STALL_FIRST_S) // STALL_PERIOD_S)
        if k >= 0:
            s0 = STALL_FIRST_S + k * STALL_PERIOD_S
            if s0 <= t < s0 + STALL_LEN_S:
                t = s0 + STALL_LEN_S
                continue
        nxt = STALL_FIRST_S
        while nxt <= t:
            nxt += STALL_PERIOD_S
        avail = nxt - t
        if remaining <= avail:
            t += remaining
            remaining = 0.0
        else:
            remaining -= avail
            t = nxt
    return t


class Server:
    """SLOTS-way concurrent service with FIFO admission."""

    def __init__(self, slots=SLOTS):
        self.free = [0.0] * slots

    def serve(self, send_s, prefill_work, decode_work):
        i = min(range(len(self.free)), key=lambda j: self.free[j])
        begin = max(send_s, self.free[i])
        first = advance(begin, prefill_work)
        end = advance(first, decode_work)
        self.free[i] = end
        return begin, first, end


def draw_tokens(rng, prompt_median=512, answer_median=128):
    return (rng.lognormal_int(prompt_median, 0.45, 16, 8192),
            rng.lognormal_int(answer_median, 0.40, 1, 2048))


def work_for(tin, tout):
    return tin * PREFILL_S_PER_TOKEN, tout * DECODE_S_PER_TOKEN


# ------------------------------------------------------------------- harnesses
def run_closed(n, workers, seed, prompt_median=512, answer_median=128):
    rng = LCG(seed)
    server = Server()
    free_at = [0.0] * workers
    rows = []
    for i in range(n):
        w = min(range(workers), key=lambda j: free_at[j])
        send = free_at[w]
        tin, tout = draw_tokens(rng, prompt_median, answer_median)
        pw, dw = work_for(tin, tout)
        _b, first, end = server.serve(send, pw, dw)
        free_at[w] = end
        rows.append(dict(rid=i, arrival=None, start=send, first=first, end=end,
                         tin=tin, tout=tout, ok=1))
    rows.sort(key=lambda r: r["start"])
    return rows


def run_open(n, rate, seed, client_cap=None, prompt_median=512, answer_median=128):
    rng = LCG(seed)
    server = Server()
    rows = []
    t = 0.0
    arrivals = []
    for _ in range(n):
        t += rng.expo(1.0 / rate)
        arrivals.append(t)
    inflight = [] if client_cap else None
    for i, due in enumerate(arrivals):
        send = due
        if client_cap:
            if len(inflight) >= client_cap:
                inflight.sort()
                send = max(due, inflight.pop(0))
        tin, tout = draw_tokens(rng, prompt_median, answer_median)
        pw, dw = work_for(tin, tout)
        _b, first, end = server.serve(send, pw, dw)
        if client_cap:
            inflight.append(end)
        rows.append(dict(rid=i, arrival=due, start=send, first=first, end=end,
                         tin=tin, tout=tout, ok=1))
    return rows


# ---------------------------------------------------------------------- output
HEADER = "request_id,arrival_s,start_s,first_token_s,end_s,input_tokens,output_tokens,ok"
HEADER_NO_ARRIVAL = "request_id,start_s,first_token_s,end_s,input_tokens,output_tokens,ok"


def write_csv(name, rows, declaration, with_arrival=True):
    path = os.path.join(HERE, name)
    lines = []
    for d in declaration:
        lines.append("# " + d)
    lines.append(HEADER if with_arrival else HEADER_NO_ARRIVAL)
    for r in rows:
        end = "" if r["end"] is None else "{:.6f}".format(r["end"])
        first = "" if r["first"] is None else "{:.6f}".format(r["first"])
        if with_arrival:
            lines.append("{},{:.6f},{:.6f},{},{},{},{},{}".format(
                r["rid"], r["arrival"], r["start"], first, end,
                r["tin"], r["tout"], r["ok"]))
        else:
            lines.append("{},{:.6f},{},{},{},{},{}".format(
                r["rid"], r["start"], first, end, r["tin"], r["tout"], r["ok"]))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def throughput(rows):
    starts = [r["start"] for r in rows]
    ends = [r["end"] for r in rows if r["end"] is not None]
    span = max(ends) - min(starts)
    return len(rows) / span, span


def q(vals, qq):
    import math
    s = sorted(vals)
    return s[max(1, min(len(s), int(math.ceil(qq * len(s))))) - 1]


DECL = ("EXAMPLE -- synthetic, generated by tests/fixtures/make_fixtures.py.",
        "Not a capture of any real deployment. The server is a {}-slot simulator "
        "that freezes for {:g}s every {:g}s.".format(SLOTS, STALL_LEN_S,
                                                     STALL_PERIOD_S))


def main():
    made = []

    # -- the pair. Same server, same stalls, same average rate. ---------------
    closed = run_closed(N_PAIR, CLOSED_WORKERS, seed=20260814)
    rate, span = throughput(closed)
    made.append(write_csv(
        "EXAMPLE_closed_loop.csv", closed,
        DECL + ("Configured for {:g} req/s the way an operator configures a closed "
                "loop: {} workers, because one request takes about {:.2f}s. Realised "
                "{:.3f} req/s over {:.1f}s -- {:.0%} of what it was asked for, and "
                "nothing in its own output says so.".format(
                    TARGET_RATE, CLOSED_WORKERS, NOMINAL_SERVICE_S, rate, span,
                    rate / TARGET_RATE),
                "No arrival_s column, because a closed loop has no schedule to "
                "record: the send times ARE the completion times, shifted."),
        with_arrival=False))

    opened = run_open(N_PAIR, TARGET_RATE, seed=20260815)
    orate, ospan = throughput(opened)
    made.append(write_csv(
        "EXAMPLE_open_loop.csv", opened,
        DECL + ("Poisson arrivals at the same configured {:g} req/s as "
                "EXAMPLE_closed_loop.csv, against the same server with the same "
                "stalls at the same instants. Realised {:.3f} req/s over {:.1f}s. "
                "The only difference between the two files is the harness."
                .format(TARGET_RATE, orate, ospan),
                "arrival_s is the schedule; start_s is when the request actually "
                "went out. They agree while the harness keeps up.")))

    # -- an open loop whose client cannot keep up ------------------------------
    over = run_open(900, TARGET_RATE * 1.35, seed=20260816, client_cap=24)
    made.append(write_csv(
        "EXAMPLE_open_loop_backlog.csv", over,
        DECL + ("Poisson arrivals at {:.3f} req/s with the client limited to 24 "
                "outstanding requests. The schedule is kept until the server slows, "
                "after which start_s runs behind arrival_s and the gap is the "
                "harness's own queue.".format(TARGET_RATE * 1.35),)))

    # -- too few requests to carry a P99 --------------------------------------
    short = run_open(200, TARGET_RATE, seed=20260817)
    made.append(write_csv(
        "EXAMPLE_short_200.csv", short,
        DECL + ("Exactly 200 requests. Enough to print a P99 to three decimals, "
                "not enough to bound one: 299 are needed at 90% confidence.",)))

    # -- 2% never finished ----------------------------------------------------
    censored = run_open(600, TARGET_RATE, seed=20260818)
    lat = [(r["end"] - r["start"], i) for i, r in enumerate(censored)]
    lat.sort(reverse=True)
    for _d, i in lat[:12]:
        censored[i]["end"] = None
        censored[i]["first"] = None
        censored[i]["ok"] = 0
    made.append(write_csv(
        "EXAMPLE_censored.csv", censored,
        DECL + ("12 of 600 requests (2.00%) timed out and have no completion time. "
                "They are the 12 slowest, which is what 'timed out' means -- so the "
                "top 2% of the true distribution is exactly the part that is gone, "
                "and the P99 lies inside it.",)))

    # -- short answers: the TPOT denominator matters --------------------------
    shortans = run_open(600, TARGET_RATE * 0.5, seed=20260819,
                        prompt_median=1024, answer_median=4)
    made.append(write_csv(
        "EXAMPLE_short_answers.csv", shortans,
        DECL + ("Median answer is about 4 tokens and the median prompt about 1024: "
                "classification / routing / extraction traffic. Dividing decode time "
                "by m instead of m-1 is a first-order error here, and the prompt is "
                "most of the tokens, so a total-tokens/s headline says almost nothing "
                "about decode.",)))

    # -- a cold start left in ------------------------------------------------
    # 60 requests, not 500, and the reason is arithmetic: the nearest-rank P99 of n
    # samples is order statistic ceil(0.99n), which equals n only for n < 100. A
    # single cold start can therefore only *be* the P99 of a run shorter than a
    # hundred requests -- which is exactly the length of run people do as a smoke
    # test and then quote. At 500 requests the same outlier moves the P99 by 0.8%
    # and hides. The rate is low enough that the span ends before the first stall,
    # so the cold start is the only outlier in the file.
    warm = run_open(60, TARGET_RATE * 0.6, seed=20260820)
    shift = 9.0
    warm[0]["first"] += shift
    warm[0]["end"] += shift
    made.append(write_csv(
        "EXAMPLE_cold_start.csv", warm,
        DECL + ("The first request carries {:g}s of extra time -- weight load, lazy "
                "context creation, an empty prefix cache. 60 requests, so that one "
                "sample IS the P99: ceil(0.99*60) = 60.".format(shift),)))

    # -- the rate ladder for `sweep` ------------------------------------------
    for i, mult in enumerate((0.40, 0.70, 0.95, 1.15)):
        rows = run_open(500, TARGET_RATE * mult, seed=20260830 + i)
        made.append(write_csv(
            "EXAMPLE_ladder_{}.csv".format(int(mult * 100)), rows,
            DECL + ("Rung {} of a 4-rung rate ladder: Poisson arrivals at {:.0f}% of "
                    "the paired run's configured rate ({:.3f} req/s).".format(
                        i + 1, mult * 100, TARGET_RATE * mult),)))

    # -- a summary report, which is a different kind of object ----------------
    summary_path = os.path.join(HERE, "EXAMPLE_summary.json")
    lats = [r["end"] - r["start"] for r in opened]
    ttfts = [r["first"] - r["start"] for r in opened]
    tpots = [(r["end"] - r["first"]) / (r["tout"] - 1)
             for r in opened if r["tout"] >= 2]
    body = {
        "_comment": ("EXAMPLE -- synthetic. The same run as EXAMPLE_open_loop.csv, "
                     "reduced to the shape a serving benchmark prints at the end. "
                     "Every distributional question in it has already been answered "
                     "and cannot be re-asked: that is the point of the file. "
                     "max_concurrency is deliberately the closed-loop run's 7, the "
                     "way a number gets carried over when two configurations are "
                     "edited in the same file -- Little's Law is what catches it, "
                     "because 6.278 req/s x 4.34 s is 27 requests in flight, not 7."),
        "completed": len(opened),
        "duration": ospan,
        "request_throughput": orate,
        "output_throughput": sum(r["tout"] for r in opened) / ospan,
        "total_token_throughput": sum(r["tin"] + r["tout"] for r in opened) / ospan,
        "total_input_tokens": sum(r["tin"] for r in opened),
        "total_output_tokens": sum(r["tout"] for r in opened),
        "mean_ttft_ms": 1e3 * sum(ttfts) / len(ttfts),
        "median_ttft_ms": 1e3 * q(ttfts, 0.5),
        "p99_ttft_ms": 1e3 * q(ttfts, 0.99),
        "mean_tpot_ms": 1e3 * sum(tpots) / len(tpots),
        "p99_tpot_ms": 1e3 * q(tpots, 0.99),
        "mean_e2el_ms": 1e3 * sum(lats) / len(lats),
        "p99_e2el_ms": 1e3 * q(lats, 0.99),
        "max_concurrency": CLOSED_WORKERS,
        "request_rate": orate,
    }
    with open(summary_path, "w", encoding="utf-8", newline="\n") as fh:
        import json
        fh.write(json.dumps(body, indent=2, sort_keys=True) + "\n")
    made.append(summary_path)

    for p in made:
        print(os.path.basename(p))
    print()
    print("closed loop : {:.3f} req/s, q99 {:.3f}s, max {:.3f}s".format(
        rate, q([r["end"] - r["start"] for r in closed], 0.99),
        max(r["end"] - r["start"] for r in closed)))
    print("open loop   : {:.3f} req/s, q99 {:.3f}s, max {:.3f}s".format(
        orate, q([r["end"] - r["start"] for r in opened], 0.99),
        max(r["end"] - r["start"] for r in opened)))
    print("ratio of q99 (open / closed): {:.2f}x".format(
        q([r["end"] - r["start"] for r in opened], 0.99)
        / q([r["end"] - r["start"] for r in closed], 0.99)))


if __name__ == "__main__":
    main()
