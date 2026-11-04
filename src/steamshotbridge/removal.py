from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
from typing import Callable, Mapping

from .exe_map import (
    default_config_path,
    load_exe_map,
    save_exe_map,
)
from .importer import (
    _atomic_write,
    _create_backup,
    load_vdf_document,
    serialize_screenshots_vdf,
)
from .pending import (
    default_pending_path,
    enqueue_remove,
)
from .shortcuts import (
    ensure_shortcut,
    load_shortcuts_vdf,
    serialize_shortcuts_vdf,
    shortcut_path,
)
from .steam import (
    find_mapping_key,
    get_mapping_value,
    is_steam_running,
    screenshot_files,
    unsigned_32,
    vdf_text,
)

MISSING = object()
MAX_STEAM_APPID = 0xFFFFFFFF
MAX_GAME_ID = 0xFFFFFFFFFFFFFFFF
PUBLISHED_PRESERVED_WARNING = (
    "O jogo continuará listado porque há screenshots publicadas mantidas."
)


class GameRemovalError(Exception):
    pass


@dataclass(frozen=True)
class RemovalInfo:
    game_id: int
    game_name: str
    shortcut_exists: bool
    normal_count: int
    published_count: int
    in_exe_map: bool = False
    in_custom_games: bool = False
    has_screenshot_block: bool = False
    has_shortcutname: bool = False
    folder_exists: bool = False

    @property
    def total_count(self) -> int:
        return self.normal_count + self.published_count

    @property
    def published_only(self) -> bool:
        return self.published_count > 0 and self.normal_count == 0


@dataclass(frozen=True)
class RemoveResult:
    game_id: int
    game_name: str
    status: str  # "removido", "pendente", "dry_run"
    shortcut_removed: bool
    screenshots_moved: int
    published_preserved: int
    lixeira_path: Path | None = None
    published_removed: int = 0
    normal_removed: int = 0
    block_removed: bool = False
    shortcutname_removed: bool = False
    game_still_listed: bool = False
    warning: str | None = None


@dataclass(frozen=True)
class RestoreResult:
    game_id: int
    game_name: str
    status: str  # "restaurado", "pendente"
    shortcut_status: str
    lixeira_folders: list[Path]


@dataclass(frozen=True)
class ScreenshotCleanup:
    unpublished_removed: int = 0
    published_removed: int = 0
    published_preserved: int = 0
    block_removed: bool = False
    shortcutname_removed: bool = False
    remaining_entries: int = 0
    shortcutname_exists: bool = False
    changed: bool = False


def default_lixeira_dir() -> Path:
    return default_config_path().parent / "lixeira"


def is_steam_game_id(game_id: int | str) -> bool:
    try:
        value = int(game_id)
    except (TypeError, ValueError):
        return False
    return 0 < value <= MAX_STEAM_APPID


def can_remove_game(game_id: int | str) -> bool:
    try:
        value = int(game_id)
    except (TypeError, ValueError):
        return False
    return MAX_STEAM_APPID < value <= MAX_GAME_ID


def is_app_managed_game(
    game_id: int | str,
    config_path: Path | str | None = None,
) -> bool:
    del config_path
    return can_remove_game(game_id)


def is_screenshot_published(entry: Mapping) -> bool:
    pub = entry.get("publishedfileid")
    if pub is None:
        return False
    val = str(pub).strip()
    return val != "" and val != "0"


def _custom_game_entries(config_path: Path) -> list[Mapping]:
    if not config_path.is_file():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    if not isinstance(data, Mapping):
        return []
    custom = data.get("custom_games", [])
    if not isinstance(custom, list):
        return []
    return [entry for entry in custom if isinstance(entry, Mapping)]


def _screenshot_mappings(
    root: Mapping,
    game_id: int | str,
) -> tuple[Mapping | None, object | None, list[Mapping], list[Mapping]]:
    gid_str = str(game_id)
    screenshots = find_mapping_key(root, "screenshots")
    if not isinstance(screenshots, Mapping):
        return None, None, [], []

    app_key = next((key for key in screenshots.keys() if str(key) == gid_str), None)
    block = screenshots[app_key] if app_key is not None else None
    entries: list[Mapping] = []
    if isinstance(block, Mapping):
        entries = [entry for entry in block.values() if isinstance(entry, Mapping)]

    published = [entry for entry in entries if is_screenshot_published(entry)]
    unpublished = [entry for entry in entries if not is_screenshot_published(entry)]
    return screenshots, block, unpublished, published


