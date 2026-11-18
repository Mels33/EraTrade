"""Публичный архив Binance (data.binance.vision): дневные файлы без аккаунта и ключей.

Используются для исследования модели «спрос и предложение»:
- aggTrades — каждая сделка BTCUSDT perp (мс, цена, объём, is_buyer_maker);
- bookDepth — каждые ~30 с накопленная глубина стакана на ±0.2/1/2/3/4/5% от цены.

Каждый файл проверяется по опубликованной sha256 (.CHECKSUM). Нет файла за день (биржа
не выложила) — день пропускается и попадает в отчёт.
"""

import hashlib
from datetime import date
from pathlib import Path

import httpx

BASE = "https://data.binance.vision/data/futures/um/daily"
SYMBOL = "BTCUSDT"
KINDS = ("aggTrades", "bookDepth")


class ArchiveError(RuntimeError):
    pass


def file_name(kind: str, day: date, symbol: str = SYMBOL) -> str:
    return f"{symbol}-{kind}-{day.isoformat()}.zip"


def url(kind: str, day: date, symbol: str = SYMBOL) -> str:
    return f"{BASE}/{kind}/{symbol}/{file_name(kind, day, symbol)}"


def parse_checksum(text: str) -> str:
    """Строка вида «<sha256>  <имя файла>»."""
    digest = text.strip().split()[0] if text.strip() else ""
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
        raise ArchiveError(f"некорректный CHECKSUM: {text[:80]!r}")
    return digest.lower()


def download(
    client: httpx.Client, kind: str, day: date, raw_dir: Path, symbol: str = SYMBOL
) -> Path | None:
    """Скачивает и проверяет дневной файл. None — файла нет в архиве."""
    target = raw_dir / kind / file_name(kind, day, symbol)
    if target.exists():
        return target
    checksum = client.get(url(kind, day, symbol) + ".CHECKSUM")
    if checksum.status_code == 404:
        return None
    checksum.raise_for_status()
    expected = parse_checksum(checksum.text)
    response = client.get(url(kind, day, symbol))
    if response.status_code == 404:
        return None
    response.raise_for_status()
    actual = hashlib.sha256(response.content).hexdigest()
    if actual != expected:
        raise ArchiveError(f"{target.name}: sha256 не совпадает ({actual} != {expected})")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    tmp.write_bytes(response.content)
    tmp.replace(target)
    return target
