from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import vdf
from PIL import Image

from steamshotbridge import importer, shortcuts
from steamshotbridge.games import create_custom_game, custom_game_id
from steamshotbridge.importer import (
    ScreenshotImportError,
    import_screenshot,
    load_vdf_document,
    serialize_screenshots_vdf,
)
from steamshotbridge.shortcuts import shortcut_path
from steamshotbridge.steam import load_shortcuts_vdf, signed_32
from tools.import_one import main as cli_main

USERDATA_ID = "12345"
REAL_VDF = Path(
    r"C:\Program Files (x86)\Steam\userdata\259740280\760\screenshots.vdf"
)

BASE_VDF = '''"screenshots"
{
    "shortcutnames"
    {
    }
}
'''


@pytest.fixture(autouse=True)
def _steam_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: False)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: False)


def _setup_userdata(
    tmp_path: Path,
    content: str = BASE_VDF,
    raw: bytes | None = None,
) -> tuple[Path, Path]:
    userdata = tmp_path / "Steam" / "userdata" / USERDATA_ID
    steam_dir = userdata / "760"
    steam_dir.mkdir(parents=True)
    vdf_path = steam_dir / "screenshots.vdf"
    if raw is None:
        vdf_path.write_bytes(content.encode("utf-8"))
    else:
        vdf_path.write_bytes(raw)
    return userdata, vdf_path


def _make_source(
    tmp_path: Path,
    size: tuple[int, int] = (400, 225),
    image_format: str = "JPEG",
) -> Path:
    suffix = ".png" if image_format == "PNG" else ".jpg"
    path = tmp_path / f"source{suffix}"
    Image.new("RGB", size, (20, 40, 80)).save(path, format=image_format)
    return path


def _existing_entry(index: str, creation: str, marker: str) -> str:
    return f'''        "{index}"
        {{
            "type" "1"
            "filename" "{marker}.jpg"
            "thumbnail" "730/screenshots/thumbnails/{marker}.jpg"
            "imported" "1"
            "width" "400"
            "height" "225"
            "gameid" "730"
            "creation" "{creation}"
            "Permissions" "2"
            "hscreenshot" "18446744073709551615"
            "extra" "{marker}"
        }}'''


def _vdf_with_entries(entries: str) -> str:
    return f'''"screenshots"
{{
    "730"
    {{
{entries}
    }}
    "shortcutnames"
    {{
    }}
}}
'''


def _load_root(vdf_path: Path) -> vdf.VDFDict:
    return vdf.loads(
        vdf_path.read_text(encoding="utf-8"),
        mapper=vdf.VDFDict,
        merge_duplicate_keys=False,
    )


def _shortcut_appid(game_id: str) -> int:
    return (int(game_id) >> 32) & 0xFFFFFFFF


def _write_existing_shortcut(
    userdata: Path,
    game_id: str,
    name: str,
) -> Path:
    path = shortcut_path(userdata)
    path.parent.mkdir(parents=True)
    root = vdf.VDFDict()
    shortcuts_block = vdf.VDFDict()
    shortcuts_block["0"] = {
        "appid": signed_32(_shortcut_appid(game_id)),
        "AppName": name,
        "tags": vdf.VDFDict(),
    }
    root["shortcuts"] = shortcuts_block
    path.write_bytes(vdf.binary_dumps(root))
    return path


