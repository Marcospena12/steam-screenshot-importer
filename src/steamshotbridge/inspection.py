from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from PIL import Image

from .steam import (
    SteamError,
    SteamInstallation,
    calculate_shortcut_ids,
    find_steam_installation,
    find_mapping_key,
    get_mapping_value,
    list_remote_app_dirs,
    list_userdata_ids,
    load_screenshots_vdf,
    load_shortcuts_vdf,
    resolve_userdata_id,
    screenshot_files,
    screenshots_vdf_path,
    shortcut_game_id,
    shortcuts_vdf_path,
    signed_32,
    thumbnail_path,
    unsigned_32,
    userdata_path,
    vdf_text,
)

SECTION_RULE = "=" * 80
MISSING = object()


@dataclass(frozen=True)
class ShortcutFolderInfo:
    key: str
    appname: str
    exe: str
    stored_appid: int
    appid32: int
    gameid64: int

    @property
    def label(self) -> str:
        name = self.appname or self.exe or "(sem nome)"
        return (
            f'atalho VDF [{self.key}]: "{name}"; '
            f"appid gravado {self.stored_appid}"
        )


def _section(number: int, title: str) -> list[str]:
    return ["", SECTION_RULE, f"{number}. {title}", SECTION_RULE]


def _escape_vdf(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def render_vdf_items(items: Iterable[tuple[object, object]], indent: int = 0) -> list[str]:
    pad = "\t" * indent
    lines = [f"{pad}{{"]
    for key, value in items:
        label = f'"{_escape_vdf(key)}"'
        if isinstance(value, Mapping):
            lines.append(f"{pad}\t{label}")
            lines.extend(render_vdf_items(value.items(), indent + 1))
        else:
            lines.append(f"{pad}\t{label}\t\"{_escape_vdf(value)}\"")
    lines.append(f"{pad}}}")
    return lines


def render_vdf_mapping(mapping: Mapping, indent: int = 0) -> list[str]:
    return render_vdf_items(mapping.items(), indent)


def _image_dimensions(path: Path) -> tuple[tuple[int, int] | None, str | None]:
    try:
        with Image.open(path) as image:
            return image.size, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _format_dimensions(size: tuple[int, int] | None, error: str | None) -> str:
    if size is not None:
        return f"{size[0]}x{size[1]}"
    return f"indisponível ({error})"


def _hex_32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}"


def _hex_64(value: int) -> str:
    return f"0x{value & 0xFFFFFFFFFFFFFFFF:016X}"


def _remote_folder_status(steam_path: Path, user_id: str, appid: int | str) -> str:
    remote = userdata_path(steam_path, user_id) / "760" / "remote"
    folder = remote / str(appid)
    if not folder.is_dir():
        return f"{folder} -> não existe"
    count = len(screenshot_files(folder))
    return f"{folder} -> existe ({count} screenshots)"


def _entry_index_key(item: tuple[object, object]) -> tuple[int, object]:
    key = str(item[0])
    return (0, int(key)) if key.isdigit() else (1, key.casefold())


def _shortcut_folder_mappings(
    shortcuts_root: Mapping | None,
) -> dict[str, list[ShortcutFolderInfo]]:
    mappings: dict[str, list[ShortcutFolderInfo]] = {}
    if not isinstance(shortcuts_root, Mapping):
        return mappings

    for shortcut_key, shortcut in shortcuts_root.items():
        if not isinstance(shortcut, Mapping):
            continue
        stored_raw = get_mapping_value(shortcut, "appid", MISSING)
        stored_unsigned = unsigned_32(stored_raw)
        if stored_unsigned is None:
            continue
        saved_as_int = int(stored_raw)
        info = ShortcutFolderInfo(
            key=str(shortcut_key),
            appname=vdf_text(get_mapping_value(shortcut, "appname", "")),
            exe=vdf_text(get_mapping_value(shortcut, "exe", "")),
            stored_appid=saved_as_int,
            appid32=stored_unsigned,
            gameid64=shortcut_game_id(stored_unsigned),
        )
        for folder_name in {str(info.appid32), str(info.gameid64)}:
            mappings.setdefault(folder_name, []).append(info)

    return mappings


def _hex_folder_value(value: int) -> str:
    return _hex_64(value) if value > 0xFFFFFFFF else _hex_32(value)


