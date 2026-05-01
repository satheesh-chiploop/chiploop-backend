import sys

from chiploop_sdk import ChipLoopClient


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python examples/sdk/download_artifacts.py <workflow_id>")

    workflow_id = sys.argv[1]
    client = ChipLoopClient()
    artifacts = client.list_artifacts(workflow_id)
    print(artifacts)

    for name in artifacts.get("files", []):
        target = client.download_artifact(workflow_id, name, "./chiploop_outputs")
        print(f"downloaded {target}")


if __name__ == "__main__":
    main()
