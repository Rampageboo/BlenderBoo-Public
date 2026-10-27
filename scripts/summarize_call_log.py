"""Print a JSON summary of local Blender MCP call logs."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_server" / "src"))
from blenderboo_mcp.call_log import summarize


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--since", help="ISO-8601 timestamp with timezone")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since else None
    if since is not None and since.tzinfo is None:
        parser.error("--since requires a timezone")
    print(json.dumps(summarize([args.path], since=since), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
