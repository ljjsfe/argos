"""Tests for PlannerCoder response parsing — JSON-fence recovery (P0-1)."""

from __future__ import annotations

from dataline.agents.planner_coder import _parse_response
from dataline.core.types import PlanStep


def test_parse_well_formed_json_block() -> None:
    response = """```json
{"plan": "count male patients", "language": "sql", "reasoning": "simple count"}
```

```sql
SELECT COUNT(*) FROM Patient WHERE SEX = 'M'
```
"""
    out = _parse_response(response)
    assert out.parse_status == "ok"
    assert out.language == "sql"
    assert out.plan.step_description == "count male patients"
    assert len(out.candidates) == 1


def test_parse_missing_json_fence_with_sql_candidate_infers_sql() -> None:
    """LLM omitted ```json block but candidate is clearly SQL.

    Pre-fix behavior: language defaulted to "python", silently routing
    SQL output through the python path. Post-fix: detect SQL candidate
    and set language="sql".
    """
    response = """```sql
SELECT COUNT(DISTINCT ID) FROM Patient WHERE SEX = 'M'
```
"""
    out = _parse_response(response)
    assert out.parse_status == "fence_missing_recovered"
    assert out.language == "sql"


def test_parse_missing_json_with_python_candidate_keeps_python() -> None:
    response = """```python
import pandas as pd
df = pd.read_csv('x.csv')
print(df.shape)
```
"""
    out = _parse_response(response)
    assert out.parse_status == "fence_missing_recovered"
    assert out.language == "python"


def test_parse_missing_json_uses_prior_plan_description() -> None:
    """Recovery should reuse prior plan rather than the placeholder."""
    prior = PlanStep(step_description="count male patients with normal WBC")
    response = """```sql
SELECT COUNT(*) FROM Patient WHERE SEX = 'M'
```
"""
    out = _parse_response(response, prior_plan=prior)
    assert out.parse_status == "fence_missing_recovered"
    assert out.plan.step_description == "count male patients with normal WBC"


def test_parse_missing_json_no_prior_falls_back_to_placeholder() -> None:
    response = """```sql
SELECT 1
```
"""
    out = _parse_response(response)
    assert out.parse_status == "fence_missing_recovered"
    assert out.plan.step_description == "Execute analysis step"


def test_parse_no_json_no_candidates_marks_failed() -> None:
    response = "I cannot answer this question."
    out = _parse_response(response)
    assert out.parse_status == "fence_missing_failed"


def test_parse_inline_json_still_works() -> None:
    response = """{"plan": "inline plan", "language": "sql"}

```sql
SELECT 1
```
"""
    out = _parse_response(response)
    assert out.parse_status == "ok"
    assert out.plan.step_description == "inline plan"
    assert out.language == "sql"


def test_parse_explicit_python_language_respected() -> None:
    """When JSON specifies python, do not override even if candidate looks SQL-ish."""
    response = """```json
{"plan": "compute via pandas", "language": "python"}
```

```python
import pandas as pd
df = pd.read_csv('x.csv')
```
"""
    out = _parse_response(response)
    assert out.language == "python"
    assert out.parse_status == "ok"
