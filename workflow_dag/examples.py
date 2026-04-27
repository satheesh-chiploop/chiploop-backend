import argparse
import json
from pathlib import Path

from .models import WorkflowDAG
from .planner import dry_run_plan
from .validator import validate_dag


def load_example(path: str = "examples/dag/arch2rtl_dag_preview.json") -> WorkflowDAG:
    return WorkflowDAG.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview a ChipLoop workflow DAG.")
    parser.add_argument("--path", default="examples/dag/arch2rtl_dag_preview.json")
    parser.add_argument("--dry-run", action="store_true", default=True)
    args = parser.parse_args()

    dag = load_example(args.path)
    ok, errors = validate_dag(dag)
    if not ok:
        print(json.dumps({"ok": False, "errors": errors}, indent=2))
        return 1
    print(json.dumps(dry_run_plan(dag), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
