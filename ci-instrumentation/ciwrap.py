#!/usr/bin/env python3
"""Wrap a CI command: stream stdout/stderr to console and export as OTLP logs + trace span.

Usage:
  ciwrap.py --name "Task name" -- <command>   # wrap a command
  ciwrap.py --init-trace                      # start trace context
  ciwrap.py --finish-trace                    # export job span
"""

import argparse
import os
import subprocess
import sys
import threading
import time

from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import TraceFlags

import ciwrap_traces as traces


class _ProcessMetricsCollector:
    """Polls a subprocess periodically and records peak CPU, RSS, and cumulative I/O.

    Only instantiated when --process-metrics is passed.  Requires psutil.
    Runs on a daemon thread so it never blocks the main flow.
    """

    def __init__(self, pid, interval=1.0, include_children=False):
        self._pid = pid
        self._interval = interval
        self._include_children = include_children
        self._samples = []  # list of (cpu_percent, rss_bytes, read_bytes, write_bytes)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=self._interval * 3 + 2)

    def _run(self):
        import psutil
        # Cache pid -> Process objects across iterations so cpu_percent() accumulates
        # a real delta. Creating a new Process object per iteration resets the baseline
        # and always returns 0.0 on the first call.
        _tracked_processes = {}  # pid -> psutil.Process

        def _update_processes():
            """Sync cache with currently live processes.

            New processes get a baseline cpu_percent() call (returns 0.0, discarded)
            and are excluded from the current iteration's CPU sample — they'll produce
            a valid reading on the next poll.  Returns the set of newly added PIDs.
            """
            try:
                root = psutil.Process(self._pid)
                alive_pids = {root.pid}
                if self._include_children:
                    for c in root.children(recursive=True):
                        alive_pids.add(c.pid)
            except psutil.NoSuchProcess:
                return set()

            added = set()
            for pid in alive_pids:
                if pid not in _tracked_processes:
                    try:
                        p = psutil.Process(pid)
                        p.cpu_percent()  # baseline — discarded
                        _tracked_processes[pid] = p
                        added.add(pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

            for pid in list(_tracked_processes):
                if pid not in alive_pids:
                    del _tracked_processes[pid]

            return added

        # Initial population — establishes cpu_percent baselines for all current processses.
        _update_processes()

        while not self._stop.is_set():
            self._stop.wait(self._interval)
            added = _update_processes()
            # Exclude newly added processes from CPU sum (their baseline was just set).
            measurable = []
            for pid, p in _tracked_processes.items():
                if pid not in added and p.is_running():
                    measurable.append(p)
            if not measurable:
                if not _tracked_processes:
                    break
                continue
            try:
                cpu = 0.0
                for p in measurable:
                    cpu += p.cpu_percent()
                rss = 0
                for p in _tracked_processes.values():
                    if p.is_running():
                        rss += p.memory_info().rss
                try:
                    io = _tracked_processes[self._pid].io_counters()
                    read_bytes, write_bytes = io.read_bytes, io.write_bytes
                except (psutil.AccessDenied, AttributeError, psutil.NoSuchProcess, KeyError):
                    read_bytes = write_bytes = None
                self._samples.append((cpu, rss, read_bytes, write_bytes))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break

    def summary(self):
        """Return peak/total resource usage as a flat dict ready to merge into span attributes."""
        if not self._samples:
            return {}
        max_cpu_raw = max(s[0] for s in self._samples)
        import psutil as _psutil
        cpu_count = _psutil.cpu_count(logical=True) or 1
        max_cpu = max_cpu_raw / cpu_count  # normalize to 0-100% regardless of core count
        max_rss = max(s[1] for s in self._samples)
        result = {
            "process.cpu.max_percent": max_cpu,
            "process.memory.rss.max_bytes": max_rss,
        }
        io_data = []
        for s in self._samples:
            if s[2] is not None:
                io_data.append((s[2], s[3]))
        if len(io_data) >= 2:
            result["process.disk.read_bytes"] = io_data[-1][0] - io_data[0][0]
            result["process.disk.write_bytes"] = io_data[-1][1] - io_data[0][1]
        return result


def _create_log_emitter(task_name, trace_id_hex, span_id_hex, task_target=None):
    """Create an OTLP log emitter for a CI task. Returns (emit_fn, shutdown_fn)."""
    job_target = os.environ.get("CI_JOB_TARGET", "")
    resource = Resource.create({})
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(timeout=5)))
    logger = provider.get_logger("ci-observability")

    trace_id_int = int(trace_id_hex, 16) if trace_id_hex else 0
    span_id_int = int(span_id_hex, 16) if span_id_hex else 0
    flags = TraceFlags(TraceFlags.SAMPLED) if trace_id_hex else TraceFlags.DEFAULT
    effective_target = task_target or job_target

    def emit(body, severity="INFO", timestamp_ns=None, extra_attrs=None):
        severity_number = SeverityNumber.ERROR if severity == "ERROR" else SeverityNumber.INFO
        attrs = {"cicd.pipeline.task.name": task_name}
        if effective_target:
            attrs["cicd.pipeline.task.target"] = effective_target
        if extra_attrs:
            attrs.update(extra_attrs)
        logger.emit(LogRecord(
            timestamp=timestamp_ns or time.time_ns(),
            trace_id=trace_id_int,
            span_id=span_id_int,
            trace_flags=flags,
            severity_text=severity,
            severity_number=severity_number,
            body=body,
            attributes=attrs,
        ))

    def shutdown():
        provider.force_flush()
        provider.shutdown()

    return emit, shutdown


