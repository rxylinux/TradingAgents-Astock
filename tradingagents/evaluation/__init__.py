"""N04 offline evaluation: frozen cases, deterministic grading, comparison."""

from .schema import EvaluationInputError, canonical_digest
from .grader import evaluate_suite, grade_report
from .comparison import compare_evaluations
from .render import render_comparison_markdown, render_evaluation_markdown

__all__ = [
    "EvaluationInputError",
    "canonical_digest",
    "grade_report",
    "evaluate_suite",
    "compare_evaluations",
    "render_evaluation_markdown",
    "render_comparison_markdown",
]
