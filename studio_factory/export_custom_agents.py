import argparse
import json

from .custom_agent_export import build_custom_agent_registry_patch, load_custom_agent_export, write_custom_agent_patch


def main() -> int:
    parser = argparse.ArgumentParser(description="Build review-only Studio registry metadata for custom agents.")
    parser.add_argument("--input", required=True, help="JSON export containing agents or custom_agents.")
    parser.add_argument("--output", default="generated_patches/custom_agents_registry_export.yaml")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Default mode; do not write files.")
    parser.add_argument("--write", action="store_true", help="Write the review patch under generated_patches/.")
    args = parser.parse_args()

    custom_agents = load_custom_agent_export(args.input)
    patch = build_custom_agent_registry_patch(custom_agents)
    written = []
    if args.write:
        written.append(write_custom_agent_patch(patch, args.output))

    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": not args.write,
                "agent_count": len(patch["agents"]),
                "output": args.output,
                "written_files": written,
                "patch": patch,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
