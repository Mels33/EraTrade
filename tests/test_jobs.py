"""Команды CLI из панели: белый список, параметры, одна за раз, вывод и ход."""

import asyncio
import sys

import pytest

from app.api.jobs import COMMANDS, JobError, JobRunner, catalog

SCRIPT = (
    "import sys;"
    "print('\\x1b[1mМонет: 2\\x1b[0m');"
    "print('12:00 hyperliquid.info meta');"
    "print('  NEAR: проверка +12.0%', file=sys.stderr);"
    "print('  ETH: нет данных на Binance', file=sys.stderr);"
    "print('готово')"
)


def fake(script: str = SCRIPT, code: int = 0) -> JobRunner:
    return JobRunner(
        runner=lambda args: [sys.executable, "-c", f"{script};import sys;sys.exit({code})"]
    )


async def finish(job: JobRunner) -> None:
    assert job._task is not None
    await asyncio.wait_for(job._task, 10)


async def test_runs_whitelisted_command_and_parses_output() -> None:
    job = fake()
    job.start("wick-scan", {"top": "40"})
    assert job.status()["state"] == "running" and job.args == {"top": "40"}
    with pytest.raises(JobError, match="уже выполняется"):
        job.start("account")
    await finish(job)
    st = job.status()
    assert st["state"] == "done" and st["total"] == 2 and st["done"] == 2
    assert st["log"] == [
        "Монет: 2",
        "  NEAR: проверка +12.0%",
        "  ETH: нет данных на Binance",
        "готово",
    ]


async def test_failed_process_is_error() -> None:
    job = fake("print('Traceback: boom')", code=1)
    job.start("account")
    await finish(job)
    assert job.status()["state"] == "error" and job.status()["returncode"] == 1


def test_only_known_commands_and_allowed_values() -> None:
    job = fake()
    with pytest.raises(JobError, match="неизвестная"):
        job.start("api")  # сервисы и опасные команды недоступны
    with pytest.raises(JobError, match="недопустимый"):
        job.start("wick-scan", {"top": "9999"})
    with pytest.raises(JobError, match="недопустимый"):
        job.start("account", {"x": "1"})
    names = {c["name"] for c in catalog()}
    assert "wick-scan" in names and not names & {"api", "wick", "mm", "set-mode", "micro-data"}
    assert COMMANDS["verify-signing"].sends_to_exchange
    assert COMMANDS["wick-scan"].argv({"top": "80"}) == ["wick-scan", "--top", "80"]