def _load_shortcuts(
    path: Path,
) -> tuple[Mapping | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        root = load_shortcuts_vdf(path)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    shortcuts = find_mapping_key(root, "shortcuts")
    if shortcuts is None:
        return None, "arquivo lido, mas a chave 'shortcuts' não foi encontrada"
    if not isinstance(shortcuts, Mapping):
        return None, "a chave 'shortcuts' não contém um dicionário"
    return shortcuts, None


def generate_report(
    installation: SteamInstallation,
    user_id: str | None = None,
    appid_filter: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    steam_path = installation.path
    selected_user_id, selection_reason = resolve_userdata_id(steam_path, user_id)
    userdata = userdata_path(steam_path, selected_user_id)
    remote = userdata / "760" / "remote"
    now = generated_at or datetime.now().astimezone()

    all_ids = list_userdata_ids(steam_path)
    app_dirs = list_remote_app_dirs(steam_path, selected_user_id)
    filtered_app_dirs = (
        [entry for entry in app_dirs if entry.name == appid_filter]
        if appid_filter is not None
        else app_dirs
    )

    lines = [
        "SteamShotBridge - inspeção da biblioteca de screenshots da Steam",
        f"Gerado em: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "Modo: somente leitura dentro da pasta da Steam.",
    ]

    lines.extend(_section(1, "INSTALAÇÃO DA STEAM E USERDATA"))
    lines.append(f"Steam.........: {steam_path}")
    lines.append(f"Fonte.........: {installation.source}")
    lines.append(f"Userdata IDs..: {', '.join(all_ids) if all_ids else '(nenhum)'}")
    lines.append(f"Userdata usado: {selected_user_id}")
    lines.append(f"Seleção.......: {selection_reason}")

    lines.extend(_section(2, "SCREENSHOTS POR APP (760\\remote)"))
    lines.append(f"Pasta remote..: {remote}")
    if not app_dirs:
        lines.append("(nenhuma pasta de app encontrada)")
    else:
        lines.append(f"{'AppID':<12} {'Imagens':>8} {'Thumbnails':>11}  Status")
        total_images = 0
        total_thumbnails = 0
        for app_dir in app_dirs:
            images = screenshot_files(app_dir)
            thumbnails = sum(1 for image in images if thumbnail_path(image).is_file())
            total_images += len(images)
            total_thumbnails += thumbnails
            if not images:
                status = "sem screenshots"
            elif thumbnails == len(images):
                status = "OK"
            else:
                status = f"sem thumbnail: {len(images) - thumbnails}"
            lines.append(
                f"{app_dir.name:<12} {len(images):>8} {thumbnails:>11}  {status}"
            )
        lines.append("")
        lines.append(
            f"Total.........: {len(app_dirs)} apps, {total_images} screenshots, "
            f"{total_thumbnails} thumbnails"
        )

    lines.extend(_section(3, "AMOSTRA DE ARQUIVOS E DIMENSÕES (até 5 por app)"))
    if appid_filter is not None:
        lines.append(f"Filtro de appid: {appid_filter}")
        if not filtered_app_dirs:
            lines.append("(appid não encontrado em 760\\remote)")
    if not filtered_app_dirs:
        lines.append("(nenhuma pasta para amostrar)")
    for app_dir in filtered_app_dirs:
        images = screenshot_files(app_dir)
        lines.append("")
        lines.append(f"[AppID {app_dir.name}] {len(images)} screenshots")
        lines.append(f"Pasta: {app_dir / 'screenshots'}")
        if not images:
            lines.append("  (sem screenshots)")
            continue
        for position, image_path in enumerate(images[:5], 1):
            original_size, original_error = _image_dimensions(image_path)
            thumb = thumbnail_path(image_path)
            thumb_size: tuple[int, int] | None = None
            thumb_error: str | None = None
            if thumb.is_file():
                thumb_size, thumb_error = _image_dimensions(thumb)
            else:
                thumb_error = "arquivo ausente"
            lines.append(f"  {position}. {image_path.name}")
            lines.append(f"     original : {_format_dimensions(original_size, original_error)}")
            lines.append(f"     thumbnail: {_format_dimensions(thumb_size, thumb_error)}")
        if len(images) > 5:
            lines.append(f"  ... {len(images) - 5} arquivo(s) adicional(is) não exibido(s)")

    lines.extend(_section(4, "SCREENSHOTS.VDF"))
    screenshot_index_path = screenshots_vdf_path(steam_path, selected_user_id)
    lines.append(f"Arquivo: {screenshot_index_path}")
    screenshots_root: Mapping | None = None
    if not screenshot_index_path.is_file():
        lines.append("Status: ARQUIVO AUSENTE")
    else:
        try:
            screenshot_index = load_screenshots_vdf(screenshot_index_path)
            screenshots_root = find_mapping_key(screenshot_index, "screenshots")
            if not isinstance(screenshots_root, Mapping):
                raise ValueError("a chave 'screenshots' não contém um dicionário")
        except Exception as exc:
            lines.append(f"ERRO ao ler o arquivo: {type(exc).__name__}: {exc}")
            screenshots_root = None

    if screenshots_root is not None:
        app_items = [
            (key, value)
            for key, value in screenshots_root.items()
            if str(key).casefold() != "shortcutnames"
        ]
        shortcutnames_value = find_mapping_key(screenshots_root, "shortcutnames")
        total_entries = sum(
            len(list(value.items()))
            for _, value in app_items
            if isinstance(value, Mapping)
        )
        lines.append(f"Tamanho: {screenshot_index_path.stat().st_size} bytes")
        lines.append(f"Apps indexados: {len(app_items)}")
        lines.append(f"Entradas de screenshots: {total_entries}")
        app_summary = ", ".join(
            f"{key} ({len(list(value.items())) if isinstance(value, Mapping) else 0})"
            for key, value in app_items
        )
        lines.append(f"Resumo por appid: {app_summary or '(nenhum)'}")

        displayed_apps = 0
        for appid, entries in app_items:
            if appid_filter is not None and str(appid) != appid_filter:
                continue
            displayed_apps += 1
            lines.append("")
            if not isinstance(entries, Mapping):
                lines.append(f"--- AppID {appid}: valor sem dicionário ---")
                lines.append(f'"{_escape_vdf(appid)}"\t"{_escape_vdf(entries)}"')
                continue
            ordered_entries = sorted(entries.items(), key=_entry_index_key)
            shown = ordered_entries[:3]
            lines.append(
                f"--- AppID {appid}: {len(ordered_entries)} entrada(s); "
                f"exibindo {len(shown)} ---"
            )
            lines.append(f'"{_escape_vdf(appid)}"')
            lines.extend(render_vdf_items(shown, 0))
            if len(ordered_entries) > 3:
                lines.append(
                    f"Entradas omitidas: {len(ordered_entries) - 3} "
                    "(limite de 3 por appid)."
                )
        if appid_filter is not None and displayed_apps == 0:
            lines.append("")
            lines.append(f"(appid {appid_filter} não encontrado no screenshots.vdf)")

        lines.append("")
        if shortcutnames_value is not None:
            count = (
                len(list(shortcutnames_value.items()))
                if isinstance(shortcutnames_value, Mapping)
                else 0
            )
            lines.append(f"--- Bloco shortcutnames: {count} entrada(s) ---")
            lines.append('"shortcutnames"')
            if isinstance(shortcutnames_value, Mapping):
                lines.extend(render_vdf_mapping(shortcutnames_value, 0))
            else:
                lines.append(f"\t\"{_escape_vdf(shortcutnames_value)}\"")
        else:
            lines.append("Bloco shortcutnames: ausente")

    lines.extend(_section(5, "SHORTCUTS.VDF (ATALHOS NÃO-STEAM)"))
    shortcuts_path = shortcuts_vdf_path(steam_path, selected_user_id)
    lines.append(f"Arquivo: {shortcuts_path}")
    shortcuts_root, shortcuts_error = _load_shortcuts(shortcuts_path)
    shortcut_folder_mappings: dict[str, list[ShortcutFolderInfo]] = {}

    if not shortcuts_path.is_file():
        lines.append("Status: ARQUIVO AUSENTE")
        lines.append(
            "Nenhum atalho não-Steam cadastrado para este userdata; "
            "não há appids de atalhos para calcular."
        )
    elif shortcuts_error is not None:
        lines.append(f"ERRO ao ler o arquivo: {shortcuts_error}")
    elif not shortcuts_root:
        lines.append("Status: arquivo presente, sem atalhos cadastrados")
    else:
        shortcut_folder_mappings = _shortcut_folder_mappings(shortcuts_root)
        lines.append(f"Tamanho: {shortcuts_path.stat().st_size} bytes")
        lines.append(f"Atalhos encontrados: {len(list(shortcuts_root.items()))}")
        for index, (shortcut_key, shortcut) in enumerate(shortcuts_root.items()):
            if not isinstance(shortcut, Mapping):
                lines.append("")
                lines.append(f"--- Atalho [{shortcut_key}] inválido: valor sem dicionário ---")
                continue
            appname = vdf_text(get_mapping_value(shortcut, "appname", ""))
            exe = vdf_text(get_mapping_value(shortcut, "exe", ""))
            calculated = calculate_shortcut_ids(exe, appname)
            stored_raw = get_mapping_value(shortcut, "appid", MISSING)

            lines.append("")
            lines.append(f"--- Atalho [{shortcut_key}] ({index + 1}) ---")
            lines.append(f"appname................: {appname}")
            lines.append(f"exe....................: {exe}")
            if stored_raw is MISSING:
                lines.append("appid gravado..........: (campo ausente)")
            else:
                stored_unsigned = unsigned_32(stored_raw)
                if stored_unsigned is None:
                    lines.append(f"appid gravado..........: {stored_raw!r}")
                else:
                    lines.append(
                        f"appid gravado..........: {stored_unsigned} "
                        f"({_hex_32(stored_unsigned)}), "
                        f"int32 com sinal: {signed_32(stored_unsigned)}"
                    )
            lines.append(f"String usada no CRC....: <{calculated.crc_input}>")
            lines.append(f"Comprimento da string..: {len(calculated.crc_input)} caracteres")
            lines.append(
                f"appid calculado 32bits.: {calculated.appid32} "
                f"({_hex_32(calculated.appid32)})"
            )
            lines.append(
                f"gameid calculado 64bits: {calculated.gameid64} "
                f"({_hex_64(calculated.gameid64)})"
            )
            lines.append(
                "Pasta remote (calculada): "
                + _remote_folder_status(
                    steam_path, selected_user_id, calculated.appid32
                )
            )
            if stored_raw is not MISSING:
                stored_unsigned = unsigned_32(stored_raw)
                if stored_unsigned is not None:
                    stored_gameid = shortcut_game_id(stored_unsigned)
                    lines.append(
                        f"gameid gravado 64bits..: {stored_gameid} "
                        f"({_hex_64(stored_gameid)})"
                    )
                    lines.append(
                        "Pasta remote (appid)....: "
                        + _remote_folder_status(
                            steam_path, selected_user_id, stored_unsigned
                        )
                    )
                    lines.append(
                        "Pasta remote (gameid)...: "
                        + _remote_folder_status(
                            steam_path, selected_user_id, stored_gameid
                        )
                    )

    lines.extend(_section(6, "PASTAS NUMÉRICAS EM 760\\REMOTE"))
    lines.append(f"Pasta remote: {remote}")
    if not shortcuts_path.is_file():
        lines.append(
            "Nenhum atalho conhecido (shortcuts.vdf ausente); "
            "todas as pastas são marcadas como sem atalho."
        )
    elif shortcuts_error is not None:
        lines.append(
            "Não foi possível carregar os atalhos; todas as pastas são marcadas "
            "como sem atalho."
        )
    numeric_app_dirs = [entry for entry in app_dirs if entry.name.isdigit()]
    if not numeric_app_dirs:
        lines.append("(nenhuma pasta numérica encontrada)")
    else:
        for entry in numeric_app_dirs:
            folder_value = int(entry.name)
            matches = shortcut_folder_mappings.get(str(folder_value), [])
            if matches:
                details = "; ".join(match.label for match in matches)
                suffix = f" [atalho: {details}]"
            else:
                suffix = " [sem atalho conhecido]"
            lines.append(
                f"{entry.name} -> decimal: {folder_value}, "
                f"hexadecimal: {_hex_folder_value(folder_value)}{suffix}"
            )

    lines.extend(["", SECTION_RULE, "FIM DO RELATÓRIO", SECTION_RULE, ""])
    return "\n".join(lines)


def _ensure_output_outside_steam(output_path: Path, steam_path: Path) -> None:
    try:
        output_path.resolve().relative_to(steam_path.resolve())
    except ValueError:
        return
    raise ValueError(
        f"O arquivo de saída não pode ficar dentro da pasta da Steam: {output_path}"
    )


def build_parser(default_output: Path | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspeciona somente a estrutura de screenshots e atalhos da Steam, "
            "sem alterar nenhum arquivo dentro da pasta da Steam."
        )
    )
    parser.add_argument(
        "user_id",
        nargs="?",
        help="userdata ID (SteamID3). Se omitido, usa o primeiro encontrado.",
    )
    parser.add_argument(
        "--appid",
        help="Limita as amostras e o screenshots.vdf a um appid específico.",
    )
    parser.add_argument(
        "--steam-path",
        type=Path,
        help="Sobrepõe a descoberta automática da pasta da Steam.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output or (Path.cwd() / "inspect_output.txt"),
        help="Arquivo de saída do relatório.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    default_output: Path | None = None,
) -> int:
    parser = build_parser(default_output)
    args = parser.parse_args(argv)

    if args.appid is not None and not args.appid.isdigit():
        parser.error("--appid deve ser um número inteiro")

    try:
        if args.steam_path is not None:
            steam_path = args.steam_path
            if not steam_path.is_dir():
                raise SteamError(f"Pasta da Steam inexistente: {steam_path}")
            installation = SteamInstallation(
                path=steam_path,
                source="argumento --steam-path",
            )
        else:
            installation = find_steam_installation()

        _ensure_output_outside_steam(args.output, installation.path)
        report = generate_report(
            installation,
            user_id=args.user_id,
            appid_filter=args.appid,
        )
        with args.output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(report)
        sys.stdout.write(report)
    except (SteamError, OSError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    return 0
