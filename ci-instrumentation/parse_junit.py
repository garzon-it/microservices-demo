#!/usr/bin/env python3
"""Parse a JUnit XML file and export each test case as an OTel span.

Test spans are children of the CI step span identified by --name.

Usage:
  parse_junit.py --name "Run Playwright tests" <junit-xml-file>
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanContext, TraceFlags, SpanKind, NonRecordingSpan, StatusCode

import ciwrap_traces as traces


def _parse_suite_timestamp_ns(suite):
    """Read the suite's start timestamp and return it in nanoseconds, or None if missing."""
    timestamp_str = suite.get("timestamp")
    if not timestamp_str:
        return None
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)
    except ValueError:
        return None


def _get_test_outcome(testcase_element):
    """Figure out if a test passed, failed, errored, or was skipped.

    JUnit encodes the result as a child element — <failure>, <error>, or <skipped>.
    If none of those are present, the test passed.
    """
    if testcase_element.find("failure") is not None:
        return True, "failed"
    if testcase_element.find("error") is not None:
        return True, "error"
    if testcase_element.find("skipped") is not None:
        return False, "skipped"
    return False, "passed"


def _build_tracer(trace_id_hex, step_span_id_hex):
    """Set up an OTel tracer and a parent context pointing at the step span."""
    resource_attrs = {
        "service.name": os.environ.get("CI_SERVICE_NAME", "unknown_service:ciwrap"),
        "cicd.provider.name": os.environ.get("CI_PROVIDER", "unknown"),
        "cicd.pipeline.job.name": os.environ.get("CI_JOB_NAME", ""),
        "cicd.pipeline.job.target": os.environ.get("CI_JOB_TARGET", ""),
    }
    if os.environ.get("CI_SERVICE_NAMESPACE"):
        resource_attrs["service.namespace"] = os.environ["CI_SERVICE_NAMESPACE"]
    resource = Resource.create(resource_attrs)

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=5)))
    tracer = provider.get_tracer("ci-observability")

    # Point all test spans at the step span as their parent
    step_span_context = SpanContext(
        trace_id=int(trace_id_hex, 16),
        span_id=int(step_span_id_hex, 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    parent_context = otel_trace.set_span_in_context(NonRecordingSpan(step_span_context))

    return provider, tracer, parent_context


def export_junit(xml_path, step_name, step_target=None):
    trace_id_hex, step_span_id_hex = traces.get_step_ids(step_name)
    if trace_id_hex is None:
        print(
            f"ERROR: No trace context found for step '{step_name}'. "
            f"Did you forget to run --init-trace?",
            file=sys.stderr,
        )
        return 1

    tree = ET.parse(xml_path)
    xml_root = tree.getroot()

    # Playwright and .NET wrap everything in <testsuites>; pytest uses a bare <testsuite>
    if xml_root.tag == "testsuites":
        test_suites = list(xml_root.iter("testsuite"))
    else:
        test_suites = [xml_root]

    provider, tracer, parent_context = _build_tracer(trace_id_hex, step_span_id_hex)

    exported_count = 0

    for suite in test_suites:
        suite_name = suite.get("name", "unknown")

        # Start from the suite's timestamp and advance by each test's duration
        # to get approximate per-test start times. Good enough for sequential runners.
        current_time_ns = _parse_suite_timestamp_ns(suite)

        for testcase in suite.findall("testcase"):
            test_name = testcase.get("name", "unknown")
            class_name = testcase.get("classname", suite_name)
            duration_seconds = float(testcase.get("time", 0))
            duration_ns = int(duration_seconds * 1e9)

            is_error, status = _get_test_outcome(testcase)

            if current_time_ns is not None:
                start_ns = current_time_ns
                end_ns = current_time_ns + duration_ns
                current_time_ns = end_ns
            else:
                start_ns = None
                end_ns = None

            span_name = f"{class_name} > {test_name}"
            attrs = {
                "test.suite.name": suite_name,
                "test.case.name": test_name,
                "test.case.result.status": status,
                "test.case.result.duration": duration_seconds,
                "cicd.pipeline.task.name": step_name,
            }
            effective_target = step_target or os.environ.get("CI_JOB_TARGET")
            if effective_target:
                attrs["cicd.pipeline.task.target"] = effective_target
            span = tracer.start_span(
                span_name,
                context=parent_context,
                kind=SpanKind.INTERNAL,
                start_time=start_ns,
                attributes=attrs,
            )
            span.set_status(StatusCode.ERROR if is_error else StatusCode.OK)
            span.end(end_time=end_ns)
            exported_count += 1

    provider.force_flush()
    provider.shutdown()
    print(f"Exported {exported_count} test spans from {xml_path} "
          f"(step='{step_name}', trace={trace_id_hex[:16]}...)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True,
                    help="Name of the ciwrap step that produced the XML file")
    ap.add_argument("xml_file", help="Path to JUnit XML file")
    args = ap.parse_args()
    return export_junit(args.xml_file, args.name)


if __name__ == "__main__":
    sys.exit(main())
