"""Render single expressions or comma-separated output columns."""

import ast

from simpleeval import SimpleEval


def render_expression(evaluator: SimpleEval, expr: str, parsed: ast.AST) -> str:
    """Evaluate output columns through SimpleEval and join their values with tabs."""
    if isinstance(parsed, ast.Expr) and isinstance(parsed.value, ast.Tuple):
        return "\t".join(
            str(evaluator.eval(expr, previously_parsed=column))
            for column in parsed.value.elts
        )
    return str(evaluator.eval(expr, previously_parsed=parsed))
