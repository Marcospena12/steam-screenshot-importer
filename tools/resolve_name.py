from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from steamshotbridge.name_resolver import resolve_game_name_diagnostic


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Descobre automaticamente o nome do jogo a partir do executável "
            "em execução (somente leitura)."
        )
    )
    parser.add_argument(
        "--exe",
        required=True,
        help="Nome do executável (ex: RocketLeague, aomx).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    diag = resolve_game_name_diagnostic(args.exe)

    print(f"Executável: {diag.exe_name}")
    print(f"Nome escolhido: {diag.chosen_name}")
    print(f"Fonte: {diag.chosen_source}")
    print()

    print("Processo em execução:")
    if diag.process_found:
        print(f"  PID: {diag.pid}")
        print(f"  Caminho: {diag.exe_path or '(indisponível)'}")
    else:
        print("  (nenhum processo encontrado em execução)")
    print()

    print("Candidatos avaliados por etapa:")
    for idx, cand in enumerate(diag.candidates, 1):
        status = "válido" if cand.valid else f"rejeitado ({cand.rejection_reason})"
        val_str = f'"{cand.value}"' if cand.value is not None else "(ausente)"
        print(f"  {idx}. [{cand.source}] {val_str} -> {status}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
