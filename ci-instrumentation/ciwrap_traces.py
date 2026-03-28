"""Distributed trace export for CI steps.

Provides three operations:
  - init_trace(): write deterministic trace/span IDs to a context file
  - finish_trace(): export the parent job span
  - export_step_span(): export a child span for a single CI step

Trace and span IDs are deterministic (SHA-256 of normalized CI_* env vars set
by setup_ci_context.sh) so every ciwrap.py invocation in the same job shares the
same trace.

Spans are sent via OTLP HTTP to localhost:4318 (the OTel Collector).
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanContext, TraceFlags, SpanKind, NonRecordingSpan, StatusCode


def _load_ci_env_fallback():
    """Read CI_* vars from ci-env.sh if they are missing from the environment.

    Needed for GitLab CI where after_script runs in a separate shell and doesn't
    inherit vars from before_script. No-op on GitHub Actions.
    """
    if os.environ.get("CI_RUN_ID"):
        return  # vars already present, nothing to do
    env_file = Path("artifacts/otel/ci-env.sh")
    if not env_file.exists():
        return
    with env_file.open() as f:
        for line in f:
            line = line.strip()
            if not line.startswith("export "):
                continue
            key, _, val = line[len("export "):].partition("=")
            # Strip surrounding quotes added by setup_ci_context.sh
            val = val.strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_ci_env_fallback()


def _ci_env():
    return (
        os.environ.get("CI_RUN_ID", ""),
        os.environ.get("CI_RUN_ATTEMPT", "1"),
        os.environ.get("CI_JOB_NAME", ""),
        os.environ.get("CI_RUNNER_ID", ""),
    )


def _deterministic_trace_id(run_id, run_attempt, job, runner):
    digest = hashlib.sha256(f"{run_id}:{run_attempt}:{job}:{runner}".encode()).digest()
    return digest[:16]


def _deterministic_span_id(run_id, run_attempt, job, runner, suffix="job"):
    digest = hashlib.sha256(f"{run_id}:{run_attempt}:{job}:{runner}:{suffix}".encode()).digest()
    return digest[:8]


def _context_path():
    workspace = os.environ.get("CI_WORKSPACE", os.getcwd())
    return Path(workspace) / "artifacts" / "otel" / "trace-context.json"


def _read_context():
    p = _context_path()
    if not p.exists():
        return None
    return json.loads(p.read_text())


class _DeterministicIdGenerator:
    """ID generator that returns pre-set IDs instead of random ones.

    The OTel SDK normally generates random trace/span IDs.  We override that
    so every process in the same CI job produces spans with the same trace ID.
    """

    def __init__(self, trace_id_bytes, span_id_bytes):
        self._deterministic_trace_id = int.from_bytes(trace_id_bytes, "big")
        self._deterministic_span_id = int.from_bytes(span_id_bytes, "big")

    def generate_trace_id(self):
        return self._deterministic_trace_id

    def generate_span_id(self):
        return self._deterministic_span_id


def _export_span(name, trace_id_bytes, span_id_bytes, parent_span_id_bytes,
                 start_time_ns, end_time_ns, attributes=None, is_error=False):
    run_id, run_attempt, job, runner = _ci_env()
    resource = Resource.create({})

    id_gen = _DeterministicIdGenerator(trace_id_bytes, span_id_bytes)
    provider = TracerProvider(resource=resource, id_generator=id_gen)
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(timeout=5)))
    tracer = provider.get_tracer("ci-observability")

    # If this span has a parent, create a remote SpanContext for it.
    parent_ctx = None
    if parent_span_id_bytes:
        remote_span = SpanContext(
            trace_id=int.from_bytes(trace_id_bytes, "big"),
            span_id=int.from_bytes(parent_span_id_bytes, "big"),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        parent_ctx = otel_trace.set_span_in_context(NonRecordingSpan(remote_span))

    span = tracer.start_span(
        name=name,
        context=parent_ctx,
        kind=SpanKind.INTERNAL,
        start_time=start_time_ns,
        attributes=attributes or {},
    )
    span.set_status(StatusCode.ERROR if is_error else StatusCode.OK)
    span.end(end_time=end_time_ns)

    provider.force_flush()
    provider.shutdown()


def get_step_ids(step_name):
    """Return (trace_id_hex, span_id_hex) for a step, or (None, None) if no context."""
    ctx = _read_context()
    if ctx is None:
        return None, None
    run_id, run_attempt, job, runner = _ci_env()
    step_span_id = _deterministic_span_id(run_id, run_attempt, job, runner, step_name)
    return ctx["trace_id"], step_span_id.hex()


def init_trace():
    """Compute deterministic IDs and write the trace context file."""
    run_id, run_attempt, job, runner = _ci_env()
    if not run_id or not job:
        print("WARNING: CI_RUN_ID or CI_JOB_NAME not set; "
              "trace IDs will be non-deterministic", file=sys.stderr)

    trace_id = _deterministic_trace_id(run_id, run_attempt, job, runner)
    job_span_id = _deterministic_span_id(run_id, run_attempt, job, runner, "job")

    ctx_path = _context_path()
    ctx_path.parent.mkdir(parents=True, exist_ok=True)
    ctx_path.write_text(json.dumps({
        "trace_id": trace_id.hex(),
        "job_span_id": job_span_id.hex(),
        "start_time_ns": time.time_ns(),
    }))

    print(f"Trace context written to {ctx_path}")
    print(f"  trace_id:    {trace_id.hex()}")
    print(f"  job_span_id: {job_span_id.hex()}")


def finish_trace():
    """Export the job-level parent span (covers the whole job duration)."""
    ctx = _read_context()
    if ctx is None:
        print(f"ERROR: trace context not found: {_context_path()}", file=sys.stderr)
        return 1

    trace_id = bytes.fromhex(ctx["trace_id"])
    job_span_id = bytes.fromhex(ctx["job_span_id"])
    end_time_ns = time.time_ns()
    job_name = os.environ.get("CI_JOB_NAME", "ci-job")

    _export_span(
        name=f"job: {job_name}",
        trace_id_bytes=trace_id,
        span_id_bytes=job_span_id,
        parent_span_id_bytes=None,
        start_time_ns=ctx["start_time_ns"],
        end_time_ns=end_time_ns,
        attributes={
            "cicd.pipeline.job.name": job_name,
            "cicd.pipeline.job.duration": (end_time_ns - ctx["start_time_ns"]) / 1e9,
        },
    )
    print(f"Job span exported (trace_id={ctx['trace_id']}, span_id={ctx['job_span_id']})")


def export_step_span(step_name, start_ns, end_ns, exit_code, duration, proc_metrics=None,
                     step_target=None):
    ctx = _read_context()
    if ctx is None:
        print(f"WARNING: No trace context found for step '{step_name}'. "
              f"Did you forget to run --init-trace?",
              file=sys.stderr)
        return

    trace_id = bytes.fromhex(ctx["trace_id"])
    job_span_id = bytes.fromhex(ctx["job_span_id"])

    run_id, run_attempt, job, runner = _ci_env()
    step_span_id = _deterministic_span_id(run_id, run_attempt, job, runner, step_name)

    result = "failure" if exit_code != 0 else "success"

    pipeline_run_url = os.environ.get("CI_PIPELINE_RUN_URL", "")
    attributes = {
        "cicd.pipeline.task.name": step_name,
        "cicd.pipeline.task.run.url.full": pipeline_run_url,
        "cicd.pipeline.task.run.result": result,
        "cicd.pipeline.task.run.duration": duration,  # ! not in OTel semantic convention
        # Also set as span attribute so Tempo indexes it for TraceQL filtering.
        # Resource attributes are not indexed by Grafana Cloud Tempo for custom keys.
        "cicd.pipeline.run.id": run_id,
    }
    effective_target = step_target or os.environ.get("CI_JOB_TARGET")
    if effective_target:
        attributes["cicd.pipeline.task.target"] = effective_target
    if proc_metrics:
        attributes.update(proc_metrics)

    _export_span(
        name=step_name,
        trace_id_bytes=trace_id,
        span_id_bytes=step_span_id,
        parent_span_id_bytes=job_span_id,
        start_time_ns=start_ns,
        end_time_ns=end_ns,
        attributes=attributes,
        is_error=(exit_code != 0),
    )
    print(f"Step span exported: {step_name} (result={result}, duration={duration:.2f}s)")
