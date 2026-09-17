"""Prefill tokens and decode tokens are not the same unit, and tokens/s hides which.

Two numbers routinely get called "tokens per second":

    output tokens / second     what the decode loop produced
    (input + output) / second  what the request objects contained

The second is larger, sometimes by a factor of ten, and it is the one that ends up
on a slide. The problem is not that it is inflated -- it is that its value depends
on the prompt-to-output ratio of the traffic used to measure it, so two systems
measured on different traffic cannot be compared with it, and the same system
measured before and after a change in prompt length appears to have changed speed.

They are also not the same kind of work. Prefill runs the whole prompt through the
model in one batched pass: compute-bound, parallel over sequence length, and its
cost per token falls as the prompt grows. Decode produces one token per forward
pass over the whole model: memory-bandwidth-bound, strictly serial, and its cost
per token is flat. Adding them gives a number whose unit is "token" and whose
meaning is "some mixture of two different resources in a ratio I did not report".

This is telemetrydoctor's aggregation contract in a different domain: the arithmetic
runs, the result is a number, and the number does not answer the question it looks
like it answers.

The second thing in here is the TPOT denominator. Time per output token is decode
time divided by the number of tokens *decode produced*, which is `m - 1` -- the
first one is TTFT and belongs to prefill. Dividing by `m` understates TPOT by
`m/(m-1)`: 0.1% at m=1000, 33% at m=4, undefined at m=1. It therefore passes review
on any long-generation benchmark and biases every short-generation one, which is
the class that includes classification, routing, ranking and structured extraction
-- most of what production traffic actually is.
"""
from . import percentile as _p

# A run whose median answer is shorter than this is one where the TPOT denominator
# is a first-order effect rather than a rounding difference.
SHORT_OUTPUT_MEDIAN = 32
# Above this share of (input+output), the "total tokens/s" figure is mostly prefill
# and says almost nothing about decode speed.
PREFILL_DOMINANT = 0.80


class TokenView:
    __slots__ = ("n", "input_total", "output_total", "span_s", "output_tps",
                 "total_tps", "prefill_share", "median_output", "n_single",
                 "tpot_median", "tpot_naive_median", "tpot_error", "rate_note",
                 "tpot_note")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def analyse(run):
    reqs = [r for r in run.ok_requests()]
    outs = [r.output_tokens for r in reqs if r.output_tokens is not None]
    ins = [r.input_tokens for r in reqs if r.input_tokens is not None]
    span = run.span_s()
    if not outs or not span:
        return None
    out_total = sum(outs)
    in_total = sum(ins) if ins else None
    out_tps = out_total / span
    total_tps = ((in_total + out_total) / span) if in_total is not None else None
    share = (in_total / float(in_total + out_total)) if in_total else None

    tpots = [r.tpot_s() for r in reqs if r.tpot_s() is not None]
    naive = []
    for r in reqs:
        d = r.decode_s()
        if d is not None and r.output_tokens:
            naive.append(d / r.output_tokens)
    med_out = _p.median(outs)
    n_single = sum(1 for m in outs if m < 2)
    tpot_med = _p.median(tpots) if tpots else None
    naive_med = _p.median(naive) if naive else None
    err = (tpot_med / naive_med - 1.0) if (tpot_med and naive_med) else None

    if total_tps is not None:
        rate_note = ("decode {:.1f} tok/s vs {:.1f} tok/s counting the prompt "
                     "({:.2f}x); prompt is {:.1%} of all tokens"
                     .format(out_tps, total_tps, total_tps / out_tps, share))
    else:
        rate_note = ("decode {:.1f} tok/s; no prompt-token column, so the "
                     "prompt/output mix of this run is not recorded and the run is "
                     "not comparable to another one".format(out_tps))
    tbits = []
    if err is not None:
        tbits.append("median TPOT {:.3f} ms with the m-1 denominator vs {:.3f} ms with "
                     "m ({:+.2%}), on a median answer of {:g} tokens"
                     .format(tpot_med * 1e3, naive_med * 1e3, err, med_out))
    if n_single:
        tbits.append("{} request(s) produced fewer than 2 tokens; TPOT is undefined "
                     "for them and they are excluded rather than counted as zero"
                     .format(n_single))
    return TokenView(n=len(reqs), input_total=in_total, output_total=out_total,
                     span_s=span, output_tps=out_tps, total_tps=total_tps,
                     prefill_share=share, median_output=med_out, n_single=n_single,
                     tpot_median=tpot_med, tpot_naive_median=naive_med,
                     tpot_error=err, rate_note=rate_note,
                     tpot_note="; ".join(tbits) if tbits else "TPOT not computable")


def denominator_error(m):
    """How much dividing decode time by m instead of m-1 understates TPOT."""
    if m is None or m < 2:
        return None
    return m / float(m - 1) - 1.0


def summary_view(run):
    """The same two questions, answered as far as a summary report allows."""
    s = run.summary
    if s is None:
        return None
    out_tps = s.get("output_throughput")
    tot_tps = s.get("total_token_throughput")
    bits = []
    if out_tps and tot_tps:
        bits.append("report carries both: decode {:.1f} tok/s and total {:.1f} tok/s "
                    "({:.2f}x). Quote the first; the second moves when the prompt "
                    "length moves.".format(out_tps, tot_tps, tot_tps / out_tps))
    elif tot_tps and not out_tps:
        bits.append("report carries only a total token throughput ({:.1f} tok/s) and "
                    "no decode-only figure, so how much of it is prefill is not "
                    "recoverable from this file".format(tot_tps))
    elif out_tps:
        bits.append("report carries decode throughput ({:.1f} tok/s) only, which is "
                    "the right one to quote".format(out_tps))
    if s.get("mean_tpot_ms") and s.get("mean_ttft_ms") and s.get("mean_e2el_ms") \
            and s.get("total_output_tokens") and s.get("completed"):
        m = s.get("total_output_tokens") / s.get("completed")
        pred_m1 = s.get("mean_ttft_ms") + s.get("mean_tpot_ms") * (m - 1)
        pred_m = s.get("mean_ttft_ms") + s.get("mean_tpot_ms") * m
        obs = s.get("mean_e2el_ms")
        bits.append("reconstructing mean end-to-end from TTFT + TPOT x (m-1) gives "
                    "{:.1f} ms against a reported {:.1f} ms ({:+.1%}); with m instead "
                    "of m-1 it gives {:.1f} ms ({:+.1%}) -- on a mean answer of {:.1f} "
                    "tokens this is how you find out which denominator the harness used"
                    .format(pred_m1, obs, pred_m1 / obs - 1.0, pred_m,
                            pred_m / obs - 1.0, m))
    return "; ".join(bits) if bits else None