def test_import_creates_images_entry_backup_and_vdf_snippet(tmp_path: Path) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    original_vdf = vdf_path.read_bytes()
    source = _make_source(tmp_path)
    local_time = datetime(
        2026,
        1,
        2,
        3,
        4,
        5,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    creation = int(local_time.timestamp())
    utc_time = local_time.astimezone(timezone.utc)
    expected_timelineid = f"timeline_730{utc_time:%Y%m%d}_{utc_time:%H%M%S}"

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=local_time,
    )

    assert result.status == "imported"
    assert result.creation == creation
    assert result.image_path.name == "20260102030405_1.jpg"
    assert result.image_path == (
        userdata / "760" / "remote" / "730" / "screenshots"
        / "20260102030405_1.jpg"
    )
    assert result.thumbnail_path == result.image_path.parent / "thumbnails" / result.image_path.name
    assert result.image_path.read_bytes().startswith(b"\xff\xd8")
    with Image.open(result.image_path) as image:
        assert image.size == (400, 225)
        assert image.format == "JPEG"
    with Image.open(result.thumbnail_path) as thumbnail:
        assert thumbnail.size == (200, 112)
        assert thumbnail.format == "JPEG"

    assert result.timelineid == expected_timelineid
    assert result.entry == {
        "type": "1",
        "filename": "730/screenshots/20260102030405_1.jpg",
        "thumbnail": "730/screenshots/thumbnails/20260102030405_1.jpg",
        "imported": "1",
        "timelineid": expected_timelineid,
        "timelinetime": "0",
        "width": "400",
        "height": "225",
        "gameid": "730",
        "creation": str(creation),
        "Permissions": "2",
        "hscreenshot": "18446744073709551615",
    }

    raw_output = vdf_path.read_bytes()
    assert raw_output.startswith(b'"screenshots"')
    assert b"\r\n" not in raw_output
    root = _load_root(vdf_path)
    screenshots = root["screenshots"]
    assert list(screenshots.keys()) == ["730", "shortcutnames"]
    entry = screenshots["730"]["0"]
    assert list(entry.keys()) == [
        "type",
        "filename",
        "thumbnail",
        "imported",
        "timelineid",
        "timelinetime",
        "width",
        "height",
        "gameid",
        "creation",
        "Permissions",
        "hscreenshot",
    ]
    assert dict(entry) == result.entry
    assert dict(screenshots["shortcutnames"]) == {}

    assert result.backup_path is not None
    assert result.backup_path.name.startswith("screenshots.vdf.bak_")
    assert result.backup_path.read_bytes() == original_vdf
    assert result.vdf_snippet.startswith('\t\t"0"\n\t\t{\n')
    assert (
        '\t\t\t"filename"\t\t"730/screenshots/20260102030405_1.jpg"\n'
        in result.vdf_snippet
    )
    assert result.vdf_snippet.endswith("\t\t}\n")


def test_existing_game_block_is_renumbered_without_duplicates(
    tmp_path: Path,
) -> None:
    entries = "\n".join(
        [
            _existing_entry("0", "300", "newer"),
            _existing_entry("1", "100", "older"),
        ]
    )
    userdata, vdf_path = _setup_userdata(tmp_path, _vdf_with_entries(entries))
    source = _make_source(tmp_path)

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=200,
    )

    assert result.vdf_snippet.startswith('\t\t"1"\n')
    root = _load_root(vdf_path)
    screenshots = root["screenshots"]
    assert list(screenshots.keys()).count("730") == 1
    app_block = screenshots["730"]
    assert list(app_block.keys()) == ["0", "1", "2"]
    assert [app_block[index]["creation"] for index in app_block.keys()] == [
        "300",
        "200",
        "100",
    ]
    assert app_block["0"]["filename"] == "newer.jpg"
    assert app_block["0"]["extra"] == "newer"
    assert app_block["2"]["filename"] == "older.jpg"
    assert app_block["2"]["extra"] == "older"
    assert dict(app_block["1"]) == result.entry


def test_non_steam_shortcut_uses_stored_gameid_and_adds_shortcutname(
    tmp_path: Path,
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)
    game_id = "17842732021063876608"

    result = import_screenshot(
        source,
        game_id,
        userdata,
        game_name="Teste importação",
        creation_time=300,
    )

    assert result.image_path == (
        userdata / "760" / "remote" / game_id / "screenshots"
        / result.image_path.name
    )
    root = _load_root(vdf_path)
    screenshots = root["screenshots"]
    assert list(screenshots.keys()) == [game_id, "shortcutnames"]
    assert screenshots[game_id]["0"]["gameid"] == game_id
    assert screenshots["shortcutnames"][game_id] == "Teste importação"


