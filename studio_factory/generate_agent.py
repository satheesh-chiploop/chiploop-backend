import argparse
import json
from pathlib import Path

from .models import AgentFactoryRequest, AgentFactoryResult
from .patch_writer import write_factory_plan
from .planner import plan_factory_request
from .validator import validate_factory_plan


def run_factory(
    request: AgentFactoryRequest,
    *,
    registry_dir: str = "registry",
    dry_run: bool = True,
    output_dir: str = ".",
) -> AgentFactoryResult:
    plan = plan_factory_request(request, registry_dir=registry_dir)
    errors = validate_factory_plan(plan, registry_dir=registry_dir)
    written = []

    if not dry_run and not errors and plan.decision not in {"reuse_existing", "extend_or_create_variant"}:
        written = write_factory_plan(plan, output_dir=output_dir)

    return AgentFactoryResult(
        ok=not errors,
        dry_run=dry_run,
        plan=plan,
        written_files=written,
        errors=errors,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely plan or generate a reviewable Studio agent stub.")
    parser.add_argument("--request", required=True, help="Path to a JSON AgentFactoryRequest.")
    parser.add_argument("--registry-dir", default="registry")
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Default mode; do not write files.")
    parser.add_argument("--write", action="store_true", help="Write only to safe generated output directories.")
    args = parser.parse_args()

    data = json.loads(Path(args.request).read_text(encoding="utf-8"))
    result = run_factory(
        AgentFactoryRequest.from_dict(data),
        registry_dir=args.registry_dir,
        dry_run=not args.write,
        output_dir=args.output_dir,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
