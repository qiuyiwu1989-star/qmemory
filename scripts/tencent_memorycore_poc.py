#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from qmemory.service import MemoryService
from qmemory.tencent_memorycore import MemoryCoreIdentity, TencentMemoryCoreLab


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Export QMemory conversations into an isolated TencentDB MemoryCore lab format."
    )
    result.add_argument("project", type=Path)
    result.add_argument("output", type=Path)
    result.add_argument("--import-local", action="store_true")
    result.add_argument("--base-url", default="http://127.0.0.1:8420")
    result.add_argument("--api-key")
    result.add_argument("--service-id", default="qmemory-lab")
    result.add_argument("--team-id", default="qmemory-lab-team")
    result.add_argument("--user-id", default="qmemory-lab-user")
    result.add_argument("--agent-id", default="qmemory-lab-agent")
    return result


def main() -> int:
    args = parser().parse_args()
    lab = TencentMemoryCoreLab(MemoryService())
    exported = lab.export_project(args.project, args.output)
    result = {"export": exported}
    if args.import_local:
        identity = MemoryCoreIdentity(
            team_id=args.team_id,
            user_id=args.user_id,
            agent_id=args.agent_id,
            service_id=args.service_id,
        )
        result["import"] = lab.import_export(
            args.output,
            identity,
            base_url=args.base_url,
            api_key=args.api_key,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

