"""Read what a load generator left behind, and record how much it left behind.

Two shapes of input exist in practice and they are not equivalent:

  * a **per-request log** -- one row per request, with absolute timestamps. Almost
    everything in this library needs it: you cannot tell an open-loop run from a
    closed-loop one, or put a confidence interval on a P99, from summary numbers.
  * a **summary report** -- the JSON `vllm bench serve` and friends print at the
    end. Perfectly real, and the thing most people actually have.

So the parser's job is not only to read the file. It is to record which of the two
it read, because that decides which conclusions are available at all. That record
is `Run.evidence`, and it is this library's honesty field -- the fourth in the
series after regressiondoctor's `noise_basis`, fitdoctor's `basis` and
telemetrydoctor's aggregation contract:

    noise_basis   is this threshold measured or assumed
    basis         where did this number come from
    contract      what arithmetic may I do with this column
    evidence      what may I conclude from this file at all

A summary report is not a small per-request log. It is a different kind of object:
every distributional question has already been answered irreversibly by whoever
produced it, and the answers cannot be re-derived. The rules that need per-request
data return `unknown` on a summary, not a guess.
"""
import csv
import json
import os

PER_REQUEST = "per_request"
SUMMARY_ONLY = "summary_only"

# Column aliases. The canonical names are on the left. These cover the shapes that
# vllm's benchmark_serving, locust, k6 and hand-rolled asyncio harnesses emit; an
# unrecognised column is kept in `Run.extra_columns` rather than dropped silently,
# because a column nobody parsed is exactly where an unnoticed unit lives.
ALIASES = {
    "request_id": ("request_id", "req_id", "id", "index", "n"),
    "arrival_s": ("arrival_s", "arrival", "scheduled_s", "scheduled", "intended_start_s",
                  "target_start_s", "due_s"),
    "start_s": ("start_s", "start", "send_s", "sent_s", "issue_s", "t_start"),
    "first_token_s": ("first_token_s", "first_token", "ttft_abs_s", "t_first_token"),
    "end_s": ("end_s", "end", "finish_s", "done_s", "t_end", "completion_s"),
    "input_tokens": ("input_tokens", "prompt_tokens", "in_tokens", "n_input"),
    "output_tokens": ("output_tokens", "completion_tokens", "generated_tokens",
                      "out_tokens", "n_output"),
    "ok": ("ok", "success", "succeeded", "status_ok"),
}
# Relative-duration columns. If the absolute timestamp is missing but one of these
# is present, the absolute one is reconstructed from `start_s`. Harnesses that log
# `start, ttft_ms, e2e_ms` are common and there is no reason to refuse them -- but
# the reconstruction is recorded, because a reconstructed timestamp inherits every
# rounding the harness applied before writing it.
DURATION_ALIASES = {
    "first_token_s": (("ttft_ms", 1e-3), ("ttft_s", 1.0), ("ttft", 1.0)),
    "end_s": (("e2e_ms", 1e-3), ("latency_ms", 1e-3), ("e2el_ms", 1e-3),
              ("e2e_s", 1.0), ("latency_s", 1.0), ("duration_s", 1.0)),
}

# Summary-report field aliases, canonical name on the left. Millisecond fields keep
# their `_ms` name: converting them here would hide the unit from every error
# message downstream, and the unit is the thing people get wrong.
SUMMARY_ALIASES = {
    "completed": ("completed", "successful_requests", "num_requests", "n_requests"),
    "duration_s": ("duration", "duration_s", "benchmark_duration", "elapsed_s"),
    "request_throughput": ("request_throughput", "requests_per_second", "rps"),
    "output_throughput": ("output_throughput", "output_tokens_per_second",
                          "decode_throughput"),
    "total_token_throughput": ("total_token_throughput", "total_tokens_per_second",
                               "token_throughput", "tokens_per_second"),
    "total_input_tokens": ("total_input_tokens", "total_prompt_tokens"),
    "total_output_tokens": ("total_output_tokens", "total_completion_tokens"),
    "mean_ttft_ms": ("mean_ttft_ms", "avg_ttft_ms"),
    "median_ttft_ms": ("median_ttft_ms", "p50_ttft_ms"),
    "p99_ttft_ms": ("p99_ttft_ms", "p99_ttft"),
    "mean_tpot_ms": ("mean_tpot_ms", "avg_tpot_ms"),
    "p99_tpot_ms": ("p99_tpot_ms",),
    "mean_e2el_ms": ("mean_e2el_ms", "mean_latency_ms", "avg_latency_ms"),
    "p99_e2el_ms": ("p99_e2el_ms", "p99_latency_ms"),
    "max_concurrency": ("max_concurrency", "concurrency", "num_workers", "clients"),
    "request_rate": ("request_rate", "target_rate", "rate"),
}


