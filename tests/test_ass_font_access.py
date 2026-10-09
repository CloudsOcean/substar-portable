from pathlib import Path
from substar_core import ass_api


def test_font_scan_skips_inaccessible_directory(monkeypatch, tmp_path):
    system = tmp_path / "Windows"
    fonts = system / "Fonts"
    fonts.mkdir(parents=True)
    (fonts / "example.ttf").write_bytes(b"font")
    local = tmp_path / "Local"
    monkeypatch.setenv("WINDIR", str(system))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    original = Path.iterdir
    def guarded(path):
        if path == local / "Microsoft/Windows/Fonts":
            raise PermissionError("blocked")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", guarded)
    assert list(ass_api._font_files().values()) == [fonts / "example.ttf"]
