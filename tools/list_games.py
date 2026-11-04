from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from steamshotbridge.games import (
    Game,
    GameError,
    create_custom_game,
    default_config_path,
    list_games,
    normalize_game_name,
)
from steamshotbridge.shortcuts import (
    ShortcutError,
    ShortcutResult,
    ensure_shortcut,
)
from steamshotbridge.steam import (
    SteamError,
    find_steam_installation,
    resolve_userdata_id,
    screenshot_files,
    userdata_path,
)


def _resolve_userdata(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    installation = find_steam_installation()
    user_id, _ = resolve_userdata_id(installation.path)
    return userdata_path(installation.path, user_id)


def _resolve_config(explicit: Path | None) -> Path:
    return explicit if explicit is not None else default_config_path()


def _screenshot_count(userdata: Path, game: Game) -> int:
    folder = userdata / "760" / "remote" / str(game.game_id)
    return len(screenshot_files(folder))


def _kind_label(game: Game) -> str:
    return f"{game.kind} (órfã)" if game.orphan else game.kind


def _command_list(args: argparse.Namespace) -> int:
    try:
        userdata = _resolve_userdata(args.steam_userdata)
        config = _resolve_config(args.config)
        games = list_games(userdata, config)
    except (GameError, SteamError, OSError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    rows = [
        (
            str(game.game_id),
            _kind_label(game),
            game.name,
            str(_screenshot_count(userdata, game)),
        )
        for game in games
    ]
    headers = ("GAME_ID", "KIND", "NAME", "SCREENSHOTS")
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        if rows
        else len(headers[index])
        for index in range(len(headers))
    ]
    print(
        f"{headers[0]:<{widths[0]}}  "
        f"{headers[1]:<{widths[1]}}  "
        f"{headers[2]:<{widths[2]}}  "
        f"{headers[3]}"
    )
    for game_id, kind, name, count in rows:
        print(
            f"{game_id:<{widths[0]}}  "
            f"{kind:<{widths[1]}}  "
            f"{name:<{widths[2]}}  "
            f"{count}"
        )
    return 0


def _command_add(args: argparse.Namespace) -> int:
    try:
        game = create_custom_game(args.name, _resolve_config(args.config))
    except (GameError, OSError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(game.game_id)
    return 0


def _select_game(
    games: list[Game],
    game_id: str | None,
    name: str | None,
) -> Game | None:
    if game_id is not None:
        matches = [game for game in games if str(game.game_id) == game_id]
    else:
        normalized = normalize_game_name(name or "")
        matches = [game for game in games if game.name == normalized]

    if not matches:
        target = game_id if game_id is not None else name
        print(f"Jogo não encontrado: {target}", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(
            "Mais de um jogo encontrado. Use --game-id:",
            file=sys.stderr,
        )
        for game in matches:
            print(
                f"  {game.game_id}\t{game.kind}\t{game.name}",
                file=sys.stderr,
            )
        return None
    return matches[0]


def _shortcut_field_type(value: object) -> str:
    if isinstance(value, Mapping):
        return "dict"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "string"
    if isinstance(value, float):
        return "float"
    if isinstance(value, bytes):
        return "bytes"
    return type(value).__name__


def _shortcut_field_value(value: object) -> str:
    if isinstance(value, Mapping) and not value:
        return "{}"
    return repr(value)


def _print_shortcut_fields(result: ShortcutResult) -> None:
    if result.entry is None:
        return
    print("Campos do atalho que seria adicionado:")
    for key, value in result.entry.items():
        print(
            f"  {key}: {_shortcut_field_type(value)} = "
            f"{_shortcut_field_value(value)}"
        )
    print(f"appid int32 com sinal: {result.appid_signed}")
    print(f"appid uint32 decimal sem sinal: {result.appid32}")


def _command_create_shortcut(args: argparse.Namespace) -> int:
    try:
        userdata = _resolve_userdata(args.steam_userdata)
        config = _resolve_config(args.config)
        game = _select_game(
            list_games(userdata, config),
            args.game_id,
            args.name,
        )
        if game is None:
            return 1
        if game.game_id <= 0xFFFFFFFF:
            print(
                "create-shortcut exige um jogo não Steam de 64 bits.",
                file=sys.stderr,
            )
            return 1
        result = ensure_shortcut(
            userdata,
            game.game_id,
            game.name,
            exe=args.exe,
            hidden=args.hidden,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (GameError, ShortcutError, SteamError, OSError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    print(f"Status: {result.status}")
    print(f"Jogo: {result.game_id} ({result.name})")
    print(f"Atalho: {result.shortcut_key}")
    print(f"VDF: {result.path}")
    if result.status == "dry_run":
        _print_shortcut_fields(result)
    if result.backup_path is not None:
        print(f"Backup: {result.backup_path}")
    if result.pending is not None:
        print("Atalho pendente:")
        print(f"  caminho: {result.pending.path}")
        print(f"  game_id: {result.pending.game_id}")
        print(f"  nome: {result.pending.name}")
        for key, value in result.pending.entry.items():
            print(f"  {key}: {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lista e cria jogos conhecidos pelo SteamShotBridge."
    )
    parser.add_argument("--steam-userdata", type=Path)
    parser.add_argument("--config", type=Path)

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="Lista todos os jogos.")
    list_parser.add_argument(
        "--steam-userdata",
        type=Path,
        default=argparse.SUPPRESS,
    )
    list_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
    )
    list_parser.set_defaults(handler=_command_list)

    add_parser = subparsers.add_parser("add", help="Cria um jogo custom.")
    add_parser.add_argument("--name", required=True)
    add_parser.add_argument(
        "--steam-userdata",
        type=Path,
        default=argparse.SUPPRESS,
    )
    add_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
    )
    add_parser.set_defaults(handler=_command_add)

    create_shortcut_parser = subparsers.add_parser(
        "create-shortcut",
        help="Cria o atalho de um jogo não Steam existente.",
    )
    target = create_shortcut_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--game-id")
    target.add_argument("--name")
    create_shortcut_parser.add_argument("--exe")
    create_shortcut_parser.add_argument("--hidden", action="store_true")
    create_shortcut_parser.add_argument("--force", action="store_true")
    create_shortcut_parser.add_argument("--dry-run", action="store_true")
    create_shortcut_parser.add_argument(
        "--steam-userdata",
        type=Path,
        default=argparse.SUPPRESS,
    )
    create_shortcut_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
    )
    create_shortcut_parser.set_defaults(handler=_command_create_shortcut)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