def _shortcutname_value(
    root: Mapping,
    game_id: int | str,
) -> tuple[bool, str | None]:
    gid_str = str(game_id)
    screenshots = find_mapping_key(root, "screenshots")
    if not isinstance(screenshots, Mapping):
        return False, None
    shortcutnames = find_mapping_key(screenshots, "shortcutnames")
    if not isinstance(shortcutnames, Mapping):
        return False, None
    for key, value in shortcutnames.items():
        if str(key) == gid_str:
            return True, vdf_text(value).strip() or None
    return False, None


def _find_shortcut(
    root: Mapping,
    appid32: int,
) -> tuple[bool, str | None]:
    shortcuts = find_mapping_key(root, "shortcuts")
    if not isinstance(shortcuts, Mapping):
        return False, None

    for shortcut in shortcuts.values():
        if not isinstance(shortcut, Mapping):
            continue
        stored = unsigned_32(get_mapping_value(shortcut, "appid", MISSING))
        if stored == appid32:
            name = vdf_text(
                get_mapping_value(shortcut, "appname", "")
            ).strip()
            return True, name or None
    return False, None


def inspect_removal(
    game_id: int | str,
    steam_userdata_dir: Path | str,
    config_path: Path | str | None = None,
) -> RemovalInfo:
    gid = int(game_id)
    cfg_path = Path(config_path) if config_path is not None else default_config_path()
    userdata = Path(steam_userdata_dir)

    game_name: str | None = None
    in_exe_map = False
    try:
        exe_map = load_exe_map(cfg_path)
    except Exception:
        exe_map = {}
    for mapping in exe_map.values():
        if mapping.game_id == gid:
            in_exe_map = True
            if game_name is None:
                game_name = mapping.name

    in_custom_games = False
    for entry in _custom_game_entries(cfg_path):
        try:
            entry_id = int(str(entry.get("game_id")))
        except (TypeError, ValueError):
            continue
        if entry_id == gid:
            in_custom_games = True
            if game_name is None:
                raw_name = entry.get("name")
                if isinstance(raw_name, str) and raw_name.strip():
                    game_name = raw_name.strip()

    appid32 = (gid >> 32) & 0xFFFFFFFF
    shortcut_exists = False
    shortcuts_file = shortcut_path(userdata)
    if shortcuts_file.is_file():
        try:
            root = load_shortcuts_vdf(shortcuts_file)
            shortcut_exists, shortcut_name = _find_shortcut(root, appid32)
            if game_name is None and shortcut_name:
                game_name = shortcut_name
        except Exception:
            pass

    normal_count = 0
    published_count = 0
    block_entry_count = 0
    has_screenshot_block = False
    has_shortcutname = False
    vdf_file = userdata / "760" / "screenshots.vdf"
    if vdf_file.is_file():
        try:
            doc = load_vdf_document(vdf_file)
            _, block, unpublished, published = _screenshot_mappings(doc.root, gid)
            has_screenshot_block = isinstance(block, Mapping)
            normal_count = len(unpublished)
            published_count = len(published)
            block_entry_count = normal_count + published_count
            has_shortcutname, shortcutname = _shortcutname_value(doc.root, gid)
            if game_name is None and shortcutname:
                game_name = shortcutname
        except Exception:
            pass

    remote_app_dir = userdata / "760" / "remote" / str(gid)
    folder_exists = remote_app_dir.is_dir()
    if block_entry_count == 0 and folder_exists:
        normal_count = len(screenshot_files(remote_app_dir))

    return RemovalInfo(
        game_id=gid,
        game_name=game_name or f"AppID {gid}",
        shortcut_exists=shortcut_exists,
        normal_count=normal_count,
        published_count=published_count,
        in_exe_map=in_exe_map,
        in_custom_games=in_custom_games,
        has_screenshot_block=has_screenshot_block,
        has_shortcutname=has_shortcutname,
        folder_exists=folder_exists,
    )


def remove_shortcut_in_memory(
    root: Mapping,
    appid32: int,
) -> bool:
    shortcuts = find_mapping_key(root, "shortcuts")
    if not isinstance(shortcuts, Mapping):
        return False

    surviving: list[Mapping] = []
    found = False

    for s in shortcuts.values():
        if not isinstance(s, Mapping):
            continue
        stored = unsigned_32(get_mapping_value(s, "appid", MISSING))
        if stored == appid32:
            found = True
        else:
            surviving.append(s)

    if not found:
        return False

    shortcuts.clear()
    for idx, s in enumerate(surviving):
        shortcuts[str(idx)] = s

    return True


