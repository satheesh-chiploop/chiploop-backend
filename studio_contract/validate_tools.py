import argparse
import json

from .tool_validation import validate_tool_availability


def main() -> int:
    parser = argparse.ArgumentParser(description="Optionally check Studio tool availability on this machine.")
    parser.add_argument("--registry-dir", default="registry")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return nonzero when required tools are unavailable. Default is report-only.",
    )
    args = parser.parse_args()

    result = validate_tool_availability(args.registry_dir)
    print(json.dumps(result, indent=2))
    return 1 if args.strict and not result["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
