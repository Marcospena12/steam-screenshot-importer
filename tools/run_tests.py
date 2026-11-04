from __future__ import annotations

import sys
import pytest


def main() -> int:
    args = ["-ra", "-v", "--tb=short"]
    exit_code = pytest.main(args)
    # Se todos os testes passaram (ExitCode.OK == 0), retorna 0 estritamente.
    if exit_code == pytest.ExitCode.OK:
        return 0
    return int(exit_code)


if __name__ == "__main__":
    sys.exit(main())
