from __future__ import annotations

from pathlib import Path

import vdf
from PIL import Image

from steamshotbridge.inspection import generate_report, main
from steamshotbridge.steam import (
    SteamInstallation,
    calculate_shortcut_ids,
    signed_32,
)


def _write_jpeg(path: Path, size: tuple[int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (32, 64, 96)).save(path, format="JPEG")


def _build_fake_steam(tmp_path: Path) -> tuple[Path, Path, str, str, int]:
    steam = tmp_path / "Steam"
    user = "12345"
    userdata = steam / "userdata" / user
    remote = userdata / "760" / "remote"
    app = remote / "730"
    screenshots = app / "screenshots"
    screenshots.mkdir(parents=True)

    for number in range(1, 7):
        _write_jpeg(screenshots / f"2026010100000{number}_1.jpg", (400, 225))
    for number in range(1, 3):
        _write_jpeg(
            screenshots / "thumbnails" / f"2026010100000{number}_1.jpg",
            (200, 112),
        )

    unrelated = remote / "999" / "screenshots"
    unrelated.mkdir(parents=True)
    _write_jpeg(unrelated / "20260101000000_1.jpg", (320, 200))

    index_text = '''"screenshots"
{
    "730"
    {
        "0"
        {
            "type" "1"
            "filename" "730/screenshots/20260101000001_1.jpg"
            "thumbnail" "730/screenshots/thumbnails/20260101000001_1.jpg"
            "imported" "1"
            "timelineid" "timeline_730"
            "timelinetime" "100"
            "width" "400"
            "height" "225"
            "gameid" "730"
            "creation" "1767225600"
            "Permissions" "2"
            "hscreenshot" "18446744073709551615"
        }
        "1"
        {
            "type" "1"
            "filename" "730/screenshots/20260101000002_1.jpg"
            "thumbnail" "730/screenshots/thumbnails/20260101000002_1.jpg"
            "imported" "1"
            "timelineid" "timeline_730"
            "timelinetime" "101"
            "width" "400"
            "height" "225"
            "gameid" "730"
            "creation" "1767225601"
            "Permissions" "2"
            "hscreenshot" "18446744073709551615"
        }
        "2"
        {
            "type" "1"
            "filename" "730/screenshots/20260101000003_1.jpg"
            "thumbnail" "730/screenshots/thumbnails/20260101000003_1.jpg"
            "imported" "1"
            "timelineid" "timeline_730"
            "timelinetime" "102"
            "width" "400"
            "height" "225"
            "gameid" "730"
            "creation" "1767225602"
            "Permissions" "2"
            "hscreenshot" "18446744073709551615"
        }
        "3"
        {
            "type" "1"
            "filename" "entry3-hidden.jpg"
            "extra_hidden_field" "should-not-be-shown"
        }
    }
    "shortcutnames"
    {
    }
}
'''
    (userdata / "760" / "screenshots.vdf").write_text(
        index_text,
        encoding="utf-8",
    )

    exe = '"C:\\Games\\My Game\\game.exe"'
    appname = "My Game"
    ids = calculate_shortcut_ids(exe, appname)
    shortcuts_dir = userdata / "config"
    shortcuts_dir.mkdir(parents=True)
    shortcuts_data = {
        "shortcuts": {
            "0": {
                "appname": appname,
                "exe": exe,
                "StartDir": '"C:\\Games\\My Game"',
                "appid": signed_32(ids.appid32),
            }
        }
    }
    (shortcuts_dir / "shortcuts.vdf").write_bytes(vdf.binary_dumps(shortcuts_data))

    shortcut_app = remote / str(ids.gameid64)
    _write_jpeg(shortcut_app / "screenshots" / "shot.jpg", (100, 100))

    return steam, app, exe, appname, ids.gameid64


def test_generate_report_contains_all_requested_details(
    tmp_path: Path,
) -> None:
    steam, _, exe, appname, shortcut_gameid = _build_fake_steam(tmp_path)
    installation = SteamInstallation(path=steam, source="teste")

    report = generate_report(installation, user_id="12345")

    assert "Userdata IDs..: 12345" in report
    assert "730" in report
    assert "6 screenshots" in report
    assert "20260101000001_1.jpg" in report
    assert "original : 400x225" in report
    assert "thumbnail: 200x112" in report
    assert "1 arquivo(s) adicional(is) não exibido(s)" in report

    assert "Apps indexados: 1" in report
    assert "exibindo 3" in report
    assert "Entradas omitidas: 1" in report
    assert "entry3-hidden.jpg" not in report
    assert "extra_hidden_field" not in report
    for field in (
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
    ):
        assert f'"{field}"' in report
    assert '"shortcutnames"' in report
    assert "Bloco shortcutnames: 0 entrada(s)" in report

    crc_input = exe + appname
    ids = calculate_shortcut_ids(exe, appname)
    assert f"String usada no CRC....: <{crc_input}>" in report
    assert f"appid calculado 32bits.: {ids.appid32}" in report
    assert f"gameid calculado 64bits: {ids.gameid64}" in report
    assert f"gameid gravado 64bits..: {ids.gameid64}" in report
    assert f"Pasta remote (gameid)...: " in report
    assert "existe (1 screenshots)" in report

    folder_section = report.split(
        "6. PASTAS NUMÉRICAS EM 760\\REMOTE",
        1,
    )[1]
    assert (
        "730 -> decimal: 730, hexadecimal: 0x000002DA "
        "[sem atalho conhecido]"
    ) in folder_section
    assert (
        "999 -> decimal: 999, hexadecimal: 0x000003E7 "
        "[sem atalho conhecido]"
    ) in folder_section
    assert (
        f"{shortcut_gameid} -> decimal: {shortcut_gameid}, "
        f"hexadecimal: 0x{shortcut_gameid:016X} "
        f'[atalho: atalho VDF [0]: "My Game"; appid gravado {signed_32(ids.appid32)}]'
    ) in folder_section


def test_main_writes_the_exact_stdout_content(
    tmp_path: Path,
    capsys,
) -> None:
    steam, _, _, _, _ = _build_fake_steam(tmp_path)
    output = tmp_path / "inspect_output.txt"

    exit_code = main(
        [
            "12345",
            "--steam-path",
            str(steam),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert output.read_text(encoding="utf-8") == captured.out
    assert "SteamShotBridge" in captured.out


def test_main_refuses_to_write_inside_steam(
    tmp_path: Path,
    capsys,
) -> None:
    steam, _, _, _, _ = _build_fake_steam(tmp_path)
    output = steam / "inspect_output.txt"

    exit_code = main(
        [
            "12345",
            "--steam-path",
            str(steam),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "não pode ficar dentro da pasta da Steam" in captured.err
    assert not output.exists()