def test_existing_shortcutname_is_not_overwritten(tmp_path: Path) -> None:
    game_id = "17842732021063876608"
    content = f'''"screenshots"
{{
    "shortcutnames"
    {{
        "{game_id}" "Nome Original"
    }}
}}
'''
    userdata, vdf_path = _setup_userdata(tmp_path, content)
    source = _make_source(tmp_path)

    import_screenshot(
        source,
        game_id,
        userdata,
        game_name="Nome Novo",
        creation_time=300,
    )

    root = _load_root(vdf_path)
    assert root["screenshots"]["shortcutnames"][game_id] == "Nome Original"


def test_non_steam_without_name_does_not_add_shortcutname(
    tmp_path: Path,
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)
    game_id = "17842732021063876608"

    import_screenshot(source, game_id, userdata, creation_time=300)

    root = _load_root(vdf_path)
    assert game_id not in root["screenshots"]["shortcutnames"]
    assert not shortcut_path(userdata).exists()


def test_custom_import_creates_shortcut(tmp_path: Path) -> None:
    userdata, _ = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)
    game_id = "17842732021063876608"

    result = import_screenshot(
        source,
        game_id,
        userdata,
        game_name="Jogo Importado",
        creation_time=300,
    )

    assert result.status == "imported"
    assert result.shortcut is not None
    assert result.shortcut.status == "created"
    assert result.shortcut.appid_signed == signed_32(
        _shortcut_appid(game_id)
    )
    entry = load_shortcuts_vdf(shortcut_path(userdata))["shortcuts"]["0"]
    assert entry["AppName"] == "Jogo Importado"
    assert entry["IsHidden"] == 0
    assert entry["appid"] == result.shortcut.appid_signed


def test_custom_import_can_disable_shortcut(tmp_path: Path) -> None:
    userdata, _ = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)

    result = import_screenshot(
        source,
        "17842732021063876608",
        userdata,
        game_name="Jogo Importado",
        creation_time=300,
        create_shortcut=False,
    )

    assert result.status == "imported"
    assert result.shortcut is None
    assert not shortcut_path(userdata).exists()


def test_custom_import_hidden_shortcut(tmp_path: Path) -> None:
    userdata, _ = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)

    result = import_screenshot(
        source,
        "17842732021063876608",
        userdata,
        game_name="Jogo Importado",
        creation_time=300,
        hidden_shortcut=True,
    )

    assert result.shortcut is not None
    entry = load_shortcuts_vdf(shortcut_path(userdata))["shortcuts"]["0"]
    assert entry["IsHidden"] == 1


def test_custom_import_reuses_existing_shortcut(tmp_path: Path) -> None:
    game_id = "17842732021063876608"
    userdata, _ = _setup_userdata(tmp_path)
    path = _write_existing_shortcut(userdata, game_id, "Já Existente")
    original_bytes = path.read_bytes()
    source = _make_source(tmp_path)

    result = import_screenshot(
        source,
        game_id,
        userdata,
        game_name="Jogo Importado",
        creation_time=300,
    )

    assert result.shortcut is not None
    assert result.shortcut.status == "already_exists"
    assert result.shortcut.name == "Já Existente"
    assert path.read_bytes() == original_bytes


def test_steam_running_leaves_screenshot_and_shortcut_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_id = "17842732021063876608"
    userdata, vdf_path = _setup_userdata(tmp_path)
    original_vdf = vdf_path.read_bytes()
    source = _make_source(tmp_path)
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)

    result = import_screenshot(
        source,
        game_id,
        userdata,
        game_name="Jogo Pendente",
        creation_time=300,
    )

    assert result.status == "vdf_pendente"
    assert result.pending is not None
    assert result.shortcut is not None
    assert result.shortcut.status == "pendente"
    assert result.shortcut.pending is not None
    assert result.shortcut.pending.name == "Jogo Pendente"
    assert result.image_path.is_file()
    assert result.thumbnail_path.is_file()
    assert vdf_path.read_bytes() == original_vdf
    assert not shortcut_path(userdata).exists()


