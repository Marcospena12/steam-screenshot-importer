from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from steamshotbridge.games import (
    GameError,
    create_custom_game,
    custom_game_id,
    default_config_path,
    list_games,
    normalize_game_name,
)
from steamshotbridge.importer import (
    ScreenshotImportError,
    import_screenshot,
)
from steamshotbridge.shortcuts import ShortcutError
from steamshotbridge.steam import (
    SteamError,
    find_steam_installation,
    resolve_userdata_id,
    userdata_path,
)


def _resolve_userdata(explicit: Path | None) -> tuple[Path, str]:
    if explicit is not None:
        return explicit, "argumento --steam-userdata"
    installation = find_steam_installation()
    user_id, reason = resolve_userdata_id(installation.path)
    return userdata_path(installation.path, user_id), f"{reason}: {user_id}"


def _resolve_config(explicit: Path | None) -> Path:
    return explicit if explicit is not None else default_config_path()


def _select_game_by_name(
    name: str,
    userdata: Path,
    config_path: Path,
    dry_run: bool,
) -> tuple[str, str] | None:
    normalized = normalize_game_name(name)
    matches = [
        game
        for game in list_games(userdata, config_path)
        if game.name == normalized
    ]
    if len(matches) > 1:
        print(
            f'Mais de um jogo encontrado com o nome "{normalized}". '
            "Use --game-id com um dos IDs abaixo:",
            file=sys.stderr,
        )
        for game in matches:
            print(
                f"  {game.game_id}\t{game.kind}\t{game.name}",
                file=sys.stderr,
            )
        return None
    if len(matches) == 1:
        game = matches[0]
        return str(game.game_id), game.name
    if dry_run:
        return str(custom_game_id(normalized)), normalized
    game = create_custom_game(normalized, config_path)
    return str(game.game_id), game.name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Importa uma imagem para a biblioteca de screenshots da Steam."
    )
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--game-id")
    parser.add_argument("--game-name")
    parser.add_argument("--steam-userdata", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-timeline", action="store_true")
    parser.add_argument("--no-shortcut", action="store_true")
    parser.add_argument("--hidden-shortcut", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.game_id is None and args.game_name is None:
        parser.error("informe --game-id ou --game-name")

    try:
        userdata, userdata_source = _resolve_userdata(args.steam_userdata)
        game_id = args.game_id
        game_name = args.game_name
        if game_id is None:
            selected = _select_game_by_name(
                game_name or "",
                userdata,
                _resolve_config(args.config),
                args.dry_run,
            )
            if selected is None:
                return 1
            game_id, game_name = selected
        result = import_screenshot(
            image_path=args.image,
            game_id=game_id,
            steam_userdata_dir=userdata,
            game_name=game_name,
            include_timeline=not args.no_timeline,
            force=args.force,
            dry_run=args.dry_run,
            create_shortcut=not args.no_shortcut,
            hidden_shortcut=args.hidden_shortcut,
        )
    except (
        GameError,
        ScreenshotImportError,
        ShortcutError,
        SteamError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    print(f"Status: {result.status}")
    print(f"Jogo: {game_id} ({game_name or 'sem nome'})")
    print(f"Userdata: {userdata} ({userdata_source})")
    print(f"Imagem: {result.image_path}")
    print(f"Thumbnail: {result.thumbnail_path}")
    print(f"VDF: {result.vdf_path}")
    print(f"creation: {result.creation}")
    print(f"timelineid: {result.timelineid or '(omitido)'}")
    if result.backup_path is not None:
        print(f"Backup: {result.backup_path}")
    if result.shortcut is not None:
        shortcut = result.shortcut
        print(
            f"Atalho: {shortcut.status} "
            f"(chave {shortcut.shortcut_key}, appid {shortcut.appid_signed})"
        )
        if shortcut.backup_path is not None:
            print(f"Backup do atalho: {shortcut.backup_path}")
        if shortcut.pending is not None:
            print("Atalho pendente:")
            print(f"  caminho: {shortcut.pending.path}")
            print(f"  game_id: {shortcut.pending.game_id}")
            print(f"  nome: {shortcut.pending.name}")
            for key, value in shortcut.pending.entry.items():
                print(f"  {key}: {value}")
    if result.status == "vdf_pendente":
        print("Steam em execução: VDF não alterado.")
        if result.pending is not None:
            print(f"Atalho em shortcutnames: {'sim' if result.pending.shortcutname_added else 'não'}")
            print("Dados pendentes:")
            print(f"  game_id: {result.pending.game_id}")
            print(f"  game_name: {result.pending.game_name or '(não informado)'}")
            for key, value in result.pending.entry.items():
                print(f"  {key}: {value}")
    print("Trecho VDF:")
    sys.stdout.write(result.vdf_snippet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
