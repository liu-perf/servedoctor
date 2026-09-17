"""Measure coordinated omission on this machine, with a real clock and real threads.

    python examples/coordinated_omission.py

Takes about 45 seconds and needs nothing but the standard library -- no GPU, no
model, no network. That is the point: the effect this whole repository is about is
a property of the *load generator*, not of the system under test, so it can be
demonstrated against a mock server whose behaviour is fully known. If it needed a
deployment to show up, nobody could check it.

CI does not run this. It is wall-clock sensitive, and a test that fails when the
machine is busy teaches people to ignore failures.

The mock server:

  * serves SLOTS requests at once, each taking SERVICE_S of work;
  * freezes completely for STALL_LEN_S every STALL_EVERY_S. Pick your cause --
    a checkpoint write, an allocator compaction, a long prefill blocking the decode
    batch, a noisy neighbour. The shape is what matters.

Two harnesses are pointed at it, both configured for the same RATE:

  * **closed loop**: WORKERS threads, each sending again when its answer arrives.
    WORKERS is derived from RATE the way an operator derives it -- "a request takes
    about SERVICE_S, so RATE * SERVICE_S in parallel gives me RATE".
  * **open loop**: a dispatcher that sends at RATE regardless of what the server
    is doing, recording both the time each request was *due* and the time it went
    out.

The prediction being tested is not "the closed loop is slower or faster". It is:

    the closed loop will report a materially shorter tail for the same server,
    because it only ever has WORKERS requests exposed to a stall, while the open
    loop has RATE * STALL_LEN_S of them.

If your machine does not reproduce it, the script says so instead of insisting.
**Keep that output.** A disagreement is information; a script that always prints
the conclusion it was written to print is not a measurement.
"""
import math
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from servedoctor import closure as _closure  # noqa: E402
from servedoctor import parse as _parse  # noqa: E402
from servedoctor import percentile as _p  # noqa: E402

# ------------------------------------------------------------------ parameters
SLOTS = 8
SERVICE_S = 0.080
RATE = 80.0
N_REQ = 1500
STALL_FIRST_S = 8.0
STALL_EVERY_S = 10.0
STALL_LEN_S = 1.2
# Below this ratio between the two tails the claim is not reproduced on this
# machine and the script says so.
CLAIM_MIN_RATIO = 1.5


class MockServer:
    """SLOTS-way concurrent service that freezes on a schedule."""

    def __init__(self):
        self._slots = threading.Semaphore(SLOTS)
        self._stall_until = 0.0
        self._stop = threading.Event()
        self._t0 = time.perf_counter()
        self._stall_thread = threading.Thread(target=self._stall_loop, daemon=True)

    def start(self):
        self._t0 = time.perf_counter()
        self._stall_thread.start()

    def stop(self):
        self._stop.set()

    def _stall_loop(self):
        nxt = self._t0 + STALL_FIRST_S
        while not self._stop.is_set():
            now = time.perf_counter()
            if now >= nxt:
                self._stall_until = now + STALL_LEN_S
                nxt = now + STALL_EVERY_S
            time.sleep(0.005)

    def serve(self):
        """Block for SERVICE_S of *progress*, which a stall does not provide."""
        with self._slots:
            remaining = SERVICE_S
            while remaining > 0.0:
                now = time.perf_counter()
                if now < self._stall_until:
                    time.sleep(min(0.01, self._stall_until - now))
                    continue
                step = min(remaining, 0.010)
                time.sleep(step)
                remaining -= step


def run_closed(server, workers, n):
    """Each worker sends again the moment its previous answer arrives."""
    rows = []
    lock = threading.Lock()
    counter = {"i": 0}

    def work():
        while True:
            with lock:
                if counter["i"] >= n:
                    return
                counter["i"] += 1
            t0 = time.perf_counter()
            server.serve()
            t1 = time.perf_counter()
            with lock:
                rows.append((t0, t0, t1))          # due == sent, by construction
    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return rows


