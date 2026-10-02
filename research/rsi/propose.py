"""Fit a real Gaussian-process surrogate to completed development experiments."""

import hashlib
import importlib
import json
import math
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any


def propose(request: dict[str, Any]) -> dict[str, Any]:
    history, choices = request["history"], request["choices"]
    if not 2 <= len(history) <= 100 or not 1 <= len(choices) <= 1000:
        raise ValueError(
            "bounded completed history and unevaluated candidates required"
        )
    inputs: list[list[float]] = [item["parameters"] for item in history]
    scores = [float(item["score"]) for item in history]
    width = len(inputs[0])
    if not 1 <= width <= 10 or any(len(row) != width for row in [*inputs, *choices]):
        raise ValueError("inconsistent model input dimensions")
    if any(
        not math.isfinite(float(value))
        for row in [*inputs, *choices, scores]
        for value in row
    ):
        raise ValueError("nonfinite experiment data")
    gp = importlib.import_module("sklearn.gaussian_process")
    kernels = importlib.import_module("sklearn.gaussian_process.kernels")
    model = gp.GaussianProcessRegressor(
        kernel=kernels.Matern(length_scale=2.0, nu=1.5),
        alpha=1e-6,
        normalize_y=True,
        optimizer=None,
        random_state=0,
    )
    model.fit(inputs, scores)
    mean, uncertainty = model.predict(choices, return_std=True)
    acquisition = [
        float(value + 0.5 * spread)
        for value, spread in zip(mean, uncertainty, strict=True)
    ]
    selected = max(range(len(choices)), key=lambda index: (acquisition[index], -index))
    state = {
        "training_inputs": model.X_train_.tolist(),
        "dual_coefficients": model.alpha_.tolist(),
        "kernel": str(model.kernel_),
    }
    return {
        "selected": choices[selected],
        "predicted_score": float(mean[selected]),
        "uncertainty": float(uncertainty[selected]),
        "acquisition": acquisition[selected],
        "model": "GaussianProcessRegressor/Matern",
        "library_version": version("scikit-learn"),
        "training_digest": hashlib.sha256(
            json.dumps(history, sort_keys=True).encode()
        ).hexdigest(),
        "model_state": state,
        "fit_rows": len(history),
        "promoted": False,
    }


def causal_ridge(request: dict[str, Any]) -> dict[str, Any]:
    family = request.get("model_family", "RIDGE")
    if family not in ("RIDGE", "ELASTIC_NET"):
        raise ValueError("unauthorized learned model family")
    features, labels = request["features"], request["labels"]
    if not 7 <= len(features) == len(labels) <= 512:
        raise ValueError("bounded feature/label series with warmup required")
    width = len(features[0])
    if not 1 <= width <= 10 or any(len(row) != width for row in features):
        raise ValueError("inconsistent signal feature width")
    if any(not math.isfinite(float(value)) for row in features for value in row) or any(
        not math.isfinite(float(value)) for value in labels
    ):
        raise ValueError("nonfinite learning inputs")
    linear = importlib.import_module("sklearn.linear_model")
    predictions: list[float] = []
    training_rows: list[int] = []
    states: list[dict[str, Any]] = []
    cutoffs = request.get("training_cutoffs", list(range(len(features))))
    if len(cutoffs) != len(features) or any(
        type(cutoff) is not int or not 0 <= cutoff <= index
        for index, cutoff in enumerate(cutoffs)
    ):
        raise ValueError("training cutoffs cannot include the current or future label")
    for index, feature in enumerate(features):
        cutoff = cutoffs[index]
        if cutoff < 5:
            predictions.append(0.0)
            training_rows.append(0)
            continue
        model = (
            linear.Ridge(alpha=1.0)
            if family == "RIDGE"
            else linear.ElasticNet(
                alpha=0.001,
                l1_ratio=0.5,
                max_iter=10000,
                selection="cyclic",
                random_state=0,
            )
        )
        model.fit(features[:cutoff], labels[:cutoff])
        predictions.append(float(model.predict([feature])[0]))
        training_rows.append(cutoff)
        states.append(
            {
                "at_index": index,
                "coefficients": model.coef_.tolist(),
                "intercept": float(model.intercept_),
            }
        )
    return {
        "model": "Ridge/expanding-past-only"
        if family == "RIDGE"
        else "ElasticNet/expanding-past-only",
        "library_version": version("scikit-learn"),
        "predictions": predictions,
        "training_rows": training_rows,
        "model_state": states,
        "promoted": False,
    }


