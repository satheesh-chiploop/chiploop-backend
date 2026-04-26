import argparse
import json

from .registry import dry_run_validate


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate ChipLoop Studio Contract registry files.")
    parser.add_argument("--registry-dir", default="registry")
    args = parser.parse_args()
    result = dry_run_validate(args.registry_dir)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
