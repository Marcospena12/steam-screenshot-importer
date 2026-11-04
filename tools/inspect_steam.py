from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from steamshotbridge.inspection import main


if __name__ == "__main__":
    raise SystemExit(
        main(default_output=PROJECT_ROOT / "inspect_output.txt")
    )
