#!/usr/bin/env python3
# Unified compatibility entry for ai-client-governance commands.

from __future__ import annotations

from pathlib import Path
import sys

AI_CLIENT_GOVERNANCE_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = AI_CLIENT_GOVERNANCE_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ai_client_governance.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
