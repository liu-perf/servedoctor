"""The first version of the summary, kept in the source and re-run by CI forever.

Same precedent as regressiondoctor's `diff_naive` and fitdoctor's `budget_naive`:
the version that was written before the data pushed back stays in the repository,
and `tests/test_naive.py` asserts that it is **still wrong**. Deleting it would
delete the evidence that the design was hit rather than imagined.

Four lines, and every one of them is what a competent person writes on the first
afternoon:

    mean latency                  because it is the obvious summary
    total tokens / elapsed        because both numbers are right there
    P99 straight off the sample   because sorted()[int(.99*n)] is one line
    decode time / output_tokens   because m is the number of tokens

The output is not nonsense. It is a plausible, well-formatted report that reads as
competent, and every figure in it is biased in the direction that flatters the
deployment. On the fixtures in this repo it reports a P99 with no confidence bound
inside the sample, a token rate whose composition is unstated, a TPOT understated
by the ratio m/(m-1), and a mean that is dominated by the requests the harness was
fast enough to send.

The one honest number in it is the request count. That is not a joke -- it is the
reason `tests/test_naive.py` also asserts the count agrees with the corrected
version. Without that control the tests would only prove the library disagrees with
something, not that it disagrees for the reasons stated.
"""


def summarise_naive(run):
    reqs = [r for r in run.requests if r.ok]
    lats = [r.end_s - r.start_s for r in reqs
            if r.end_s is not None and r.start_s is not None]
    if not lats:
        return None
    span = max(r.end_s for r in reqs if r.end_s is not None) - \
        min(r.start_s for r in reqs if r.start_s is not None)
    toks_in = sum(r.input_tokens or 0 for r in reqs)
    toks_out = sum(r.output_tokens or 0 for r in reqs)

    s = sorted(lats)
    p99 = s[int(0.99 * len(s))] if int(0.99 * len(s)) < len(s) else s[-1]

    tpots = []
    for r in reqs:
        if r.first_token_s is not None and r.end_s is not None and r.output_tokens:
            tpots.append((r.end_s - r.first_token_s) / r.output_tokens)

    return {
        "requests": len(reqs),
        "mean_latency_s": sum(lats) / len(lats),
        "p99_latency_s": p99,
        "tokens_per_s": (toks_in + toks_out) / span if span else None,
        "mean_tpot_s": sum(tpots) / len(tpots) if tpots else None,
    }
