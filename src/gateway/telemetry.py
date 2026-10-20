"""Bounded-cardinality metrics and metadata-only distributed traces."""

import logging
import os

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


def build_tracer():
    provider = TracerProvider(resource=Resource.create({"service.name": "llm-gateway"}))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    return provider, provider.get_tracer("gateway")


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "gateway_requests", "Completed API requests", ["team", "status"], registry=self.registry
        )
        self.attempts = Counter(
            "gateway_provider_attempts",
            "Provider attempts",
            ["provider", "outcome"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "gateway_request_seconds",
            "End to end gateway latency",
            ["team"],
            registry=self.registry,
        )
        self.provider_latency = Histogram(
            "gateway_provider_seconds",
            "Provider attempt latency",
            ["provider"],
            registry=self.registry,
        )
        self.tokens = Counter(
            "gateway_tokens", "Reported tokens", ["team", "direction"], registry=self.registry
        )
        self.cost = Counter(
            "gateway_accounted_microusd",
            "Actual or conservative accounted cost",
            ["team", "estimated"],
            registry=self.registry,
        )
        self.fallback = Counter(
            "gateway_fallback",
            "Requests selecting an alternate provider",
            ["model"],
            registry=self.registry,
        )
        self.circuit = Gauge(
            "gateway_circuit_open",
            "1 when a per-worker circuit is open or probing",
            ["provider"],
            registry=self.registry,
        )
        self.budget = Gauge(
            "gateway_budget_utilization",
            "Current daily accounted spend divided by cap",
            ["team"],
            registry=self.registry,
        )
        self.logger = logging.getLogger("gateway")
