from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from steamshotbridge.exe_map import (
    ExeMapError,
    list_exe_map,
    rename_exe,
)
from steamshotbridge.removal import (
    GameRemovalError,
    remove_game,
    restore_game,
)
from steamshotbridge.pending import (
    PendingError,
    apply_pending,
    default_pending_path,
    load_pending,
)
from steamshotbridge.steam import (
    SteamError,
    find_steam_installation,
    resolve_userdata_id,
    userdata_path,
)
from steamshotbridge.watcher import (
    FolderWatcher,
    WatcherError,
)


def _resolve_userdata(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    installation = find_steam_installation()
    user_id, _ = resolve_userdata_id(installation.path)
    return userdata_path(installation.path, user_id)


def _resolve_config(explicit: Path | None) -> Path:
    from steamshotbridge.games import default_config_path

    return explicit if explicit is not None else default_config_path()


def _command_pending(args: argparse.Namespace) -> int:
    pending_path = default_pending_path()
    if not args.apply:
        try:
            entries = load_pending(pending_path)
        except PendingError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1
        if not entries:
            print("Fila vazia.")
            return 0
        for record in entries:
            entry = record.get("entry")
            filename = (
                entry.get("filename")
                if isinstance(entry, dict)
                else None
            )
            shortcut = record.get("shortcut")
            shortcut_name = (
                shortcut.get("name")
                if isinstance(shortcut, dict)
                else None
            )
            print(
                f"{record.get('game_id', '?')}\t"
                f"{record.get('game_name') or '-'}\t"
                f"{filename or '-'}\t"
                f"atalho: {shortcut_name or '-'}"
            )
        return 0

    try:
        userdata = _resolve_userdata(args.steam_userdata)
        result = apply_pending(userdata, pending_path)
    except (PendingError, SteamError, OSError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    if result.status == "vazio":
        print("Fila vazia.")
    elif result.status == "steam_aberta":
        print("Steam aberta: fila mantida.")
    else:
        print(
            f"aplicado: {result.applied} pendência(s) "
            f"({result.screenshots} screenshot(s), "
            f"{result.shortcuts} atalho(s))"
        )
    return 0


def _command_exes(args: argparse.Namespace) -> int:
    try:
        entries = list_exe_map(_resolve_config(args.config))
    except ExeMapError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    print("EXE\tNOME\tGAME_ID\tPROVISORIO")
    for entry in entries:
        print(
            f"{entry.key}\t{entry.name}\t{entry.game_id}\t"
            f"{'sim' if entry.provisional else 'não'}"
        )
    return 0


def _command_rename(args: argparse.Namespace) -> int:
    try:
        userdata = _resolve_userdata(args.steam_userdata)
        config = _resolve_config(args.config)
        pending = getattr(args, "pending", None)
        mapping, status = rename_exe(
            exe=args.exe,
            new_name=args.name,
            steam_userdata_dir=userdata,
            config_path=config,
            pending_path=pending,
        )
    except (ExeMapError, SteamError, OSError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    if status == "pendente":
        print(
            f"Renomeado no exe_map: {args.exe} -> {mapping.name} "
            f"(game_id: {mapping.game_id}). Steam aberta: pendência registrada na fila."
        )
    else:
        print(
            f"Renomeado: {args.exe} -> {mapping.name} "
            f"(game_id: {mapping.game_id}). Steam fechada: VDFs atualizados."
        )
    return 0


def _command_remove(args: argparse.Namespace) -> int:
    try:
        userdata = _resolve_userdata(args.steam_userdata)
        config = _resolve_config(args.config)
        pending = getattr(args, "pending", None)
        result = remove_game(
            game_id=args.game_id,
            steam_userdata_dir=userdata,
            remove_screenshots=args.with_screenshots,
            include_published=args.include_published,
            config_path=config,
            pending_path=pending,
            dry_run=args.dry_run,
        )
    except (GameRemovalError, ExeMapError, SteamError, OSError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    if result.status == "dry_run":
        print(
            f"[dry-run] Jogo: {result.game_name} ({result.game_id}) | "
            f"Atalho seria removido: {'sim' if result.shortcut_removed else 'não'} | "
            f"Screenshots a mover: {result.screenshots_moved} | "
            f"Publicadas preservadas: {result.published_preserved} | "
            f"Publicadas a remover: {result.published_removed}"
        )
    elif result.status == "pendente":
        print(
            f"Remoção pendente: {result.game_name} ({result.game_id}). "
            "Steam aberta: operação registrada na fila para quando a Steam fechar."
        )
    else:
        print(
            f"Removido: {result.game_name} ({result.game_id}). "
            f"Atalho removido: {'sim' if result.shortcut_removed else 'não'}. "
            f"Screenshots movidas: {result.screenshots_moved}. "
            f"Publicadas mantidas: {result.published_preserved}."
        )
    if result.warning:
        print(result.warning)
    return 0


def _command_restore(args: argparse.Namespace) -> int:
    try:
        userdata = _resolve_userdata(args.steam_userdata)
        config = _resolve_config(args.config)
        pending = getattr(args, "pending", None)
        result = restore_game(
            game_id=args.game_id,
            steam_userdata_dir=userdata,
            config_path=config,
            pending_path=pending,
        )
    except (GameRemovalError, ExeMapError, SteamError, OSError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    print(
        f"Restaurado: {result.game_name} ({result.game_id}). "
        f"Status do atalho: {result.shortcut_status}."
    )
    if result.lixeira_folders:
        print("Pastas na lixeira encontradas (screenshots não voltam automaticamente):")
        for folder in result.lixeira_folders:
            print(f"  - {folder}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Monitora uma pasta de capturas e importa cada imagem nova para "
            "o jogo descoberto pelo nome do executável no arquivo."
        )
    )
    parser.add_argument("--folder", type=Path)
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--game-name",
        help="Fallback para arquivos fora do padrão do Afterburner.",
    )
    target.add_argument(
        "--game-id",
        help="Fallback para arquivos fora do padrão do Afterburner.",
    )
    parser.add_argument("--steam-userdata", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--import-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-shortcut", action="store_true")
    parser.add_argument("--hidden-shortcut", action="store_true")

    subparsers = parser.add_subparsers(dest="command")

    pending_parser = subparsers.add_parser(
        "pending",
        help="Lista a fila de pendências ou aplica com --apply.",
    )
    pending_parser.add_argument("--apply", action="store_true")
    pending_parser.add_argument(
        "--steam-userdata",
        type=Path,
        default=argparse.SUPPRESS,
    )
    pending_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
    )
    pending_parser.set_defaults(handler=_command_pending)

    exes_parser = subparsers.add_parser(
        "exes",
        help="Lista o mapa exe -> jogo.",
    )
    exes_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
    )
    exes_parser.set_defaults(handler=_command_exes)

    rename_parser = subparsers.add_parser(
        "rename",
        help="Renomeia manualmente um jogo mapeado.",
    )
    rename_parser.add_argument(
        "--exe",
        required=True,
        help="Nome do executável.",
    )
    rename_parser.add_argument(
        "--name",
        required=True,
        help="Novo nome do jogo.",
    )
    rename_parser.add_argument(
        "--steam-userdata",
        type=Path,
        default=argparse.SUPPRESS,
    )
    rename_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
    )
    rename_parser.add_argument(
        "--pending",
        type=Path,
        default=argparse.SUPPRESS,
        help="Caminho do arquivo pending.json.",
    )
    rename_parser.set_defaults(handler=_command_rename)

    remove_parser = subparsers.add_parser(
        "remove",
        help="Exclui um jogo do aplicativo.",
    )
    remove_parser.add_argument("--game-id", required=True, help="Game ID do jogo.")
    remove_parser.add_argument(
        "--with-screenshots",
        action="store_true",
        help="Move screenshots deste jogo para a lixeira.",
    )
    remove_parser.add_argument(
        "--include-published",
        action="store_true",
        help=(
            "Remove também as screenshots publicadas do índice local e move "
            "os arquivos para a lixeira (não apaga da comunidade Steam)."
        ),
    )
    remove_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula a remoção sem alterações.",
    )
    remove_parser.add_argument(
        "--steam-userdata",
        type=Path,
        default=argparse.SUPPRESS,
    )
    remove_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
    )
    remove_parser.add_argument(
        "--pending",
        type=Path,
        default=argparse.SUPPRESS,
    )
    remove_parser.set_defaults(handler=_command_remove)

    restore_parser = subparsers.add_parser(
        "restore",
        help="Restaura um jogo ignorado.",
    )
    restore_parser.add_argument("--game-id", required=True, help="Game ID do jogo.")
    restore_parser.add_argument(
        "--steam-userdata",
        type=Path,
        default=argparse.SUPPRESS,
    )
    restore_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
    )
    restore_parser.add_argument(
        "--pending",
        type=Path,
        default=argparse.SUPPRESS,
    )
    restore_parser.set_defaults(handler=_command_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if handler is not None:
        return handler(args)

    if args.folder is None:
        parser.error("--folder é obrigatório para monitorar")

    try:
        userdata = _resolve_userdata(args.steam_userdata)
        watcher = FolderWatcher(
            args.folder,
            steam_userdata_dir=userdata,
            config_path=_resolve_config(args.config),
            fallback_game_name=args.game_name,
            fallback_game_id=args.game_id,
            import_existing=args.import_existing,
            dry_run=args.dry_run,
            create_shortcut=not args.no_shortcut,
            hidden_shortcut=args.hidden_shortcut,
            emit=lambda ev: print(getattr(ev, "mensagem", str(ev)), flush=True),
        )
        watcher.run_forever()
    except (WatcherError, SteamError, OSError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
