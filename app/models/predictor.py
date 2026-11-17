"""Прогноз конкретной версии модели: проверка входа → CatBoost → калибровка температурой."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from catboost import CatBoostClassifier

from app.features.pipeline import (
    FEATURE_NAMES,
    FEATURE_SET_VERSION,
    FeatureVector,
    pipeline_code_hash,
)
from app.features.target import CLASSES
from app.models.artifact import ArtifactError, ModelArtifact
from app.models.calibration import softmax


class OutOfBoundsError(ValueError):
    """Признак вне диапазона, на котором обучалась модель, — решение HOLD."""


@dataclass(frozen=True, slots=True)
class Prediction:
    version: str
    probabilities: dict[str, float]  # калиброванные, в порядке CLASSES
    raw: tuple[float, ...]
    expected_return: float  # Σ p(класс) × средняя доходность класса на train

    @property
    def p_up(self) -> float:
        return self.probabilities["UP"]

    @property
    def p_down(self) -> float:
        return self.probabilities["DOWN"]


class Predictor:
    def __init__(self, artifact: ModelArtifact) -> None:
        if artifact.feature_set_version != FEATURE_SET_VERSION:
            raise ArtifactError(
                f"модель обучена на признаках {artifact.feature_set_version}, "
                f"код считает {FEATURE_SET_VERSION}"
            )
        if artifact.feature_names != FEATURE_NAMES:
            raise ArtifactError("порядок или состав признаков модели не совпадает с pipeline")
        if artifact.meta.get("pipeline_code_sha256") != pipeline_code_hash():
            raise ArtifactError(
                "код feature pipeline изменился после обучения — нужна новая версия модели"
            )
        model = CatBoostClassifier()
        model.load_model(str(artifact.model_path))
        if [str(c) for c in model.classes_] != [c.value for c in CLASSES]:
            raise ArtifactError(f"классы CatBoost {list(model.classes_)} не совпадают")
        self.artifact = artifact
        self._model = model
        self._temperature = artifact.temperature
        self._bounds = artifact.bounds
        self._class_returns = artifact.class_mean_returns

    @property
    def version(self) -> str:
        return self.artifact.version

    def check_bounds(self, vector: FeatureVector) -> None:
        for name, value in zip(vector.names, vector.values, strict=True):
            b = self._bounds[name]
            if not (b.low <= value <= b.high):
                raise OutOfBoundsError(
                    f"{name}={value:.6g} вне диапазона обучения [{b.low:.6g}, {b.high:.6g}]"
                )

    def raw(self, rows: Sequence[Sequence[float]]) -> list[tuple[float, ...]]:
        values = self._model.predict(
            [list(r) for r in rows], prediction_type="RawFormulaVal", thread_count=1
        )
        return [tuple(float(x) for x in row) for row in values]

    def predict(self, vector: FeatureVector) -> Prediction:
        if vector.version != FEATURE_SET_VERSION or vector.names != FEATURE_NAMES:
            raise ArtifactError("вектор признаков другой версии")
        self.check_bounds(vector)
        raw = self.raw([vector.values])[0]
        probs = softmax(raw, self._temperature)
        if not all(math.isfinite(p) for p in probs):
            raise ArtifactError("модель вернула нечисловые вероятности")
        probabilities = {c.value: p for c, p in zip(CLASSES, probs, strict=True)}
        expected = math.fsum(probabilities[k] * self._class_returns[k] for k in probabilities)
        return Prediction(
            version=self.version, probabilities=probabilities, raw=raw, expected_return=expected
        )