def test_reuses_timeline_from_most_recent_screenshot(tmp_path: Path) -> None:
    session_start = datetime(
        2026,
        1,
        1,
        12,
        0,
        0,
        tzinfo=timezone.utc,
    )
    start_timestamp = int(session_start.timestamp())
    creation = start_timestamp + 120
    content = f'''"screenshots"
{{
    "730"
    {{
        "0"
        {{
            "creation" "{start_timestamp}"
            "timelineid" "timeline_73020260101_120000"
            "timelinetime" "0"
        }}
    }}
    "shortcutnames"
    {{
    }}
}}
'''
    userdata, _ = _setup_userdata(tmp_path, content)
    source = _make_source(tmp_path)

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=creation,
        session_gap_minutes=30,
    )

    assert result.timelineid == "timeline_73020260101_120000"
    assert result.entry["timelinetime"] == "120000"


def test_starts_new_timeline_outside_gap(tmp_path: Path) -> None:
    start_timestamp = int(
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    )
    creation = start_timestamp + 31 * 60
    content = f'''"screenshots"
{{
    "730"
    {{
        "0"
        {{
            "creation" "{start_timestamp}"
            "timelineid" "timeline_73020260101_120000"
            "timelinetime" "0"
        }}
    }}
    "shortcutnames"
    {{
    }}
}}
'''
    userdata, _ = _setup_userdata(tmp_path, content)
    source = _make_source(tmp_path)
    moment = datetime.fromtimestamp(creation, tz=timezone.utc)

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=creation,
        session_gap_minutes=30,
    )

    assert result.timelineid == f"timeline_730{moment:%Y%m%d}_{moment:%H%M%S}"
    assert result.entry["timelinetime"] == "0"


def test_no_timeline_omits_both_fields(tmp_path: Path) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=300,
        include_timeline=False,
    )

    assert "timelineid" not in result.entry
    assert "timelinetime" not in result.entry
    root = _load_root(vdf_path)
    entry = root["screenshots"]["730"]["0"]
    assert list(entry.keys()) == [
        "type",
        "filename",
        "thumbnail",
        "imported",
        "width",
        "height",
        "gameid",
        "creation",
        "Permissions",
        "hscreenshot",
    ]
    assert result.timelineid is None


def test_steam_running_returns_pending_and_leaves_vdf_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    original_vdf = vdf_path.read_bytes()
    source = _make_source(tmp_path)
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=400,
    )

    assert result.status == "vdf_pendente"
    assert result.steam_running is True
    assert result.backup_path is None
    assert result.image_path.is_file()
    assert result.thumbnail_path.is_file()
    assert vdf_path.read_bytes() == original_vdf
    assert not list(vdf_path.parent.glob("screenshots.vdf.bak_*"))
    assert result.pending is not None
    assert result.pending.vdf_path == vdf_path
    assert result.pending.game_id == "730"
    assert result.pending.entry == result.entry
    assert result.pending.shortcutname_added is False


def test_force_updates_vdf_while_steam_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=400,
        force=True,
    )

    assert result.status == "imported"
    assert result.steam_running is True
    assert result.backup_path is not None
    root = _load_root(vdf_path)
    assert "730" in root["screenshots"]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    original_vdf = vdf_path.read_bytes()
    source = _make_source(tmp_path)

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=500,
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert not result.image_path.exists()
    assert not result.thumbnail_path.exists()
    assert not (userdata / "760" / "remote").exists()
    assert vdf_path.read_bytes() == original_vdf
    assert not list(vdf_path.parent.glob("screenshots.vdf.bak_*"))
    assert result.backup_path is None
    assert '"filename"' in result.vdf_snippet