def dsr_diagnostic(request: dict[str, Any]) -> dict[str, Any]:
    numpy = importlib.import_module("numpy")
    stats = importlib.import_module("scipy.stats")
    returns = numpy.asarray(request["returns"], dtype=float)
    trial_sharpes = numpy.asarray(request["trial_sharpes"], dtype=float)
    trials = request["total_trials"]
    minimum = request.get("minimum_observations", 60)
    if (
        type(trials) is not int
        or not 2 <= trials <= 1000
        or type(minimum) is not int
        or not 20 <= minimum <= 512
        or returns.ndim != 1
        or trial_sharpes.ndim != 1
        or len(returns) > 512
        or len(trial_sharpes) > trials
        or not numpy.isfinite(returns).all()
        or not numpy.isfinite(trial_sharpes).all()
    ):
        raise ValueError("invalid bounded DSR input")
    result: dict[str, Any] = {
        "model": "DSR/SciPy-per-period",
        "library_version": version("scipy"),
        "observations": len(returns),
        "total_trials": trials,
        "confidence": None,
        "status": "INSUFFICIENT_EVIDENCE",
        "assumption": "NON_NORMAL_IID_APPROXIMATION_NOT_DEPENDENCE_CERTIFICATION",
        "promoted": False,
    }
    if (
        len(returns) < minimum
        or len(trial_sharpes) < 2
        or float(numpy.std(returns, ddof=1)) == 0
    ):
        return result
    dispersion = float(numpy.std(trial_sharpes, ddof=1))
    if dispersion <= 0:
        return result
    observed = float(numpy.mean(returns) / numpy.std(returns, ddof=1))
    skewness = float(stats.skew(returns, bias=False))
    pearson_kurtosis = float(stats.kurtosis(returns, fisher=False, bias=False))
    gaussian_max = (1 - float(numpy.euler_gamma)) * float(
        stats.norm.ppf(1 - 1 / trials)
    )
    gaussian_max += float(numpy.euler_gamma) * float(
        stats.norm.ppf(1 - 1 / (trials * math.e))
    )
    hurdle = dispersion * gaussian_max
    correction = (
        1 - skewness * observed + (pearson_kurtosis - 1) * observed * observed / 4
    )
    if not math.isfinite(correction) or correction <= 0:
        return result
    confidence = float(
        stats.norm.cdf((observed - hurdle) * math.sqrt((len(returns) - 1) / correction))
    )
    result.update(
        status="DIAGNOSTIC_ONLY",
        confidence=confidence,
        per_period_sharpe=observed,
        selection_hurdle=hurdle,
        skewness=skewness,
        pearson_kurtosis=pearson_kurtosis,
    )
    return result


def block_bootstrap(request: dict[str, Any]) -> dict[str, Any]:
    numpy = importlib.import_module("numpy")
    segments = [numpy.asarray(values, dtype=float) for values in request["segments"]]
    trials = request["total_trials"]
    lengths = request["block_lengths"]
    samples, seed = 8192, 0
    observations = sum(len(values) for values in segments)
    if (
        type(trials) is not int
        or not 1 <= trials <= 1000
        or not 2 <= len(segments) <= 6
        or observations > 512
        or not 1 <= len(lengths) <= 4
        or len(set(lengths)) != len(lengths)
        or any(type(length) is not int or not 2 <= length <= 32 for length in lengths)
        or any(
            values.ndim != 1 or not numpy.isfinite(values).all() for values in segments
        )
    ):
        raise ValueError("invalid bounded block bootstrap input")
    result: dict[str, Any] = {
        "model": "NumPy/circular-block-bootstrap",
        "library_version": version("numpy"),
        "observations": observations,
        "total_trials": trials,
        "block_lengths": lengths,
        "resamples": samples,
        "seed": seed,
        "assumption": "WITHIN_FOLD_STATIONARITY_NOT_MARKET_CERTIFICATION",
        "lower_bound": None,
        "status": "INSUFFICIENT_EVIDENCE",
        "promoted": False,
    }
    alpha = 0.05 / trials
    if (
        observations < 60
        or samples * alpha < 5
        or any(len(values) < 4 * max(lengths) for values in segments)
    ):
        return result
    bounds: list[float] = []
    for length in lengths:
        generator = numpy.random.default_rng(seed)
        sums = numpy.zeros(samples)
        for values in segments:
            count = len(values)
            starts = generator.integers(
                0, count, size=(samples, math.ceil(count / length))
            )
            indices = (starts[:, :, None] + numpy.arange(length)) % count
            indices = indices.reshape(samples, -1)[:, :count]
            sums += values[indices].sum(axis=1)
        bounds.append(float(numpy.quantile(sums / observations, alpha)))
    result.update(status="DIAGNOSTIC_ONLY", lower_bound=min(bounds), bounds=bounds)
    return result


if __name__ == "__main__":
    payload = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError("proposal request exceeds byte limit")
    request = json.loads(payload)
    if request.get("task") == "causal_ridge":
        result = causal_ridge(request)
    elif request.get("task") == "dsr":
        result = dsr_diagnostic(request)
    elif request.get("task") == "block_bootstrap":
        result = block_bootstrap(request)
    else:
        result = propose(request)
    result["input_sha256"] = hashlib.sha256(payload).hexdigest()
    result["code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["lock_sha256"] = hashlib.sha256(
        Path("/opt/requirements.lock").read_bytes()
    ).hexdigest()
    print(json.dumps(result, sort_keys=True, allow_nan=False))