def _reader(stream, console, buffer, source):
    """Read lines from stream, write to console in real time, and buffer for later OTLP export."""
    while True:
        raw = stream.readline()
        if raw == b"":
            break
        console.write(raw)
        console.flush()
        line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
        if line:
            buffer.append((time.time_ns(), line, source))
    stream.close()


def run_and_tee(cmd, emit, metrics_opts=None):
    """Run cmd, stream output to console, buffer logs, then emit with severity based on exit code.

    process_metrics_opts: dict with 'interval' (float, seconds) and 'include_children' (bool),
                          or None to disable process-level metric collection.
    Returns (exit_code, duration, process_metrics_summary).
    """
    t0 = time.monotonic()
    log_lines = []  # list of (timestamp_ns, body, source)

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    monitor = None
    if metrics_opts is not None:
        monitor = _ProcessMetricsCollector(
            process.pid,
            interval=metrics_opts.get("interval", 1.0),
            include_children=metrics_opts.get("include_children", False),
        )
        monitor.start()

    out_thread = threading.Thread(target=_reader,
                                  args=(process.stdout, sys.stdout.buffer, log_lines, "stdout"))
    err_thread = threading.Thread(target=_reader,
                                  args=(process.stderr, sys.stderr.buffer, log_lines, "stderr"))
    out_thread.start()
    err_thread.start()
    out_thread.join()
    err_thread.join()

    exit_code = process.wait()

    proc_metrics = {}
    if monitor is not None:
        monitor.stop()
        proc_metrics = monitor.summary()

    result = "failure" if exit_code != 0 else "success"
    summary_severity = "ERROR" if exit_code != 0 else "INFO"

    # Flush buffered logs — severity based on stream source
    for ts, body, source in log_lines:
        severity = "ERROR" if source == "stderr" else "INFO"
        emit(body, severity=severity, timestamp_ns=ts,
             extra_attrs={"log.source": source})

    duration = time.monotonic() - t0
    summary = f"__TASK_RESULT__ duration_seconds={duration:.2f} result={result}"
    if proc_metrics:
        if "process.cpu.max_percent" in proc_metrics:
            summary += f" peak_cpu_percent={proc_metrics['process.cpu.max_percent']:.1f}"
        if "process.memory.rss.max_bytes" in proc_metrics:
            summary += f" rss_bytes={int(proc_metrics['process.memory.rss.max_bytes'])}"
        if "process.disk.read_bytes" in proc_metrics:
            summary += f" disk_read_bytes={int(proc_metrics['process.disk.read_bytes'])}"
        if "process.disk.write_bytes" in proc_metrics:
            summary += f" disk_write_bytes={int(proc_metrics['process.disk.write_bytes'])}"
    summary_extra = {
        "log.source": "stdout",
        "cicd.pipeline.task.run.duration": duration,  # ! not in OTEL semantic convention
        "cicd.pipeline.task.run.result": result,
    }
    if proc_metrics:
        summary_extra.update(proc_metrics)
    emit(summary, severity=summary_severity, extra_attrs=summary_extra)
    sys.stdout.buffer.write(f"{summary}\n".encode())
    sys.stdout.buffer.flush()

    return exit_code, duration, proc_metrics


