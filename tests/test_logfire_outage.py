"""Недоступный Logfire не мешает работе: экспорт идёт в фоне, ошибки не всплывают."""

import subprocess
import sys
import time

SCRIPT = """
import logfire
logfire.configure(
    token="pylf_v1_us_" + "0" * 40,
    send_to_logfire=True,
    console=False,
    advanced=logfire.AdvancedOptions(base_url="http://127.0.0.1:9"),
)
for i in range(50):
    logfire.info("событие {i}", i=i)
with logfire.span("span"):
    pass
logfire.force_flush(timeout_millis=2000)
print("ok")
"""


def test_logging_survives_unreachable_logfire() -> None:
    started = time.monotonic()
    result = subprocess.run(  # noqa: S603 — фиксированный скрипт
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")
    assert time.monotonic() - started < 30