def _remove_screenshot_entries(
    root: Mapping,
    game_id: int | str,
    remove_unpublished: bool,
    remove_published: bool,
) -> ScreenshotCleanup:
    gid_str = str(game_id)
    screenshots = find_mapping_key(root, "screenshots")
    if not isinstance(screenshots, Mapping):
        return ScreenshotCleanup()

    app_key = next((key for key in screenshots.keys() if str(key) == gid_str), None)
    block = screenshots[app_key] if app_key is not None else None

    changed = False
    block_removed = False
    unpublished_removed = 0
    published_removed = 0
    remaining_entries: list[Mapping] = []

    if isinstance(block, Mapping):
        kept_values: list[object] = []
        for value in block.values():
            if isinstance(value, Mapping):
                published = is_screenshot_published(value)
                if published and remove_published:
                    published_removed += 1
                    changed = True
                    continue
                if not published and remove_unpublished:
                    unpublished_removed += 1
                    changed = True
                    continue
                remaining_entries.append(value)
            kept_values.append(value)

        if not remaining_entries:
            if app_key is not None:
                del screenshots[app_key]
                block_removed = True
                changed = True
        elif changed:
            block.clear()
            for idx, value in enumerate(kept_values):
                block[str(idx)] = value
    elif app_key is not None:
        del screenshots[app_key]
        block_removed = True
        changed = True

    shortcutname_exists = False
    shortcutname_removed = False
    if not remaining_entries:
        shortcutnames = find_mapping_key(screenshots, "shortcutnames")
        if isinstance(shortcutnames, Mapping):
            sn_key = next(
                (key for key in shortcutnames.keys() if str(key) == gid_str),
                None,
            )
            if sn_key is not None:
                shortcutname_exists = True
                del shortcutnames[sn_key]
                shortcutname_removed = True
                changed = True
    else:
        shortcutname_exists, _ = _shortcutname_value(root, game_id)

    published_preserved = sum(
        1 for entry in remaining_entries if is_screenshot_published(entry)
    )
    return ScreenshotCleanup(
        unpublished_removed=unpublished_removed,
        published_removed=published_removed,
        published_preserved=published_preserved,
        block_removed=block_removed,
        shortcutname_removed=shortcutname_removed,
        remaining_entries=len(remaining_entries),
        shortcutname_exists=shortcutname_exists,
        changed=changed,
    )


def remove_screenshots_in_memory(
    root: Mapping,
    game_id: int | str,
    remove_unpublished: bool = True,
    remove_published: bool = False,
) -> tuple[int, int]:
    cleanup = _remove_screenshot_entries(
        root,
        game_id,
        remove_unpublished=remove_unpublished,
        remove_published=remove_published,
    )
    return cleanup.unpublished_removed, cleanup.published_preserved


def _entry_file_names(entries: list[Mapping] | tuple[Mapping, ...]) -> set[str]:
    names: set[str] = set()
    for entry in entries:
        for field in ("filename", "thumbnail"):
            raw = entry.get(field)
            if raw:
                names.add(Path(str(raw)).name.lower())
    return names


def _move_screenshot_files_to_lixeira(
    userdata_dir: Path,
    game_id: int | str,
    lixeira_dir: Path,
    keep_entries: list[Mapping] | tuple[Mapping, ...] = (),
    move_entries: list[Mapping] | tuple[Mapping, ...] | None = None,
    move_all_unkept: bool = False,
    dry_run: bool = False,
) -> tuple[int, Path | None]:
    gid_str = str(game_id)
    remote_app_dir = userdata_dir / "760" / "remote" / gid_str
    if not remote_app_dir.is_dir():
        return 0, None

    keep_names = _entry_file_names(keep_entries)
    move_names = _entry_file_names(move_entries or [])

    files_to_move: list[tuple[Path, Path]] = []
    for file_path in sorted(remote_app_dir.rglob("*")):
        if not file_path.is_file():
            continue
        name = file_path.name.lower()
        if move_all_unkept:
            if name in keep_names:
                continue
        elif move_entries is not None:
            if name not in move_names:
                continue
        else:
            continue
        rel = file_path.relative_to(remote_app_dir)
        files_to_move.append((file_path, rel))

    if dry_run or not files_to_move:
        return len(files_to_move), None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_lixeira = lixeira_dir / f"{gid_str}_{timestamp}"
    counter = 1
    while target_lixeira.exists():
        target_lixeira = lixeira_dir / f"{gid_str}_{timestamp}_{counter}"
        counter += 1
    target_lixeira.mkdir(parents=True, exist_ok=True)

    for src, rel in files_to_move:
        dest = target_lixeira / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))

    return len(files_to_move), target_lixeira


