"""
Pure-numpy Ridge regression — no external pip dependencies.

Predicts BASE LOAD only (= total consumption minus known device watts).
Device contributions are modelled separately via historical averages
and added back in the coordinator forecast.

Feature vector layout (22 elements)
─────────────────────────────────────
Time (cyclic):
  h_sin, h_cos          hour-of-day
  d_sin, d_cos          day-of-week
  m_sin, m_cos          month-of-year

Context:
  is_workday            0/1
  any_person_home       0/1  (at least one tracked person home)
  n_persons_home_n      0…1  (fraction of tracked persons at home)

Weather:
  temperature_n         (T - 15) / 15
  cloud_n               cloud_cover / 100
  heat_deg              max(0, 18 - T) / 10   heating demand proxy
  cool_deg              max(0, T - 23) / 10   cooling demand proxy

Interactions (base-load relevant cross-terms):
  is_workday × h_sin    workday morning/evening shape
  is_workday × h_cos
  any_home   × h_sin    presence time patterns
  any_home   × h_cos
  temp_n     × h_sin    temperature-time shape (linear)
  temp_n     × h_cos
  heat_deg   × h_sin    heating-time shape (non-linear, asymmetric)
  heat_deg   × h_cos

Bias:
  1.0
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


def _cyc(v: float, p: float) -> tuple[float, float]:
    a = 2.0 * math.pi * v / p
    return math.sin(a), math.cos(a)


def build_features(
    *,
    hour: int,
    day_of_week: int,
    month: int,
    is_workday: int,
    presence: dict[str, int],
    n_persons_total: int,
    temperature: float,
    cloud_cover: float,
) -> list[float]:

    h_sin, h_cos = _cyc(hour, 24)
    d_sin, d_cos = _cyc(day_of_week, 7)
    m_sin, m_cos = _cyc(month - 1, 12)

    any_home = float(any(v >= 0.5 for v in presence.values())) if presence else 0.0
    n_home_n = sum(presence.values()) / max(n_persons_total, 1)

    temp_n   = (temperature - 15.0) / 15.0
    heat_deg = max(0.0, 18.0 - temperature) / 10.0   # heating demand proxy
    cool_deg = max(0.0, temperature - 23.0) / 10.0   # cooling demand proxy
    cloud_n  = cloud_cover / 100.0

    wday = float(is_workday)

    return [
        h_sin, h_cos,
        d_sin, d_cos,
        m_sin, m_cos,
        wday,
        any_home,
        n_home_n,
        temp_n,
        cloud_n,
        heat_deg,
        cool_deg,
        # interactions
        wday     * h_sin,
        wday     * h_cos,
        any_home * h_sin,
        any_home * h_cos,
        temp_n   * h_sin,
        temp_n   * h_cos,
        heat_deg * h_sin,
        heat_deg * h_cos,
        1.0,  # bias
    ]


@dataclass
class RidgeModel:
    alpha: float = 10.0
    weights: np.ndarray | None = field(default=None, repr=False)
    is_fitted: bool = False
    n_samples: int = 0
    r2: float = 0.0
    rmse: float = 0.0
    mae: float = 0.0

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        sample_weight: np.ndarray | None = None,
    ) -> None:
        """
        Fit Ridge regression, optionally with per-sample weights.

        Weighted closed-form: A = Xᵀ W X + αI,  b = Xᵀ W y
        where W = diag(sample_weight).
        """
        n = X.shape[1]
        if sample_weight is not None:
            W = sample_weight                     # shape (n_samples,)
            A = (X.T * W) @ X + self.alpha * np.eye(n)
            b = X.T @ (W * y)
        else:
            A = X.T @ X + self.alpha * np.eye(n)
            b = X.T @ y

        try:
            self.weights = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            self.weights = np.linalg.lstsq(A, b, rcond=None)[0]

        self.is_fitted = True
        self.n_samples = len(y)
        yp  = self._raw(X)
        res = y - yp
        ss_res = float(np.sum(res ** 2))
        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        self.r2   = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
        self.rmse = float(np.sqrt(np.mean(res ** 2)))
        self.mae  = float(np.mean(np.abs(res)))

    def predict_one(self, features: list[float]) -> float:
        if not self.is_fitted or self.weights is None:
            return 0.0
        return float(max(0.0, np.dot(np.array(features, dtype=float), self.weights)))

    def _raw(self, X: np.ndarray) -> np.ndarray:
        assert self.weights is not None
        return np.maximum(0.0, X @ self.weights)