# CLI dispatch

def cmd_run_step(args):
    """Run a wrapped command, emit logs via OTLP, and export a task span."""
    trace_id, span_id = traces.get_task_ids(args.name)
    if trace_id is None:
        print(f"WARNING: No trace context found for task '{args.name}'. "
              f"Did you forget to run --init-trace?",
              file=sys.stderr)
    emit, shutdown_logs = _create_log_emitter(args.name, trace_id, span_id, args.task_target)

    cmd = args.command
    if cmd[0] == "--":
        cmd = cmd[1:]

    process_metrics_opts = None
    if args.process_metrics:
        try:
            import psutil  # noqa: F401
        except ImportError:
            print("WARNING: --process-metrics requires psutil. "
                  "Run: pip install psutil", file=sys.stderr)
        else:
            process_metrics_opts = {
                "interval": args.process_sample_interval,
                "include_children": args.process_include_children,
            }

    start_ns = time.time_ns()
    exit_code, duration, process_metrics = run_and_tee(cmd, emit, process_metrics_opts)
    end_ns = time.time_ns()

    shutdown_logs()

    traces.export_task_span(args.name, start_ns, end_ns, exit_code, duration, process_metrics,
                            args.task_target)

    if args.junit:
        if not os.path.exists(args.junit):
            print(f"WARNING: JUnit file not found, skipping test span export: {args.junit}",
                  file=sys.stderr)
        else:
            import parse_junit
            parse_junit.export_junit(args.junit, args.name, args.task_target)

    return exit_code


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default=None, help="CI task name")
    ap.add_argument("--init-trace", action="store_true",
                    help="Initialize trace context (write trace-context.json)")
    ap.add_argument("--finish-trace", action="store_true",
                    help="Finalize trace and export job span")
    ap.add_argument("--junit", default=None, metavar="XML_FILE",
                    help="Path to JUnit XML file to parse and export as test spans")
    ap.add_argument("--task-target", default=None, metavar="TARGET",
                    help="Component or microservice this task targets (e.g. 'catalog-api'). "
                         "Attached as cicd.pipeline.task.target on the task span and every log record.")
    ap.add_argument("--process-metrics", action="store_true",
                    help="Collect peak CPU/RSS/IO for the subprocess (requires psutil)")
    ap.add_argument("--process-include-children", action="store_true",
                    help="Aggregate child processes into process metrics")
    ap.add_argument("--process-sample-interval", type=float, default=1.0, metavar="SECONDS",
                    help="Sampling interval for process metrics in seconds (default: 1.0)")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    if args.init_trace:
        return traces.init_trace()

    if args.finish_trace:
        return traces.finish_trace()

    if not args.name:
        ap.print_usage(sys.stderr)
        return 2

    if not args.command:
        print("Usage: ciwrap.py --name \"Task name\" -- <command>", file=sys.stderr)
        return 2

    return cmd_run_step(args)


if __name__ == "__main__":
    sys.exit(main())