def move_unpublished_files_to_lixeira(
    userdata_dir: Path,
    game_id: int | str,
    lixeira_dir: Path,
    published_entries: list[Mapping] | None = None,
    dry_run: bool = False,
) -> tuple[int, Path | None]:
    return _move_screenshot_files_to_lixeira(
        userdata_dir,
        game_id,
        lixeira_dir,
        keep_entries=published_entries or (),
        move_all_unkept=True,
        dry_run=dry_run,
    )


def _mark_config_removed(game_id: int, config_path: Path) -> None:
    gid = int(game_id)

    try:
        exe_map = load_exe_map(config_path)
    except Exception:
        exe_map = {}
    matching_keys = [key for key, mapping in exe_map.items() if mapping.game_id == gid]
    if matching_keys:
        for key in matching_keys:
            mapping = exe_map[key]
            exe_map[key] = type(mapping)(
                key=mapping.key,
                exe=mapping.exe,
                name=mapping.name,
                game_id=mapping.game_id,
                provisional=mapping.provisional,
                source=mapping.source,
                is_new=False,
                ignored=True,
            )
        save_exe_map(config_path, exe_map)

    if not config_path.is_file():
        return
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    custom_list = data.get("custom_games")
    if not isinstance(custom_list, list):
        return

    filtered = [
        entry
        for entry in custom_list
        if not (
            isinstance(entry, Mapping)
            and str(entry.get("game_id")) == str(gid)
        )
    ]
    if len(filtered) != len(custom_list):
        data["custom_games"] = filtered
        _atomic_write(
            config_path,
            (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )


def _still_listed_by_published(
    cleanup: ScreenshotCleanup,
    include_published: bool,
) -> bool:
    if include_published:
        return False
    if cleanup.published_preserved <= 0:
        return False
    if cleanup.remaining_entries != cleanup.published_preserved:
        return False
    return cleanup.shortcutname_exists


def remove_game(
    game_id: int | str,
    steam_userdata_dir: Path | str,
    remove_screenshots: bool = False,
    config_path: Path | str | None = None,
    pending_path: Path | str | None = None,
    force: bool = False,
    dry_run: bool = False,
    lixeira_dir: Path | str | None = None,
    steam_running_fn: Callable[[], bool] | None = None,
    include_published: bool = False,
) -> RemoveResult:
    gid = int(game_id)
    if not can_remove_game(gid):
        if is_steam_game_id(gid):
            raise GameRemovalError(
                f"Jogo {gid} é da Steam e não pode ser excluído pelo aplicativo."
            )
        raise GameRemovalError(f"game_id inválido para exclusão: {gid}")

    cfg_path = Path(config_path) if config_path is not None else default_config_path()
    pnd_path = Path(pending_path) if pending_path is not None else default_pending_path()
    trash_dir = Path(lixeira_dir) if lixeira_dir is not None else default_lixeira_dir()
    userdata = Path(steam_userdata_dir)

    info = inspect_removal(gid, userdata, cfg_path)
    game_name = info.game_name
    appid32 = (gid >> 32) & 0xFFFFFFFF
    shortcuts_file = shortcut_path(userdata)
    vdf_file = userdata / "760" / "screenshots.vdf"

    if dry_run:
        if vdf_file.is_file():
            try:
                doc = load_vdf_document(vdf_file)
                _, _, unpublished, published = _screenshot_mappings(doc.root, gid)
            except Exception:
                unpublished, published = [], []
        else:
            unpublished, published = [], []

        keep_entries = [] if include_published else published
        move_entries = published if (include_published and not remove_screenshots) else None
        moved = 0
        if remove_screenshots or include_published:
            moved, _ = _move_screenshot_files_to_lixeira(
                userdata,
                gid,
                trash_dir,
                keep_entries=keep_entries,
                move_entries=move_entries,
                move_all_unkept=remove_screenshots,
                dry_run=True,
            )

        published_preserved = 0 if include_published else len(published)
        still_listed = (
            published_preserved > 0
            and (remove_screenshots or len(unpublished) == 0)
            and info.has_shortcutname
        )
        return RemoveResult(
            game_id=gid,
            game_name=game_name,
            status="dry_run",
            shortcut_removed=info.shortcut_exists,
            screenshots_moved=moved,
            published_preserved=published_preserved,
            published_removed=len(published) if include_published else 0,
            normal_removed=len(unpublished) if remove_screenshots else 0,
            game_still_listed=still_listed,
            warning=PUBLISHED_PRESERVED_WARNING if still_listed else None,
        )

    _mark_config_removed(gid, cfg_path)

    running_check = steam_running_fn or is_steam_running
    if running_check() and not force:
        enqueue_remove(
            gid,
            remove_screenshots,
            userdata,
            pnd_path,
            include_published=include_published,
            lixeira_dir=trash_dir,
            game_name=game_name,
        )
        published_preserved = 0 if include_published else info.published_count
        still_listed = (
            published_preserved > 0
            and (remove_screenshots or info.normal_count == 0)
            and info.has_shortcutname
        )
        return RemoveResult(
            game_id=gid,
            game_name=game_name,
            status="pendente",
            shortcut_removed=info.shortcut_exists,
            screenshots_moved=0,
            published_preserved=published_preserved,
            published_removed=info.published_count if include_published else 0,
            normal_removed=info.normal_count if remove_screenshots else 0,
            game_still_listed=still_listed,
            warning=PUBLISHED_PRESERVED_WARNING if still_listed else None,
        )

    shortcut_removed = False
    if shortcuts_file.is_file():
        root = load_shortcuts_vdf(shortcuts_file)
        if remove_shortcut_in_memory(root, appid32):
            _create_backup(shortcuts_file)
            _atomic_write(shortcuts_file, serialize_shortcuts_vdf(root))
            shortcut_removed = True

    screenshots_moved = 0
    trash_path: Path | None = None
    cleanup = ScreenshotCleanup()
    doc = load_vdf_document(vdf_file) if vdf_file.is_file() else None
    published: list[Mapping] = []
    if doc is not None:
        _, _, _, published = _screenshot_mappings(doc.root, gid)

    if remove_screenshots or include_published:
        keep_entries = [] if include_published else published
        move_entries = (
            published if (include_published and not remove_screenshots) else None
        )
        screenshots_moved, trash_path = _move_screenshot_files_to_lixeira(
            userdata,
            gid,
            trash_dir,
            keep_entries=keep_entries,
            move_entries=move_entries,
            move_all_unkept=remove_screenshots,
            dry_run=False,
        )

    if doc is not None:
        cleanup = _remove_screenshot_entries(
            doc.root,
            gid,
            remove_unpublished=remove_screenshots,
            remove_published=include_published,
        )
        if cleanup.changed:
            _create_backup(vdf_file)
            _atomic_write(
                vdf_file,
                serialize_screenshots_vdf(doc.root, doc.eol).encode("utf-8"),
            )

    still_listed = _still_listed_by_published(cleanup, include_published)
    return RemoveResult(
        game_id=gid,
        game_name=game_name,
        status="removido",
        shortcut_removed=shortcut_removed,
        screenshots_moved=screenshots_moved,
        published_preserved=cleanup.published_preserved,
        lixeira_path=trash_path,
        published_removed=cleanup.published_removed,
        normal_removed=cleanup.unpublished_removed,
        block_removed=cleanup.block_removed,
        shortcutname_removed=cleanup.shortcutname_removed,
        game_still_listed=still_listed,
        warning=PUBLISHED_PRESERVED_WARNING if still_listed else None,
    )


def restore_game(
    game_id: int | str,
    steam_userdata_dir: Path | str,
    config_path: Path | str | None = None,
    pending_path: Path | str | None = None,
    force: bool = False,
    lixeira_dir: Path | str | None = None,
) -> RestoreResult:
    gid = int(game_id)
    cfg_path = Path(config_path) if config_path is not None else default_config_path()
    trash_dir = Path(lixeira_dir) if lixeira_dir is not None else default_lixeira_dir()
    userdata = Path(steam_userdata_dir)

    exe_map = load_exe_map(cfg_path)
    matched_key = None
    game_name = f"AppID {gid}"

    for k, m in exe_map.items():
        if m.game_id == gid:
            matched_key = k
            game_name = m.name
            break

    if matched_key is None:
        raise GameRemovalError(f"Jogo {gid} não encontrado no exe_map para restaurar.")

    m = exe_map[matched_key]
    exe_map[matched_key] = type(m)(
        key=m.key,
        exe=m.exe,
        name=m.name,
        game_id=m.game_id,
        provisional=m.provisional,
        source=m.source,
        is_new=False,
        ignored=False,
    )
    save_exe_map(cfg_path, exe_map)

    sc_res = ensure_shortcut(
        userdata,
        gid,
        game_name,
        force=force,
    )

    lixeira_folders: list[Path] = []
    if trash_dir.is_dir():
        prefix = f"{gid}_"
        lixeira_folders = [
            p for p in trash_dir.iterdir()
            if p.is_dir() and p.name.startswith(prefix)
        ]

    status = "pendente" if sc_res.status == "pendente" else "restaurado"
    return RestoreResult(
        game_id=gid,
        game_name=game_name,
        status=status,
        shortcut_status=sc_res.status,
        lixeira_folders=lixeira_folders,
    )