def test_filename_collision_uses_next_number(tmp_path: Path) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)
    local_time = datetime(2026, 2, 3, 4, 5, 6)
    screenshots_dir = userdata / "760" / "remote" / "730" / "screenshots"
    thumbnails_dir = screenshots_dir / "thumbnails"
    thumbnails_dir.mkdir(parents=True)
    (screenshots_dir / "20260203040506_1.jpg").write_bytes(b"existing-image")
    (thumbnails_dir / "20260203040506_1.jpg").write_bytes(b"existing-thumb")

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=local_time,
    )

    assert result.image_path.name == "20260203040506_2.jpg"
    assert result.thumbnail_path.name == "20260203040506_2.jpg"
    assert (screenshots_dir / "20260203040506_1.jpg").read_bytes() == b"existing-image"
    assert (thumbnails_dir / "20260203040506_1.jpg").read_bytes() == b"existing-thumb"
    assert _load_root(vdf_path)["screenshots"]["730"]["0"]["filename"].endswith(
        "20260203040506_2.jpg"
    )


def test_png_is_converted_to_jpeg_and_thumbnail_is_truncated(
    tmp_path: Path,
) -> None:
    userdata, _ = _setup_userdata(tmp_path)
    source = _make_source(tmp_path, size=(401, 300), image_format="PNG")

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=600,
    )

    assert result.image_path.read_bytes().startswith(b"\xff\xd8")
    assert result.entry["width"] == "401"
    assert result.entry["height"] == "300"
    with Image.open(result.thumbnail_path) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert thumbnail.size == (200, 149)


def test_crlf_and_bom_are_preserved_and_removed(tmp_path: Path) -> None:
    content = BASE_VDF.replace("\n", "\r\n")
    raw = b"\xef\xbb\xbf" + content.encode("utf-8")
    userdata, vdf_path = _setup_userdata(tmp_path, raw=raw)
    source = _make_source(tmp_path)

    result = import_screenshot(
        source,
        "730",
        userdata,
        creation_time=700,
    )

    output = vdf_path.read_bytes()
    assert not output.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in output
    assert b"\n" not in output.replace(b"\r\n", b"")
    assert result.vdf_snippet.startswith('\t\t"0"\r\n\t\t{\r\n')
    assert _load_root(vdf_path)["screenshots"]["730"]["0"]["creation"] == "700"


@pytest.mark.skipif(not REAL_VDF.is_file(), reason="screenshots.vdf real ausente")
def test_real_screenshots_vdf_round_trips_byte_for_byte() -> None:
    raw_before = REAL_VDF.read_bytes()

    document = load_vdf_document(REAL_VDF)
    rendered = serialize_screenshots_vdf(document.root, document.eol).encode("utf-8")

    if rendered != raw_before:
        raw_lines = raw_before.decode("utf-8-sig").splitlines()
        rendered_lines = rendered.decode("utf-8").splitlines()
        for index in range(max(len(raw_lines), len(rendered_lines))):
            expected = raw_lines[index] if index < len(raw_lines) else "<fim>"
            actual = (
                rendered_lines[index]
                if index < len(rendered_lines)
                else "<fim>"
            )
            if expected != actual:
                pytest.fail(
                    f"Primeira linha divergente ({index + 1}):\n"
                    f"VDF real:    {expected!r}\n"
                    f"serializado: {actual!r}"
                )

    assert rendered == raw_before
    assert REAL_VDF.read_bytes() == raw_before


def test_missing_userdata_raises_clear_error(tmp_path: Path) -> None:
    source = _make_source(tmp_path)

    with pytest.raises(ScreenshotImportError, match="Userdata da Steam inválido"):
        import_screenshot(source, "730", tmp_path / "missing")


