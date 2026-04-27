import argparse
import json
from pathlib import Path

from .models import AgentPlanRequest
from .planner import plan_agent


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan whether to reuse, extend, or create a Studio agent.")
    parser.add_argument("--request", required=True, help="Path to a JSON AgentPlanRequest.")
    parser.add_argument("--registry-dir", default="registry")
    args = parser.parse_args()

    data = json.loads(Path(args.request).read_text(encoding="utf-8"))
    result = plan_agent(AgentPlanRequest.from_dict(data), registry_dir=args.registry_dir)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
