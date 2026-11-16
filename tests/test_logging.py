import asyncio

import logfire
from logfire.testing import CaptureLogfire
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.observability.logging import (
    EXTRA_SCRUB_PATTERNS,
    correlation_scope,
    current_correlation_id,
)

FAKE_KEY = "0x" + "cd" * 32


def _attributes(capfire: CaptureLogfire) -> list[dict[str, object]]:
    return [dict(span.attributes or {}) for span in capfire.exporter.exported_spans]


def test_secrets_are_scrubbed() -> None:
    exporter = InMemorySpanExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(exporter)],
        scrubbing=logfire.ScrubbingOptions(extra_patterns=EXTRA_SCRUB_PATTERNS),
    )
    logfire.info(
        "order signed",
        private_key=FAKE_KEY,
        signature="0xsig-value",
        database_url="postgresql://u:pw@h/db",
        coin="BTC",
    )
    dumped = str([dict(span.attributes or {}) for span in exporter.get_finished_spans()])
    assert FAKE_KEY not in dumped
    assert "0xsig-value" not in dumped
    assert "pw@h" not in dumped
    assert "BTC" in dumped


def test_correlation_id_added_to_spans(capfire: CaptureLogfire) -> None:
    with correlation_scope("cid-123") as cid:
        assert cid == "cid-123"
        assert current_correlation_id() == "cid-123"
        logfire.info("decision")
    assert current_correlation_id() is None
    attrs = [a for a in _attributes(capfire) if a.get("logfire.msg") == "decision"]
    assert attrs and attrs[0].get("correlation_id") == "cid-123"


async def test_correlation_id_isolated_between_tasks() -> None:
    async def worker(name: str) -> str | None:
        with correlation_scope(name):
            await asyncio.sleep(0)
            return current_correlation_id()

    results = await asyncio.gather(worker("a"), worker("b"))
    assert list(results) == ["a", "b"]