class Request:
    """One request. Times are seconds from an arbitrary but common origin."""

    __slots__ = ("rid", "arrival_s", "start_s", "first_token_s", "end_s",
                 "input_tokens", "output_tokens", "ok")

    def __init__(self, rid, arrival_s, start_s, first_token_s, end_s,
                 input_tokens, output_tokens, ok=True):
        self.rid = rid
        self.arrival_s = arrival_s
        self.start_s = start_s
        self.first_token_s = first_token_s
        self.end_s = end_s
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.ok = ok

    # -- the two latencies, which are not the same number ---------------------
    def latency_s(self):
        """End-to-end as the harness would report it: from the moment it sent."""
        if self.end_s is None or self.start_s is None:
            return None
        return self.end_s - self.start_s

    def corrected_latency_s(self):
        """From the moment the request was *due*, which is what a user experiences.

        Equal to `latency_s()` whenever the harness kept up. The gap between the two
        is coordinated omission, and it is only visible because the harness wrote
        down its own schedule -- a closed-loop harness has no schedule to write.
        """
        if self.end_s is None or self.arrival_s is None:
            return None
        return self.end_s - self.arrival_s

    def queue_wait_s(self):
        """How late the harness itself was: send time minus due time."""
        if self.arrival_s is None or self.start_s is None:
            return None
        return self.start_s - self.arrival_s

    def ttft_s(self):
        if self.first_token_s is None or self.start_s is None:
            return None
        return self.first_token_s - self.start_s

    def decode_s(self):
        """Time spent generating tokens 2..m. Not defined for a one-token answer."""
        if self.end_s is None or self.first_token_s is None:
            return None
        return self.end_s - self.first_token_s

    def tpot_s(self):
        """Time per output token, with the denominator that is actually correct.

        The first token is TTFT; the remaining `m - 1` are what the decode phase
        produced. Dividing by `m` instead of `m - 1` understates TPOT by a factor
        `m / (m - 1)` -- 33% on a four-token answer, 0.1% on a thousand-token one,
        which is why it survives review on long-output benchmarks and then quietly
        biases every short-output one. Undefined, not zero, at m < 2.
        """
        d = self.decode_s()
        if d is None or self.output_tokens is None or self.output_tokens < 2:
            return None
        return d / (self.output_tokens - 1)


class Summary:
    """A finished report. Every distributional question is already answered."""

    __slots__ = ("fields", "source", "unknown_fields")

    def __init__(self, fields, source="", unknown_fields=()):
        self.fields = fields
        self.source = source
        self.unknown_fields = list(unknown_fields)

    def get(self, name, default=None):
        v = self.fields.get(name)
        return default if v is None else v

    def has(self, *names):
        return all(self.fields.get(n) is not None for n in names)


class Run:
    __slots__ = ("requests", "summary", "evidence", "source", "path",
                 "extra_columns", "reconstructed", "declared_rate",
                 "declared_concurrency")

    def __init__(self, requests=None, summary=None, source="", path="",
                 extra_columns=(), reconstructed=()):
        self.requests = list(requests or ())
        self.summary = summary
        self.source = source
        self.path = path
        self.extra_columns = list(extra_columns)
        self.reconstructed = list(reconstructed)
        self.evidence = PER_REQUEST if self.requests else SUMMARY_ONLY
        self.declared_rate = None
        self.declared_concurrency = None
        if summary is not None:
            self.declared_rate = summary.fields.get("request_rate")
            self.declared_concurrency = summary.fields.get("max_concurrency")

    # -- basic shape ---------------------------------------------------------
    @property
    def n(self):
        return len(self.requests)

    def ok_requests(self):
        return [r for r in self.requests if r.ok]

    def span_s(self):
        """Wall time from the first send to the last completion."""
        if not self.requests:
            return self.summary.get("duration_s") if self.summary else None
        starts = [r.start_s for r in self.requests if r.start_s is not None]
        ends = [r.end_s for r in self.requests if r.end_s is not None]
        if not starts or not ends:
            return None
        return max(ends) - min(starts)

    def has_field(self, name):
        return any(getattr(r, name) is not None for r in self.requests)

    def values(self, fn):
        """Every non-None result of `fn(request)` over successful requests."""
        out = []
        for r in self.ok_requests():
            v = fn(r)
            if v is not None:
                out.append(v)
        return out


