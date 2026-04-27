import argparse
import json
import os
from pathlib import Path

from .service import LOCAL_KEY_STORE_ENV, create_api_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a local ChipLoop test API key.")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--store",
        default=os.getenv(LOCAL_KEY_STORE_ENV, ".chiploop/local_api_keys.json"),
        help=f"Local JSON key store. Also set {LOCAL_KEY_STORE_ENV} to this path when running the backend.",
    )
    args = parser.parse_args()

    os.environ[LOCAL_KEY_STORE_ENV] = args.store
    raw_key, record = create_api_key(args.user_id, args.name, test=True)
    print(
        json.dumps(
            {
                "api_key": raw_key,
                "key_prefix": record.key_prefix,
                "user_id": record.user_id,
                "name": record.name,
                "store": str(Path(args.store)),
                "note": "The raw key is shown only now. Store only the key hash in persistence.",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
