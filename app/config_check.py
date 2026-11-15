"""Validate configuration without opening network connections or printing secrets."""

import argparse
import json

from app.core.config import ConfigurationError, load_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("development", "test", "company"))
    parser.add_argument("--env-file")
    args = parser.parse_args()
    try:
        loaded = load_config(environment=args.environment, env_file=args.env_file)
    except ConfigurationError as exc:
        print(json.dumps({"status": "invalid", "reason": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "status": "valid",
                "environment": loaded.environment,
                "configuration_fields": len(type(loaded.settings).model_fields),
                "network_connections": 0,
                "milestone_signoff": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
