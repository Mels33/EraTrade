"""Команды CLI по кнопке из панели: белый список, отдельный процесс (тяжёлые расчёты не
тормозят API), одна команда за раз, вывод виден построчно.

Команды с ключом API-кошелька (wallet-check, verify-signing) берут его из окружения процесса
API, как и CLI; при работающем боте они честно сообщат, что торговый процесс уже запущен.
"""

import asyncio
import re
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PROGRESS = re.compile(r"^\s*(\S+): (проверка|нет данных)")
_TOTAL = re.compile(r"Монет: (\d+)")
_NOISE = ("hyperliquid.info", "Logfire project URL")


@dataclass(frozen=True, slots=True)
class Choice:
    name: str
    label: str
    values: tuple[str, ...]
    default: str


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    group: str
    label: str
    what: str
    argv: Callable[[dict[str, str]], list[str]]
    choices: tuple[Choice, ...] = ()
    sends_to_exchange: bool = False  # подписывает действие на бирже


COMMANDS: dict[str, CommandSpec] = {
    c.name: c
    for c in (
        CommandSpec(
            "wick-scan",
            "Монеты",
            "Найти новые монеты",
            "Проверить рынки Hyperliquid на истории Binance и обновить отбор для бота.",
            lambda a: ["wick-scan", "--top", a["top"]],
            (Choice("top", "Рынков", ("40", "80", "120"), "80"),),
        ),
        CommandSpec(
            "mm-scan",
            "Монеты",
            "Спреды рынков",
            "Спред, шаг цены, оборот и глубина у лучших рынков прямо сейчас.",
            lambda a: ["mm-scan", "--top", a["top"]],
            (Choice("top", "Рынков", ("30", "50", "80"), "50"),),
        ),
        CommandSpec(
            "account",
            "Счёт",
            "Состояние счёта",
            "Маржа, позиции, открытые заявки, сделки за сутки и лимит запросов адреса.",
            lambda a: ["account"],
        ),
        CommandSpec(
            "status",
            "Счёт",
            "Состояние бота",
            "Режим, запрет входов, сервисы и последние записи журнала.",
            lambda a: ["status"],
        ),
        CommandSpec(
            "wallet-check",
            "Проверки",
            "Проверить ключ",
            "API-кошелёк, срок действия, часы, блокировка торгового процесса. Без подписи.",
            lambda a: ["wallet-check"],
        ),
        CommandSpec(
            "verify-signing",
            "Проверки",
            "Проверить подпись на бирже",
            "Отправляет отмену несуществующего ордера: биржа проверит подпись, ничего не меняется.",
            lambda a: ["verify-signing", "--yes"],
            sends_to_exchange=True,
        ),
        CommandSpec(
            "notify-test",
            "Проверки",
            "Тест уведомления",
            "Отправить тестовое сообщение в Telegram (или в лог, если Telegram не настроен).",
            lambda a: ["notify-test"],
        ),
        CommandSpec(
            "config-check",
            "Проверки",
            "Настройки",
            "Сеть, база, стратегия — без вывода секретов.",
            lambda a: ["config-check"],
        ),
        CommandSpec(
            "db-check",
            "Проверки",
            "База данных",
            "Подключение к PostgreSQL и версия.",
            lambda a: ["db-check"],
        ),
        CommandSpec(
            "averaging-research",
            "Исследования",
            "Усреднение на истории BTC",
            "Сетка вариантов ловли проколов на минутных данных BTC 2023–2026 (около минуты).",
            lambda a: ["averaging-research"],
        ),
        CommandSpec(
            "sd-research",
            "Исследования",
            "Спрос и предложение",
            "Зоны, стены в стакане и дисбаланс на часовых свечах BTC — проверка по кварталам.",
            lambda a: ["sd-research", "--idea", a["idea"]],
            (Choice("idea", "Идея", ("all", "zones", "walls", "imbalance"), "all"),),
        ),
    )
}


class JobError(ValueError):
    pass


def default_runner(args: list[str]) -> list[str]:
    return [sys.executable, "-m", "app.cli", *args]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class JobRunner:
    runner: Callable[[list[str]], list[str]] = default_runner
    clock: Callable[[], datetime] = _utc_now
    name: str | None = None
    args: dict[str, str] = field(default_factory=dict)
    running: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = None
    total: int | None = None
    done: int = 0
    log: deque[str] = field(default_factory=lambda: deque(maxlen=400))
    _task: asyncio.Task[None] | None = None

    def start(self, name: str, args: dict[str, str] | None = None) -> None:
        spec = COMMANDS.get(name)
        if spec is None:
            raise JobError(f"неизвестная команда: {name}")
        if self.running:
            raise JobError(f"уже выполняется: {self.name}")
        values = {c.name: c.default for c in spec.choices}
        for key, value in (args or {}).items():
            choice = next((c for c in spec.choices if c.name == key), None)
            if choice is None or str(value) not in choice.values:
                raise JobError(f"недопустимый параметр {key}={value}")
            values[key] = str(value)
        self.name, self.args, self.running = name, values, True
        self.started_at, self.finished_at, self.returncode = self.clock(), None, None
        self.total, self.done = None, 0
        self.log.clear()
        self._task = asyncio.create_task(self._run(self.runner(spec.argv(values))))

    async def _run(self, argv: list[str]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if proc.stdout is None:
                raise RuntimeError("нет вывода процесса")
            async for raw in proc.stdout:
                self._line(raw.decode(errors="replace").rstrip())
            self.returncode = await proc.wait()
        except Exception as exc:
            self._line(f"ошибка запуска: {exc!r}")
            self.returncode = -1
        finally:
            self.running, self.finished_at = False, self.clock()

    def _line(self, line: str) -> None:
        line = _ANSI.sub("", line)
        if not line.strip() or any(n in line for n in _NOISE):
            return
        if (m := _TOTAL.search(line)) and self.total is None:
            self.total = int(m.group(1))
        if _PROGRESS.match(line):
            self.done += 1
        self.log.append(line)

    def status(self, *, lines: int = 400) -> dict[str, Any]:
        if self.running:
            state = "running"
        elif self.returncode is None:
            state = "idle"
        else:
            state = "done" if self.returncode == 0 else "error"
        return {
            "name": self.name,
            "args": self.args,
            "state": state,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "returncode": self.returncode,
            "total": self.total,
            "done": self.done,
            "log": list(self.log)[-lines:],
        }


def catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": c.name,
            "group": c.group,
            "label": c.label,
            "what": c.what,
            "sends_to_exchange": c.sends_to_exchange,
            "choices": [
                {
                    "name": ch.name,
                    "label": ch.label,
                    "values": list(ch.values),
                    "default": ch.default,
                }
                for ch in c.choices
            ],
        }
        for c in COMMANDS.values()
    ]
