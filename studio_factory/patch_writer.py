from pathlib import Path
from typing import List

from .models import AgentFactoryPlan
from .validator import assert_output_base_safe


def write_factory_plan(plan: AgentFactoryPlan, output_dir: str = ".") -> List[str]:
    base = assert_output_base_safe(output_dir)
    written: List[str] = []

    for file_plan in plan.files_to_generate:
        target = base / file_plan.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_plan.content, encoding="utf-8")
        written.append(str(target))

    return written
