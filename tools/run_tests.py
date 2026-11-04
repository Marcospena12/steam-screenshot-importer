from __future__ import annotations

import os
import sys
import pytest


def main() -> None:
    args = ["-ra", "-v", "--tb=short"]
    exit_code = pytest.main(args)
    # Usa os._exit para evitar crash no destrutor C++ do Qt/PySide6 (0xC0000409)
    # durante a finalização do interpretador no Windows.
    code = 0 if exit_code == pytest.ExitCode.OK else int(exit_code)
    os._exit(code)


if __name__ == "__main__":
    main()
