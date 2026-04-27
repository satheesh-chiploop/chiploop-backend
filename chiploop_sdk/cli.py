import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from .client import ChipLoopClient
from .errors import ChipLoopError


def _print(data: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, indent=2))
    elif isinstance(data, dict):
        for key, value in data.items():
            print(f"{key}: {value}")
    else:
        print(data)


def _load_json_file(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chiploop", description="ChipLoop SDK CLI v1")
    parser.add_argument("--base-url", default=None, help="ChipLoop backend base URL. Defaults to CHIPLOOP_BASE_URL.")
    parser.add_argument("--api-key", default=None, help="API key/JWT. Defaults to CHIPLOOP_API_KEY.")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--json", action="store_true", help="Print JSON output where useful.")
    sub = parser.add_subparsers(dest="command", required=True)

    workflows = sub.add_parser("workflows")
    workflows_sub = workflows.add_subparsers(dest="workflows_command", required=True)
    workflows_sub.add_parser("list")

    agents = sub.add_parser("agents")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_sub.add_parser("list")

    run = sub.add_parser("run")
    run.add_argument("workflow")
    spec = run.add_mutually_exclusive_group(required=True)
    spec.add_argument("--spec", help="Spec file path.")
    spec.add_argument("--spec-text", help="Inline spec text.")
    run.add_argument("--inputs", help="Optional JSON file with extra workflow inputs.")

    status = sub.add_parser("status")
    status.add_argument("workflow_id")

    artifacts = sub.add_parser("artifacts")
    artifacts_sub = artifacts.add_subparsers(dest="artifacts_command", required=True)
    artifacts_list = artifacts_sub.add_parser("list")
    artifacts_list.add_argument("workflow_id")
    artifacts_download = artifacts_sub.add_parser("download")
    artifacts_download.add_argument("workflow_id")
    artifacts_download.add_argument("--name", required=True)
    artifacts_download.add_argument("--out", required=True)

    studio = sub.add_parser("studio")
    studio_sub = studio.add_subparsers(dest="studio_command", required=True)
    studio_plan = studio_sub.add_parser("plan-agent")
    studio_plan.add_argument("--request", required=True)
    studio_generate = studio_sub.add_parser("generate-agent")
    studio_generate.add_argument("--request", required=True)
    studio_generate.add_argument("--dry-run", action="store_true", default=True)
    studio_generate.add_argument("--write", action="store_true", help="Request server-side write mode when supported.")

    sub.add_parser("doctor")
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    json_requested = False
    if "--json" in argv:
        json_requested = True
        argv = [arg for arg in argv if arg != "--json"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if json_requested:
        args.json = True

    try:
        client = ChipLoopClient(base_url=args.base_url, api_key=args.api_key, timeout=args.timeout)

        if args.command == "doctor":
            _print({"ok": True, "base_url": client.base_url, "api_key": "set" if client.api_key else "not set"}, args.json)
            return 0

        if args.command == "workflows" and args.workflows_command == "list":
            _print(client.list_workflows(), args.json)
            return 0

        if args.command == "agents" and args.agents_command == "list":
            _print(client.list_agents(), args.json)
            return 0

        if args.command == "run":
            inputs = _load_json_file(args.inputs) if args.inputs else None
            result = client.run_workflow(args.workflow, spec_path=args.spec, spec_text=args.spec_text, inputs=inputs)
            _print(result.to_dict(), args.json)
            return 0

        if args.command == "status":
            _print(client.get_workflow_status(args.workflow_id).raw, args.json)
            return 0

        if args.command == "artifacts" and args.artifacts_command == "list":
            _print(client.list_artifacts(args.workflow_id), args.json)
            return 0

        if args.command == "artifacts" and args.artifacts_command == "download":
            target = client.download_artifact(args.workflow_id, args.name, args.out)
            _print({"downloaded": str(target)}, args.json)
            return 0

        if args.command == "studio" and args.studio_command == "plan-agent":
            _print(client.plan_agent(_load_json_file(args.request)), args.json)
            return 0

        if args.command == "studio" and args.studio_command == "generate-agent":
            dry_run = not args.write
            if args.write:
                print("WARNING: requesting Studio Factory write mode. Review generated outputs before use.", file=sys.stderr)
            _print(client.generate_agent(_load_json_file(args.request), dry_run=dry_run), args.json)
            return 0

        parser.error("unsupported command")
        return 2
    except ChipLoopError as exc:
        print(f"chiploop: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
