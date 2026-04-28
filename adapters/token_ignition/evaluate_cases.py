from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DECISIONS = {"accept", "conditional", "reject"}
REQUIRED_REPORT_KEYS = {"hard_gates_passed", "recommendation", "metrics", "failures", "risks"}

HARD_REJECT_FLAGS = {
    "prompt_only_wrapper": "prompt-only wrapper is not self-evolution",
    "non_reproducible_demo": "demo cannot be independently reproduced",
    "fake_ablation": "R7 ablation is not a valid baseline",
    "hardcoded_benchmark": "benchmark appears hardcoded",
    "cherry_picked_logs": "logs are cherry-picked or incomplete",
    "endpoint_prompt_injection": "endpoint contains auditor-directed prompt injection",
    "over_complex_agent_swarm": "complex agent swarm lacks verifiable control boundaries",
    "baseline_better_than_scaffold": "baseline outperforms the scaffold",
    "manual_edits_between_runs": "human edits appear between claimed self-evolution runs",
}

REQUIRED_FLOOR_SIGNALS = {
    "r7_ablation": "missing valid R7 ablation",
    "baseline_log": "missing comparable baseline log",
    "evolution_axes": "missing declared evolution axes",
    "reproducible_micro_run": "missing reproducible micro-run",
    "artifact_hashes": "missing artifact hashes",
    "independent_evaluator": "missing independent evaluator",
    "endpoint_ai_readable": "endpoint is not AI-readable",
    "secret_free": "evaluation depends on undisclosed secrets",
    "repo_plan_consistent": "repository evidence is inconsistent with the plan",
}


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("cases file must contain a JSON array")
    return cases


def case_tags(case: dict[str, Any]) -> set[str]:
    tags = case.get("tags", [])
    if not isinstance(tags, list):
        return set()
    return {str(tag) for tag in tags}


