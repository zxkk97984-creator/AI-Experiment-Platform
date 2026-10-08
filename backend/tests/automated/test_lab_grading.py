import copy
import csv
import math

import pytest
from pydantic import ValidationError

from app.schemas.lab import LabCriterion
from app.services.lab_grading import LabGradingNeedsReview, compute_metrics, evidence_cells, metric_score, validate_marks


def write_csv(path, rows):
    with path.open("w", newline="") as file:
        csv.writer(file).writerows(rows)
    return path


def test_classification_metrics_are_grounded_in_predictions(tmp_path):
    labels = write_csv(tmp_path / "labels.csv", [["sample_id", "label"], ["1", "a"], ["2", "b"]])
    predicted = write_csv(tmp_path / "pred.csv", [["sample_id", "prediction"], ["2", "b"], ["1", "a"]])
    assert compute_metrics("tabular_classification", labels, predicted, ["a", "b"]) == {"accuracy": 1, "macro_f1": 1}
    predicted = write_csv(predicted, [["sample_id", "prediction"], ["1", "a"], ["2", "a"]])
    metrics = compute_metrics("tabular_classification", labels, predicted, ["a", "b"])
    assert metrics["accuracy"] == .5 and metrics["macro_f1"] == pytest.approx(1 / 3)


@pytest.mark.parametrize("rows", [
    [["1", "a"]], [["1", "a"], ["1", "b"]], [["1", "a"], ["2", "b"], ["3", "b"]],
    [["1", "a"], ["2", "undefined"]], [["", "a"], ["2", "b"]],
])
def test_prediction_ids_and_classes_must_match_exactly(tmp_path, rows):
    labels = write_csv(tmp_path / "labels.csv", [["sample_id", "label"], ["1", "a"], ["2", "b"]])
    prediction = write_csv(tmp_path / "pred.csv", [["sample_id", "prediction"], *rows])
    with pytest.raises(ValueError):
        compute_metrics("tabular_classification", labels, prediction, ["a", "b"])


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1e309", "1e308", "not-a-number"])
def test_regression_rejects_nonfinite_and_overflow(tmp_path, bad):
    labels = write_csv(tmp_path / "labels.csv", [["sample_id", "label"], ["1", "1"]])
    prediction = write_csv(tmp_path / "pred.csv", [["sample_id", "prediction"], ["1", bad]])
    with pytest.raises(ValueError):
        compute_metrics("tabular_regression", labels, prediction)


def test_regression_and_linear_grade_boundaries(tmp_path):
    labels = write_csv(tmp_path / "labels.csv", [["sample_id", "label"], ["1", "2"], ["2", "4"]])
    prediction = write_csv(tmp_path / "pred.csv", [["sample_id", "prediction"], ["1", "1"], ["2", "6"]])
    metrics = compute_metrics("tabular_regression", labels, prediction)
    assert metrics == {"rmse": math.sqrt(2.5), "mae": 1.5}
    config = {"metric": "rmse", "zero_baseline": 10, "full_target": 2}
    assert [metric_score(config, {"rmse": x}, 40) for x in [12, 10, 6, 2, 0]] == [0, 0, 20, 40, 40]


def test_prediction_duplicate_columns_are_not_accepted(tmp_path):
    labels = write_csv(tmp_path / "labels.csv", [["sample_id", "label"], ["1", "a"]])
    prediction = write_csv(tmp_path / "pred.csv", [["sample_id", "prediction", "prediction"], ["1", "wrong", "a"]])
    with pytest.raises(ValueError):
        compute_metrics("tabular_classification", labels, prediction, ["a"])


@pytest.fixture()
def marking():
    criteria = [{"id": "method", "name": "Algorithm", "max_score": 100,
        "levels": [{"id": "pass", "points": 100}, {"id": "fail", "points": 0}]}]
    cells = [{"id": "code", "source": "x = 1\nprint(x)"}]
    raw = {"needs_review": False, "feedback": "good", "items": [{"criterion_id": "method",
        "level_id": "pass", "reason": "implemented", "evidence": [{"cell_id": "code",
            "line_start": 1, "line_end": 1, "quote": "x = 1"}]}]}
    return criteria, cells, raw


def test_ai_valid_evidence_selects_teacher_level(marking):
    criteria, cells, raw = marking
    items, feedback = validate_marks(raw, criteria, cells)
    assert items[0]["score"] == 100 and feedback == "good"


def test_external_training_file_is_addressable_ai_evidence(marking):
    criteria, _, raw = marking
    cells = evidence_cells({"cells": [], "source_files": {"train.py": "x = 1\nprint(x)"}, "source": "external"})
    raw["items"][0]["evidence"][0]["cell_id"] = "file:train.py"
    items, _ = validate_marks(raw, criteria, cells)
    assert items[0]["score"] == 100


@pytest.mark.parametrize("fault", ["missing", "duplicate", "invented_level", "invented_cell", "fake_quote", "bad_lines", "review"])
def test_ai_invalid_or_uncertain_evidence_never_publishes(marking, fault):
    criteria, cells, original = marking
    raw = copy.deepcopy(original)
    if fault == "missing": raw["items"] = []
    elif fault == "duplicate": raw["items"] *= 2
    elif fault == "invented_level": raw["items"][0]["level_id"] = "bonus"
    elif fault == "invented_cell": raw["items"][0]["evidence"][0]["cell_id"] = "unknown"
    elif fault == "fake_quote": raw["items"][0]["evidence"][0]["quote"] = "model.fit(X)"
    elif fault == "bad_lines": raw["items"][0]["evidence"][0]["line_end"] = 999
    else: raw["needs_review"] = True
    with pytest.raises(LabGradingNeedsReview):
        validate_marks(raw, criteria, cells)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -1, 101])
def test_teacher_rubric_rejects_nonfinite_and_invalid_scores(score):
    with pytest.raises(ValidationError):
        LabCriterion(id="method", name="Method", description="Complete method", max_score=score,
            levels=[{"id": "zero", "description": "not done", "points": 0},
                    {"id": "full", "description": "done", "points": score}])
