"""Артефакт модели (§14.4): model.cbm + artifact.json рядом с ним.

artifact.json содержит всё, что нужно для воспроизведения прогноза и проверки входа:
упорядоченные признаки, версию и хеш кода pipeline, границы значений, параметры target,
период обучения, конфигурацию CatBoost, температуру калибровки, пороги, метрики и хеши.
"""

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.features.target import CLASSES

ARTIFACT_FORMAT = 1
MODEL_FILE = "model.cbm"
ARTIFACT_FILE = "artifact.json"


class ArtifactError(ValueError):
    """Артефакт повреждён или несовместим — модель использовать нельзя (HOLD)."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def model_fingerprint(model: Any) -> str:
    """Хеш самой модели (деревья, границы, масштаб) без метаданных CatBoost.

    Файл .cbm при повторном обучении отличается случайным model_guid и временем
    окончания обучения, поэтому воспроизводимость проверяется по отпечатку, а целостность
    файла — по model_sha256.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.json"
        model.save_model(str(path), format="json")
        data = json.loads(path.read_text())
    data.pop("model_info", None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class FeatureBounds:
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    path: Path  # каталог версии
    meta: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.meta["version"])

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(self.meta["feature_names"])

    @property
    def feature_set_version(self) -> str:
        return str(self.meta["feature_set_version"])

    @property
    def temperature(self) -> float:
        return float(self.meta["calibration"]["temperature"])

    @property
    def bounds(self) -> dict[str, FeatureBounds]:
        return {
            name: FeatureBounds(low=b[0], high=b[1])
            for name, b in self.meta["feature_bounds"].items()
        }

    @property
    def class_mean_returns(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.meta["class_mean_returns"].items()}

    @property
    def horizon_candles(self) -> int:
        return int(self.meta["target"]["horizon_candles"])

    @property
    def model_path(self) -> Path:
        return self.path / MODEL_FILE

    @property
    def model_sha256(self) -> str:
        return str(self.meta["hashes"]["model_sha256"])


def write_artifact(path: Path, meta: dict[str, Any]) -> ModelArtifact:
    (path / ARTIFACT_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return ModelArtifact(path=path, meta=meta)


def load_artifact(path: Path) -> ModelArtifact:
    """Загружает и проверяет артефакт: формат, классы, хеш файла модели."""
    meta_path = path / ARTIFACT_FILE
    if not meta_path.is_file():
        raise ArtifactError(f"нет {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("format") != ARTIFACT_FORMAT:
        raise ArtifactError(f"формат артефакта {meta.get('format')}, ожидается {ARTIFACT_FORMAT}")
    if meta.get("classes") != [c.value for c in CLASSES]:
        raise ArtifactError(f"классы модели {meta.get('classes')} не совпадают с {list(CLASSES)}")
    artifact = ModelArtifact(path=path, meta=meta)
    if not artifact.model_path.is_file():
        raise ArtifactError(f"нет файла модели {artifact.model_path}")
    actual = sha256_file(artifact.model_path)
    if actual != artifact.model_sha256:
        raise ArtifactError(f"хеш {MODEL_FILE} не совпадает с артефактом (файл изменён?)")
    return artifact