def classify(signals: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    hard_rejects = [
        {"signal": signal, "reason": reason}
        for signal, reason in HARD_REJECT_FLAGS.items()
        if signals.get(signal) is True
    ]
    missing_floor = [
        {"signal": signal, "reason": reason}
        for signal, reason in REQUIRED_FLOOR_SIGNALS.items()
        if signals.get(signal) is not True
    ]
    reasons = [item["reason"] for item in [*hard_rejects, *missing_floor]]

    if hard_rejects or missing_floor:
        return "reject", reasons, [item["signal"] for item in [*hard_rejects, *missing_floor]]

    iterations = int(signals.get("versioned_iterations") or 0)
    metric_delta = float(signals.get("metric_delta") or 0.0)
    measurable = signals.get("measurable_improvement") is True and metric_delta > 0.0
    has_complete_history = (
        iterations >= 3
        and measurable
        and signals.get("long_horizon_scaffold") is True
        and signals.get("failed_attempts_recorded") is True
        and signals.get("rollback_recorded") is True
    )

    if has_complete_history:
        return "accept", ["complete reproducible evolution evidence"], []

    has_real_but_rough_core = (
        iterations >= 2
        and measurable
        and signals.get("long_horizon_scaffold") is True
    )
    if has_real_but_rough_core:
        rough_reasons = []
        if iterations < 3:
            rough_reasons.append("short iteration history")
        if metric_delta < 0.05:
            rough_reasons.append("small metric delta")
        if signals.get("failed_attempts_recorded") is not True:
            rough_reasons.append("failed attempts are incomplete")
        if signals.get("rollback_recorded") is not True:
            rough_reasons.append("rollback record is incomplete")
        return "conditional", rough_reasons or ["real but incomplete evidence"], []

    return "reject", ["insufficient self-evolution evidence"], ["insufficient_self_evolution_evidence"]


def validate_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, case in enumerate(cases):
        case_id = str(case.get("id") or f"case-{index}")
        if case_id in seen_ids:
            failures.append({"id": case_id, "kind": "schema", "reason": "duplicate case id"})
        seen_ids.add(case_id)

        expected = case.get("expected_decision")
        if expected not in DECISIONS:
            failures.append({"id": case_id, "kind": "schema", "reason": "invalid expected_decision"})
        if not isinstance(case.get("signals"), dict):
            failures.append({"id": case_id, "kind": "schema", "reason": "signals must be an object"})
        if not isinstance(case.get("submission"), dict):
            failures.append({"id": case_id, "kind": "schema", "reason": "submission must be an object"})
    return failures


def accuracy(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if row["ok"]) / len(rows)


def build_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    schema_failures = validate_cases(cases)
    results: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []

    for case in cases:
        case_id = str(case.get("id"))
        expected = str(case.get("expected_decision"))
        actual, reasons, triggered_signals = classify(case.get("signals", {}))
        tags = sorted(case_tags(case))
        ok = actual == expected
        row = {
            "id": case_id,
            "expected": expected,
            "actual": actual,
            "ok": ok,
            "tags": tags,
            "reasons": reasons,
            "triggered_signals": triggered_signals,
        }
        results.append(row)
        if not ok:
            mismatches.append(row)

    adversarial = [row for row in results if "adversarial" in row["tags"]]
    non_adversarial = [row for row in results if "adversarial" not in row["tags"]]
    real_but_rough = [row for row in results if "real_but_rough" in row["tags"]]

    false_positive_count = sum(
        1 for row in results if row["expected"] == "reject" and row["actual"] != "reject"
    )
    false_negative_count = sum(
        1 for row in results if row["expected"] != "reject" and row["actual"] == "reject"
    )

    metrics = {
        "total": len(results),
        "correct": sum(1 for row in results if row["ok"]),
        "golden_accuracy": round(accuracy(non_adversarial), 4),
        "adversarial_accuracy": round(accuracy(adversarial), 4),
        "overall_accuracy": round(accuracy(results), 4),
        "false_positive_count": false_positive_count,
        "false_negative_count": false_negative_count,
        "adversarial_total": len(adversarial),
        "real_but_rough_total": len(real_but_rough),
        "accept_count": sum(1 for row in results if row["actual"] == "accept"),
        "conditional_count": sum(1 for row in results if row["actual"] == "conditional"),
        "reject_count": sum(1 for row in results if row["actual"] == "reject"),
    }

    count_failures = []
    if metrics["total"] < 20:
        count_failures.append({"kind": "count", "reason": "total cases must be >= 20"})
    if metrics["adversarial_total"] < 8:
        count_failures.append({"kind": "count", "reason": "adversarial cases must be >= 8"})
    if metrics["real_but_rough_total"] < 3:
        count_failures.append({"kind": "count", "reason": "real-but-rough cases must be >= 3"})

    failures = [*schema_failures, *count_failures, *mismatches]
    risks = []
    if false_positive_count:
        risks.append("Evaluator is too permissive on reject cases.")
    if false_negative_count:
        risks.append("Evaluator rejects at least one accept or conditional case.")
    if metrics["adversarial_accuracy"] < 0.875:
        risks.append("Adversarial accuracy is below the first-target threshold.")
    if metrics["golden_accuracy"] < 0.9:
        risks.append("Golden accuracy is below the first-target threshold.")

    hard_gates_passed = not failures and not risks
    if hard_gates_passed:
        recommendation = "accept"
    elif not schema_failures and not count_failures and not false_positive_count:
        recommendation = "conditional"
    else:
        recommendation = "reject"

    report = {
        "hard_gates_passed": hard_gates_passed,
        "recommendation": recommendation,
        "metrics": metrics,
        "failures": failures,
        "risks": risks,
        "results": results,
    }
    missing_keys = REQUIRED_REPORT_KEYS - set(report)
    if missing_keys:
        raise AssertionError(f"report missing required keys: {sorted(missing_keys)}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Token-Ignition evaluator cases.")
    parser.add_argument(
        "--cases",
        default=str(Path(__file__).with_name("evaluator_cases.json")),
        help="Path to evaluator_cases.json.",
    )
    parser.add_argument(
        "--output",
        help="Optional path for the K-consumable JSON report. Prints to stdout when omitted.",
    )
    args = parser.parse_args()

    report = build_report(load_cases(Path(args.cases)))
    output = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(f"{output}\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