def run_open(server, rate, n, seed=20260814):
    """Sends on a schedule that ignores the server, and records both times."""
    rows = []
    lock = threading.Lock()
    x = seed & 0x7FFFFFFF
    gaps = []
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        u = (x % 100000 + 1) / 100001.0
        gaps.append(-math.log(u) / rate)

    def one(due):
        t0 = time.perf_counter()
        server.serve()
        t1 = time.perf_counter()
        with lock:
            rows.append((due, t0, t1))

    base = time.perf_counter()
    due = base
    threads = []
    for g in gaps:
        due += g
        now = time.perf_counter()
        if due > now:
            time.sleep(due - now)
        t = threading.Thread(target=one, args=(due,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return rows


def to_run(rows):
    """Hand the measurement to this library rather than re-implementing it here."""
    reqs = []
    for i, (due, sent, done) in enumerate(sorted(rows, key=lambda r: r[1])):
        reqs.append(_parse.Request(rid=str(i), arrival_s=due, start_s=sent,
                                   first_token_s=None, end_s=done,
                                   input_tokens=None, output_tokens=None))
    return _parse.Run(requests=reqs, source="in_process")


def describe(label, run):
    lats = run.values(lambda r: r.latency_s())
    span = run.span_s()
    e = _p.estimate(lats, 0.99, 0.90)
    v = _closure.classify(run)
    print("  {:<12} n={:<5d} rate={:6.2f} req/s  q50={:6.3f}s  q99={:6.3f}s  "
          "max={:6.3f}s".format(label, len(lats), len(lats) / span,
                                _p.median(lats), e.value, max(lats)))
    print("  {:<12} q99 90% CI {}  |  detector says: {}".format(
        "", "[{:.3f}, {:.3f}]".format(e.lo, e.hi) if e.bounded else "UNBOUNDED",
        v.kind))
    return e, max(lats), len(lats) / span


def main():
    workers = max(1, int(round(RATE * SERVICE_S)))
    print("mock server: {} slots, {:.0f} ms service, freezes {:.1f}s every {:.0f}s"
          .format(SLOTS, SERVICE_S * 1e3, STALL_LEN_S, STALL_EVERY_S))
    print("both harnesses configured for {:.0f} req/s; the closed loop gets {} "
          "workers ({:.0f} x {:.3f}s)".format(RATE, workers, RATE, SERVICE_S))
    print()

    server = MockServer()
    server.start()
    closed_rows = run_closed(server, workers, N_REQ)
    open_rows = run_open(server, RATE, N_REQ)
    server.stop()

    closed = to_run(closed_rows)
    opened = to_run(open_rows)
    print("results")
    c_e, c_max, c_rate = describe("closed loop", closed)
    o_e, o_max, o_rate = describe("open loop", opened)
    print()

    print("rate actually delivered: closed {:.2f} req/s = {:.0%} of the {:.0f} it "
          "was configured for; open {:.2f} req/s = {:.0%}"
          .format(c_rate, c_rate / RATE, RATE, o_rate, o_rate / RATE))
    if c_e.value and o_e.value:
        ratio = o_e.value / c_e.value
        print("q99 ratio open/closed: {:.2f}x     max ratio: {:.2f}x"
              .format(ratio, o_max / c_max))
        print()
        if ratio >= CLAIM_MIN_RATIO:
            print("verdict: [reproduced] the same server, offered the same nominal "
                  "load, reports a q99 {:.2f}x shorter to the closed-loop harness "
                  "while the two maxima differ by only {:.2f}x. The closed loop did "
                  "see the stall -- it is right there in its worst case -- it just "
                  "never had enough requests exposed to it for the stall to reach a "
                  "percentile.".format(ratio, o_max / c_max))
            if not c_e.bounded or (c_e.hi and c_e.lo and c_e.hi / c_e.lo > 3.0):
                print("         and note the closed run cannot bound its own q99: "
                      "the 90% interval spans {:.1f}x, because the distribution is "
                      "two spikes with nothing in between. A single number quoted "
                      "off it means nothing whichever spike it landed on."
                      .format((c_e.hi / c_e.lo) if c_e.bounded else float("inf")))
        else:
            print("verdict: [not reproduced] the two tails came out {:.2f}x apart, "
                  "under the {:.1f}x this script calls a result. Keep this output "
                  "and look at why rather than re-running until it agrees. The usual "
                  "cause is that the stalls landed such that "
                  "workers*stalls/n was not below 1% -- print that ratio before "
                  "concluding anything about the harness."
                  .format(ratio, CLAIM_MIN_RATIO))
    print()
    n_stalls = max(0, int((len(closed.requests) / max(c_rate, 1e-9)
                           - STALL_FIRST_S) // STALL_EVERY_S) + 1)
    print("bookkeeping: about {} stall(s) in the closed run; {} workers exposed to "
          "each, so {:.2%} of its {} requests -- the P99 sits at the top 1%, which "
          "is why it misses them. The open run exposed about {:.0f} per stall, "
          "{:.1%}.".format(n_stalls, workers, workers * n_stalls / len(closed.requests),
                           len(closed.requests), RATE * STALL_LEN_S,
                           RATE * STALL_LEN_S * n_stalls / len(opened.requests)))
    print("stdev of the closed-loop latencies: {:.4f}s; of the open-loop: {:.4f}s"
          .format(statistics.pstdev(closed.values(lambda r: r.latency_s())),
                  statistics.pstdev(opened.values(lambda r: r.latency_s()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