# --------------------------------------------------------------------- reading
def _num(s):
    if s is None:
        return None
    s = s.strip()
    if s == "" or s.upper() in ("N/A", "NA", "NAN", "NULL", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _bool(s):
    if s is None:
        return True
    s = s.strip().lower()
    if s in ("", "1", "true", "yes", "y", "ok", "success", "200"):
        return True
    if s in ("0", "false", "no", "n", "error", "fail", "failed"):
        return False
    return True


def _resolve(header):
    """canonical name -> column index, plus the columns nobody claimed."""
    lower = [h.strip().lower() for h in header]
    idx = {}
    claimed = set()
    for canon, names in ALIASES.items():
        for want in names:
            if want in lower:
                idx[canon] = lower.index(want)
                claimed.add(lower.index(want))
                break
    dur = {}
    for canon, opts in DURATION_ALIASES.items():
        if canon in idx:
            continue
        for want, scale in opts:
            if want in lower:
                dur[canon] = (lower.index(want), scale)
                claimed.add(lower.index(want))
                break
    extra = [h for i, h in enumerate(lower) if i not in claimed]
    return idx, dur, extra


def load_requests(path):
    """Read a per-request CSV. Lines starting with `#` are declarations, not data."""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            rows.append(line)
    if not rows:
        raise ValueError("{}: no data rows".format(os.path.basename(path)))
    reader = csv.reader(rows)
    header = next(reader)
    idx, dur, extra = _resolve(header)
    if "start_s" not in idx:
        raise ValueError(
            "{}: no send-time column (looked for {}). Without it there is no "
            "timeline, and every question this tool asks is about the timeline."
            .format(os.path.basename(path), "/".join(ALIASES["start_s"])))

    reqs = []
    for i, row in enumerate(reader):
        if not row:
            continue

        def cell(canon, row=row):
            j = idx.get(canon)
            return row[j] if j is not None and j < len(row) else None

        start = _num(cell("start_s"))
        if start is None:
            continue
        vals = {}
        for canon in ("arrival_s", "first_token_s", "end_s"):
            v = _num(cell(canon))
            if v is None and canon in dur:
                j, scale = dur[canon]
                d = _num(row[j]) if j < len(row) else None
                v = None if d is None else start + d * scale
            vals[canon] = v
        rid = cell("request_id")
        toks_in = _num(cell("input_tokens"))
        toks_out = _num(cell("output_tokens"))
        reqs.append(Request(
            rid=(rid.strip() if rid else str(i)),
            arrival_s=vals["arrival_s"], start_s=start,
            first_token_s=vals["first_token_s"], end_s=vals["end_s"],
            input_tokens=None if toks_in is None else int(toks_in),
            output_tokens=None if toks_out is None else int(toks_out),
            ok=_bool(cell("ok"))))
    if not reqs:
        raise ValueError("{}: header parsed but no usable rows".format(
            os.path.basename(path)))
    reqs.sort(key=lambda r: r.start_s)
    return Run(requests=reqs, source="per_request_csv", path=path,
               extra_columns=extra, reconstructed=sorted(dur))


def _flatten(obj, out, prefix=""):
    """vllm nests some reports one level deep; a flat view is enough to alias on."""
    for k, v in obj.items():
        if isinstance(v, dict):
            _flatten(v, out, prefix)
        elif not isinstance(v, (list, tuple)):
            out.setdefault(k.strip().lower(), v)
            if prefix:
                out.setdefault(prefix + k.strip().lower(), v)


def load_summary(path):
    with open(path, encoding="utf-8-sig") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("{}: top level is not an object".format(os.path.basename(path)))
    flat = {}
    _flatten(raw, flat)
    fields, claimed = {}, set()
    for canon, names in SUMMARY_ALIASES.items():
        for want in names:
            if want in flat and isinstance(flat[want], (int, float)):
                fields[canon] = float(flat[want])
                claimed.add(want)
                break
    unknown = sorted(k for k in flat if k not in claimed)
    if not fields:
        raise ValueError(
            "{}: no recognised summary field. Looked for names like "
            "output_throughput / mean_ttft_ms / p99_e2el_ms."
            .format(os.path.basename(path)))
    return Run(summary=Summary(fields, source=path, unknown_fields=unknown),
               source="summary_json", path=path)


def load(path, fmt=None):
    """Read either shape. `fmt` is 'requests' | 'summary' | None (sniff on extension)."""
    if fmt is None:
        fmt = "summary" if path.lower().endswith(".json") else "requests"
    if fmt == "summary":
        return load_summary(path)
    if fmt == "requests":
        return load_requests(path)
    raise ValueError("unknown format {!r}".format(fmt))
