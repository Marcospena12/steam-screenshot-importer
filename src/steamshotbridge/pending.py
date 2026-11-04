from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import vdf

from .games import default_config_path
from .importer import (
    ImportResult,
    _apply_vdf_entry,
    _atomic_write,
    _build_timeline,
    _create_backup,
    load_vdf_document,
    serialize_screenshots_vdf,
)
from .shortcuts import _next_shortcut_key, serialize_shortcuts_vdf
from .steam import (
    find_mapping_key,
    get_mapping_value,
    is_steam_running,
    load_shortcuts_vdf,
    unsigned_32,
)

MISSING = object()


class PendingError(Exception):
    pass


@dataclass(frozen=True)
class ApplyResult:
    status: str
    applied: int = 0
    shortcuts: int = 0
    screenshots: int = 0


def default_pending_path() -> Path:
    return default_config_path().with_name("pending.json")


def _load_pending_raw(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingError(
            f"Não foi possível ler {path}: {type(exc).__name__}: {exc}"
        ) from exc

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("pending", [])
    else:
        raise PendingError(f"Formato inválido em {path}")

    if not isinstance(entries, list):
        raise PendingError(f"'pending' deve ser uma lista em {path}")
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


def load_pending(
    pending_path: Path | str | None = None,
) -> list[dict[str, object]]:
    path = (
        Path(pending_path)
        if pending_path is not None
        else default_pending_path()
    )
    return _load_pending_raw(path)


def _atomic_write_json(path: Path, data: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def save_pending(
    entries: list[dict[str, object]],
    pending_path: Path | str | None = None,
) -> None:
    path = (
        Path(pending_path)
        if pending_path is not None
        else default_pending_path()
    )
    _atomic_write_json(path, {"pending": entries})


def _shortcut_from_result(result: ImportResult) -> dict[str, object] | None:
    if result.shortcut is None or result.shortcut.pending is None:
        return None
    pending = result.shortcut.pending
    return {
        "path": str(pending.path),
        "shortcut_key": pending.shortcut_key,
        "entry": dict(pending.entry),
        "game_id": str(pending.game_id),
        "appid32": pending.appid32,
        "name": pending.name,
    }


def _record_signature(record: Mapping) -> tuple[object, object, object]:
    entry = record.get("entry")
    filename = entry.get("filename") if isinstance(entry, Mapping) else None
    shortcut = record.get("shortcut")
    shortcut_key = (
        shortcut.get("shortcut_key")
        if isinstance(shortcut, Mapping)
        else None
    )
    return (record.get("game_id"), filename, shortcut_key)


def enqueue_import(
    result: ImportResult,
    pending_path: Path | str | None = None,
    milliseconds: int = 0,
) -> dict[str, object] | None:
    screenshot_pending = result.pending
    shortcut_pending = _shortcut_from_result(result)
    if screenshot_pending is None and shortcut_pending is None:
        return None

    if screenshot_pending is not None:
        game_id = screenshot_pending.game_id
        game_name = screenshot_pending.game_name
        entry: dict[str, object] | None = dict(screenshot_pending.entry)
        vdf_path = str(screenshot_pending.vdf_path)
        shortcutname_added = screenshot_pending.shortcutname_added
    else:
        assert result.shortcut is not None
        assert result.shortcut.pending is not None
        game_id = str(result.shortcut.pending.game_id)
        game_name = result.shortcut.pending.name
        entry = None
        vdf_path = ""
        shortcutname_added = False

    record: dict[str, object] = {
        "game_id": str(game_id),
        "game_name": game_name,
        "entry": entry,
        "vdf_path": vdf_path,
        "shortcutname_added": bool(shortcutname_added),
        "milliseconds": int(milliseconds),
        "shortcut": shortcut_pending,
    }

    path = (
        Path(pending_path)
        if pending_path is not None
        else default_pending_path()
    )
    entries = _load_pending_raw(path)
    signature = _record_signature(record)
    for existing in entries:
        if _record_signature(existing) == signature:
            return existing
    entries.append(record)
    save_pending(entries, path)
    return record


def update_pending_game_name(
    game_id: int | str,
    new_name: str,
    pending_path: Path | str | None = None,
) -> int:
    path = (
        Path(pending_path)
        if pending_path is not None
        else default_pending_path()
    )
    entries = _load_pending_raw(path)
    gid = str(game_id)
    updated = 0
    for record in entries:
        if str(record.get("game_id")) == gid:
            record["game_name"] = new_name
            shortcut = record.get("shortcut")
            if isinstance(shortcut, dict):
                shortcut["name"] = new_name
                entry = shortcut.get("entry")
                if isinstance(entry, dict):
                    entry["AppName"] = new_name
            updated += 1
    if updated > 0:
        save_pending(entries, path)
    return updated


def enqueue_rename(
    game_id: int | str,
    new_name: str,
    userdata_dir: Path | str,
    pending_path: Path | str | None = None,
) -> dict[str, object]:
    path = (
        Path(pending_path)
        if pending_path is not None
        else default_pending_path()
    )
    userdata = Path(userdata_dir)
    gid = str(game_id)
    appid32 = (int(game_id) >> 32) & 0xFFFFFFFF
    record: dict[str, object] = {
        "type": "rename",
        "game_id": gid,
        "game_name": new_name,
        "vdf_path": str(userdata / "760" / "screenshots.vdf"),
        "shortcut": {
            "path": str(userdata / "config" / "shortcuts.vdf"),
            "appid32": appid32,
            "name": new_name,
            "rename": True,
        },
    }
    entries = _load_pending_raw(path)
    entries = [
        e
        for e in entries
        if not (e.get("type") == "rename" and str(e.get("game_id")) == gid)
    ]
    entries.append(record)
    save_pending(entries, path)
    return record


def enqueue_remove(
    game_id: int | str,
    remove_screenshots: bool,
    userdata_dir: Path | str,
    pending_path: Path | str | None = None,
    include_published: bool = False,
    lixeira_dir: Path | str | None = None,
    game_name: str | None = None,
) -> dict[str, object]:
    path = (
        Path(pending_path)
        if pending_path is not None
        else default_pending_path()
    )
    userdata = Path(userdata_dir)
    gid = str(game_id)
    appid32 = (int(game_id) >> 32) & 0xFFFFFFFF
    record: dict[str, object] = {
        "type": "remove",
        "game_id": gid,
        "game_name": game_name,
        "remove_screenshots": bool(remove_screenshots),
        "include_published": bool(include_published),
        "vdf_path": str(userdata / "760" / "screenshots.vdf"),
        "shortcut": {
            "path": str(userdata / "config" / "shortcuts.vdf"),
            "appid32": appid32,
            "remove": True,
        },
    }
    if lixeira_dir is not None:
        record["lixeira_dir"] = str(lixeira_dir)
    entries = _load_pending_raw(path)
    entries = [
        e
        for e in entries
        if not (e.get("type") == "remove" and str(e.get("game_id")) == gid)
    ]
    entries.append(record)
    save_pending(entries, path)
    return record


def _same_path(left: object, right: Path) -> bool:
    if not isinstance(left, str) or not left:
        return True
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(str(right))
    )


def _targets_userdata(record: Mapping, userdata_dir: Path) -> bool:
    entry = record.get("entry")
    if isinstance(entry, Mapping):
        if not _same_path(
            record.get("vdf_path"),
            userdata_dir / "760" / "screenshots.vdf",
        ):
            return False
    shortcut = record.get("shortcut")
    if isinstance(shortcut, Mapping):
        if not _same_path(
            shortcut.get("path"),
            userdata_dir / "config" / "shortcuts.vdf",
        ):
            return False
    return True


def _find_app_block(root: Mapping, game_id: str) -> Mapping | None:
    screenshots = find_mapping_key(root, "screenshots")
    if not isinstance(screenshots, Mapping):
        return None
    for key, value in screenshots.items():
        if str(key) == game_id:
            return value if isinstance(value, Mapping) else None
    return None


def _screenshot_sort_key(record: Mapping) -> tuple[int, int, str]:
    entry = record.get("entry")
    if not isinstance(entry, Mapping):
        return (0, 0, "")
    try:
        creation = int(entry.get("creation", 0))
    except (TypeError, ValueError):
        creation = 0
    try:
        milliseconds = int(record.get("milliseconds", 0))
    except (TypeError, ValueError):
        milliseconds = 0
    return (creation, milliseconds, str(entry.get("filename", "")))


def _prepare_shortcuts(
    userdata_dir: Path,
    records: list[Mapping],
) -> tuple[Path, object, int]:
    path = userdata_dir / "config" / "shortcuts.vdf"
    if path.is_file():
        root = load_shortcuts_vdf(path)
    else:
        root = vdf.VDFDict()

    shortcuts = find_mapping_key(root, "shortcuts")
    if shortcuts is None:
        shortcuts = vdf.VDFDict()
        root["shortcuts"] = shortcuts
    if not isinstance(shortcuts, vdf.VDFDict):
        if not isinstance(shortcuts, Mapping):
            raise PendingError(
                f"O bloco 'shortcuts' de {path} não é um dicionário"
            )
        mutable = vdf.VDFDict()
        for key, value in shortcuts.items():
            mutable[key] = value
        root["shortcuts"] = mutable
        shortcuts = mutable

    added = 0
    for record in records:
        try:
            appid32 = int(record["appid32"]) & 0xFFFFFFFF
        except (KeyError, TypeError, ValueError) as exc:
            raise PendingError(
                f"Atalho pendente inválido: {record!r}"
            ) from exc

        already_exists = False
        target_shortcut = None
        for shortcut in shortcuts.values():
            if not isinstance(shortcut, Mapping):
                continue
            if unsigned_32(get_mapping_value(shortcut, "appid", MISSING)) == appid32:
                already_exists = True
                target_shortcut = shortcut
                break

        if record.get("rename"):
            new_app_name = record.get("name")
            if new_app_name and target_shortcut is not None:
                target_shortcut["AppName"] = str(new_app_name)
                added += 1
            continue

        if record.get("remove"):
            from .removal import remove_shortcut_in_memory
            if remove_shortcut_in_memory(root, appid32):
                shortcuts = find_mapping_key(root, "shortcuts")
                added += 1
            continue

        if already_exists:
            continue

        entry = record.get("entry")
        if not isinstance(entry, Mapping):
            raise PendingError(f"Atalho pendente sem entrada: {record!r}")
        key = _next_shortcut_key(shortcuts)
        shortcuts[key] = entry
        added += 1

    return path, root, added


def _prepare_screenshots(
    userdata_dir: Path,
    records: list[Mapping],
    session_gap_minutes: float,
) -> tuple[Path, bytes | None]:
    path = userdata_dir / "760" / "screenshots.vdf"
    if not path.is_file():
        if any(record.get("type") != "remove" for record in records):
            raise PendingError(f"screenshots.vdf não encontrado: {path}")
        for record in records:
            if not (
                bool(record.get("remove_screenshots"))
                or bool(record.get("include_published"))
            ):
                continue
            from .removal import _move_screenshot_files_to_lixeira, default_lixeira_dir

            lixeira_raw = record.get("lixeira_dir")
            lixeira_dir = (
                Path(str(lixeira_raw))
                if lixeira_raw
                else default_lixeira_dir()
            )
            _move_screenshot_files_to_lixeira(
                userdata_dir,
                record["game_id"],
                lixeira_dir,
                keep_entries=(),
                move_entries=() if not record.get("remove_screenshots") else None,
                move_all_unkept=bool(record.get("remove_screenshots")),
                dry_run=False,
            )
        return path, None

    document = load_vdf_document(path)
    for record in sorted(records, key=_screenshot_sort_key):
        if record.get("type") == "rename":
            game_id = str(record["game_id"])
            game_name = str(record["game_name"])
            screenshots = find_mapping_key(document.root, "screenshots")
            if isinstance(screenshots, Mapping):
                shortcutnames = find_mapping_key(screenshots, "shortcutnames")
                if shortcutnames is None:
                    shortcutnames = vdf.VDFDict()
                    screenshots["shortcutnames"] = shortcutnames
                if isinstance(shortcutnames, Mapping):
                    shortcutnames[game_id] = game_name
            continue

        if record.get("type") == "remove":
            from .removal import (
                _move_screenshot_files_to_lixeira,
                _remove_screenshot_entries,
                _screenshot_mappings,
                default_lixeira_dir,
            )

            gid = record["game_id"]
            remove_screenshots = bool(record.get("remove_screenshots"))
            include_published = bool(record.get("include_published"))
            _, _, _, published = _screenshot_mappings(
                document.root,
                gid,
            )

            if remove_screenshots or include_published:
                keep_entries = [] if include_published else published
                move_entries = (
                    published
                    if (include_published and not remove_screenshots)
                    else None
                )
                lixeira_raw = record.get("lixeira_dir")
                lixeira_dir = (
                    Path(str(lixeira_raw))
                    if lixeira_raw
                    else default_lixeira_dir()
                )
                _move_screenshot_files_to_lixeira(
                    userdata_dir,
                    gid,
                    lixeira_dir,
                    keep_entries=keep_entries,
                    move_entries=move_entries,
                    move_all_unkept=remove_screenshots,
                    dry_run=False,
                )

            _remove_screenshot_entries(
                document.root,
                gid,
                remove_unpublished=remove_screenshots,
                remove_published=include_published,
            )
            continue

        entry_raw = record.get("entry")
        if not isinstance(entry_raw, Mapping):
            raise PendingError(f"Pendência sem entrada: {record!r}")
        entry = dict(entry_raw)

        game_id = str(record.get("game_id") or entry.get("gameid") or "")
        try:
            game_id_value = int(game_id)
        except (TypeError, ValueError) as exc:
            raise PendingError(
                f"game_id inválido na pendência: {game_id!r}"
            ) from exc
        try:
            creation = int(entry.get("creation", 0))
        except (TypeError, ValueError) as exc:
            raise PendingError(
                f"creation inválida na pendência: {entry.get('creation')!r}"
            ) from exc

        app_block = _find_app_block(document.root, game_id)
        if app_block is not None and not isinstance(app_block, Mapping):
            app_block = None
        timelineid, timelinetime = _build_timeline(
            game_id,
            creation,
            app_block,
            session_gap_minutes,
            True,
        )
        entry["timelineid"] = timelineid
        entry["timelinetime"] = timelinetime

        game_name = record.get("game_name")
        _apply_vdf_entry(
            document.root,
            game_id,
            game_id_value,
            entry,
            creation,
            game_name if isinstance(game_name, str) else None,
        )

    content = serialize_screenshots_vdf(document.root, document.eol).encode(
        "utf-8"
    )
    return path, content


def apply_pending(
    steam_userdata_dir: Path | str,
    pending_path: Path | str | None = None,
    steam_running: Callable[[], bool] | None = None,
    session_gap_minutes: float = 30,
) -> ApplyResult:
    userdata_dir = Path(steam_userdata_dir)
    path = (
        Path(pending_path)
        if pending_path is not None
        else default_pending_path()
    )

    entries = _load_pending_raw(path)
    if not entries:
        return ApplyResult(status="vazio")

    running_check = steam_running or is_steam_running
    if running_check():
        return ApplyResult(status="steam_aberta")

    targeted = [
        record for record in entries
        if _targets_userdata(record, userdata_dir)
    ]
    if not targeted:
        return ApplyResult(status="vazio")

    shortcut_records = [
        record["shortcut"]
        for record in targeted
        if isinstance(record.get("shortcut"), Mapping)
    ]
    screenshot_records = [
        record
        for record in targeted
        if isinstance(record.get("entry"), Mapping)
        or record.get("type") in ("rename", "remove")
    ]

    try:
        shortcut_target: Path | None = None
        shortcut_root: object | None = None
        shortcut_added = 0
        if shortcut_records:
            (
                shortcut_target,
                shortcut_root,
                shortcut_added,
            ) = _prepare_shortcuts(userdata_dir, shortcut_records)

        screenshot_target: Path | None = None
        screenshot_content: bytes | None = None
        if screenshot_records:
            screenshot_target, screenshot_content = _prepare_screenshots(
                userdata_dir,
                screenshot_records,
                session_gap_minutes,
            )
    except PendingError:
        raise
    except Exception as exc:
        raise PendingError(
            f"Falha ao preparar pendências: {type(exc).__name__}: {exc}"
        ) from exc

    shortcut_applied = len(shortcut_records)
    if shortcut_target is not None and shortcut_root is not None:
        if shortcut_added:
            shortcut_target.parent.mkdir(parents=True, exist_ok=True)
            if shortcut_target.is_file():
                _create_backup(shortcut_target)
            _atomic_write(
                shortcut_target,
                serialize_shortcuts_vdf(shortcut_root),
            )

    if screenshot_target is not None and screenshot_content is not None:
        _create_backup(screenshot_target)
        _atomic_write(screenshot_target, screenshot_content)

    target_ids = {id(record) for record in targeted}
    remaining = [
        record for record in entries if id(record) not in target_ids
    ]
    save_pending(remaining, path)
    return ApplyResult(
        status="aplicado",
        applied=len(targeted),
        shortcuts=shortcut_applied,
        screenshots=len(screenshot_records),
    )