def test_cli_dry_run_prints_status_and_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    original_vdf = vdf_path.read_bytes()
    source = _make_source(tmp_path)

    exit_code = cli_main(
        [
            "--image",
            str(source),
            "--game-id",
            "730",
            "--steam-userdata",
            str(userdata),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Status: dry_run" in captured.out
    assert "Trecho VDF:" in captured.out
    assert captured.err == ""
    assert vdf_path.read_bytes() == original_vdf
    assert not (userdata / "760" / "remote").exists()


def test_cli_new_game_name_dry_run_does_not_write_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    original_vdf = vdf_path.read_bytes()
    source = _make_source(tmp_path)
    config = tmp_path / "config.json"
    name = "Jogo Novo"

    exit_code = cli_main(
        [
            "--image",
            str(source),
            "--game-name",
            name,
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Status: dry_run" in captured.out
    assert f"Jogo: {custom_game_id(name)} ({name})" in captured.out
    assert captured.err == ""
    assert "Atalho: dry_run" in captured.out
    assert not config.exists()
    assert vdf_path.read_bytes() == original_vdf
    assert not (userdata / "760" / "remote").exists()
    assert not shortcut_path(userdata).exists()


def test_cli_duplicate_game_name_requires_explicit_game_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    original_vdf = vdf_path.read_bytes()
    source = _make_source(tmp_path)
    config = tmp_path / "config.json"
    first_id = custom_game_id("Primeiro")
    second_id = custom_game_id("Segundo")
    config.write_text(
        json.dumps(
            {
                "custom_games": [
                    {"name": "Duplicado", "game_id": str(first_id)},
                    {"name": "Duplicado", "game_id": str(second_id)},
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "--image",
            str(source),
            "--game-name",
            "Duplicado",
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Mais de um jogo encontrado" in captured.err
    assert "Use --game-id" in captured.err
    assert str(first_id) in captured.err
    assert str(second_id) in captured.err
    assert vdf_path.read_bytes() == original_vdf
    assert not (userdata / "760" / "remote").exists()


def test_cli_finds_existing_custom_game_by_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata, _ = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)
    config = tmp_path / "config.json"
    name = "Jogo Existente"
    game = create_custom_game(name, config)
    config_before = config.read_bytes()

    exit_code = cli_main(
        [
            "--image",
            str(source),
            "--game-name",
            name,
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Jogo: {game.game_id} ({name})" in captured.out
    assert config.read_bytes() == config_before
    assert not (userdata / "760" / "remote").exists()


def test_cli_creates_custom_game_when_name_is_new(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)
    config = tmp_path / "config.json"
    name = "Jogo Criado"
    game_id = custom_game_id(name)

    exit_code = cli_main(
        [
            "--image",
            str(source),
            "--game-name",
            name,
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Status: imported" in captured.out
    assert captured.err == ""
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["custom_games"] == [
        {"name": name, "game_id": str(game_id)}
    ]
    root = _load_root(vdf_path)
    assert root["screenshots"]["shortcutnames"][str(game_id)] == name
    assert root["screenshots"][str(game_id)]["0"]["gameid"] == str(game_id)
    entry = load_shortcuts_vdf(shortcut_path(userdata))["shortcuts"]["0"]
    assert entry["AppName"] == name
    assert entry["appid"] == signed_32((game_id >> 32) & 0xFFFFFFFF)


def test_cli_no_shortcut_skips_shortcut_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata, _ = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)
    config = tmp_path / "config.json"
    name = "Sem Atalho"

    exit_code = cli_main(
        [
            "--image",
            str(source),
            "--game-name",
            name,
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
            "--no-shortcut",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Status: imported" in captured.out
    assert "Atalho:" not in captured.out
    assert not shortcut_path(userdata).exists()


def test_cli_hidden_shortcut_flag_sets_hidden(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata, _ = _setup_userdata(tmp_path)
    source = _make_source(tmp_path)
    config = tmp_path / "config.json"
    name = "Atalho Oculto"

    exit_code = cli_main(
        [
            "--image",
            str(source),
            "--game-name",
            name,
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
            "--hidden-shortcut",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Atalho: created" in captured.out
    entry = load_shortcuts_vdf(shortcut_path(userdata))["shortcuts"]["0"]
    assert entry["AppName"] == name
    assert entry["IsHidden"] == 1
