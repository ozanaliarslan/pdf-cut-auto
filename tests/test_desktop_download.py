from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from desktop_app import DesktopDownloadApi


class FakeWindow:
    def __init__(self, destination):
        self.destination = destination

    def create_file_dialog(self, *_args, **_kwargs):
        return self.destination


def test_desktop_download_saves_local_job_response(tmp_path, monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(FileDialog=types.SimpleNamespace(SAVE="save")))
    destination = tmp_path / "results.zip"
    api = DesktopDownloadApi(8123)
    api.window = FakeWindow(str(destination))

    with patch("desktop_app.urllib.request.urlopen", return_value=BytesIO(b"zip data")) as urlopen:
        result = api.save_download("/jobs/abc-123/download/all", "abc.zip")

    assert result["ok"] is True
    assert result["cancelled"] is False
    assert destination.read_bytes() == b"zip data"
    urlopen.assert_called_once_with("http://127.0.0.1:8123/jobs/abc-123/download/all", timeout=120)


def test_desktop_download_rejects_external_url(tmp_path):
    api = DesktopDownloadApi(8123)
    api.window = FakeWindow(str(tmp_path / "bad.txt"))

    result = api.save_download("https://example.com/file", "bad.txt")

    assert result["ok"] is False
    assert not (tmp_path / "bad.txt").exists()


def test_desktop_download_cancel_does_not_request_file(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(FileDialog=types.SimpleNamespace(SAVE="save")))
    api = DesktopDownloadApi(8123)
    api.window = FakeWindow(None)

    with patch("desktop_app.urllib.request.urlopen") as urlopen:
        result = api.save_download("/jobs/abc/download/log", "log.txt")

    assert result == {"ok": True, "cancelled": True}
    urlopen.assert_not_called()
