from __future__ import annotations
import os
import socket
import sys
import threading
import time
import urllib.request
import ssl
import platform
from pathlib import Path
import uvicorn

# Set application envs for desktop mode
os.environ["PDF_CUT_LOCAL_MODE"] = "true"
os.environ["PDF_CUT_FEEDBACK_MODE"] = "true"


def application_data_dir() -> Path:
    configured = (os.getenv("PDF_CUT_DATA_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    system = platform.system().lower()
    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / "PDF Kesim Offline"
    if system == "windows":
        return Path(os.getenv("LOCALAPPDATA") or Path.home()) / "PDF Kesim Offline"
    return Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "pdf-kesim-offline"


def bundled_resource_root() -> Path:
    configured = (os.getenv("PDF_CUT_BUNDLE_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    executable_dir = Path(sys.executable).resolve().parent
    resources = executable_dir.parent / "Resources"
    if resources.exists():
        return resources
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent


def configure_offline_environment() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("LANG", "C.UTF-8")
    os.environ.setdefault("LC_ALL", "C.UTF-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")

    data_dir = application_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "web_jobs").mkdir(exist_ok=True)
    (data_dir / "local-jobs").mkdir(exist_ok=True)
    (data_dir / "crop_memory").mkdir(exist_ok=True)
    os.environ["PDF_CUT_DATA_ROOT"] = str(data_dir)
    executable_dir = Path(sys.executable).resolve().parent
    is_app_bundle = (executable_dir.parent / "Resources").exists()
    if getattr(sys, "frozen", False) or is_app_bundle:
        os.environ["PDF_CUT_DESKTOP_EXECUTABLE"] = str(Path(sys.executable).resolve())
    else:
        os.environ.pop("PDF_CUT_DESKTOP_EXECUTABLE", None)

    resource_root = bundled_resource_root()
    os.environ["PDF_CUT_BUNDLE_ROOT"] = str(resource_root)
    system = platform.system().lower()
    platform_dir = "mac" if system == "darwin" else ("win" if system == "windows" else "linux")
    bin_dir = resource_root / "bin" / platform_dir
    if bin_dir.exists():
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    tessdata_dir = resource_root / "tessdata"
    if tessdata_dir.exists():
        os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)
    fontconfig_file = resource_root / "fontconfig" / "fonts.conf"
    if fontconfig_file.exists():
        os.environ["FONTCONFIG_FILE"] = str(fontconfig_file)
    poppler_data = resource_root / "poppler"
    if poppler_data.exists():
        os.environ["POPPLER_DATADIR"] = str(poppler_data)

    runtime_value = (os.getenv("PDF_CUT_RUNTIME_ROOT") or "").strip()
    if runtime_value:
        runtime_root = Path(runtime_value).expanduser().resolve()
        if system == "windows":
            runtime_paths = [runtime_root, runtime_root / "Library" / "bin", runtime_root / "Scripts"]
        else:
            runtime_paths = [runtime_root / "bin"]
            lib_dir = runtime_root / "lib"
            os.environ["LD_LIBRARY_PATH"] = f"{lib_dir}{os.pathsep}{os.environ.get('LD_LIBRARY_PATH', '')}"
        os.environ["PATH"] = os.pathsep.join(
            [*(str(path) for path in runtime_paths if path.exists()), os.environ.get("PATH", "")]
        )
        runtime_tessdata = runtime_root / "share" / "tessdata"
        if runtime_tessdata.exists():
            os.environ["TESSDATA_PREFIX"] = str(runtime_tessdata)
        runtime_poppler = runtime_root / "share" / "poppler"
        if runtime_poppler.exists():
            os.environ["POPPLER_DATADIR"] = str(runtime_poppler)
    os.chdir(data_dir)

class ServerThread(threading.Thread):
    def __init__(self, port: int):
        super().__init__()
        config = uvicorn.Config(
            "web_app:app",
            host="127.0.0.1",
            port=port,
            log_level="warning",
            loop="asyncio"
        )
        self.server = uvicorn.Server(config)
        self.daemon = True

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True


def find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def is_server_ready(port: int) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


def check_for_updates(window):
    try:
        if getattr(sys, "frozen", False):
            app_root = Path(sys._MEIPASS)
        else:
            app_root = Path(__file__).resolve().parent

        version_file = app_root / "VERSION"
        local_version = "2026.04.13.1"
        if version_file.exists():
            local_version = version_file.read_text(encoding="utf-8").strip()

        # Fetch remote version from GitHub
        url = "https://raw.githubusercontent.com/ozanaliarslan/pdf-cut-auto/master/VERSION"
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3.0, context=ctx) as response:
            remote_version = response.read().decode('utf-8').strip()

        if remote_version and remote_version != local_version:
            # Wait for frontend to render initial layout
            time.sleep(3.0)
            js_code = (
                f"console.log('Update check: Local {local_version}, Remote {remote_version}');"
                f"const container = document.getElementById('update-notification-container');"
                f"if (container) {{"
                f"  const updateLink = document.createElement('a');"
                f"  updateLink.href = 'https://pdf.ozanaliarslan.com/offline';"
                f"  updateLink.target = '_blank';"
                f"  updateLink.style.display = 'flex';"
                f"  updateLink.style.alignItems = 'center';"
                f"  updateLink.style.justifyContent = 'center';"
                f"  updateLink.style.width = '100%';"
                f"  updateLink.style.padding = '12px';"
                f"  updateLink.style.marginTop = '10px';"
                f"  updateLink.style.backgroundColor = '#ffc107';"
                f"  updateLink.style.color = '#000';"
                f"  updateLink.style.border = '1px solid #d39e00';"
                f"  updateLink.style.borderRadius = '8px';"
                f"  updateLink.style.fontWeight = '700';"
                f"  updateLink.style.textDecoration = 'none';"
                f"  updateLink.style.fontSize = '0.9rem';"
                f"  updateLink.style.transition = 'all 0.2s ease';"
                f"  updateLink.innerHTML = '✨ Yeni Sürüm Hazır! ({remote_version})';"
                f"  updateLink.onmouseenter = () => updateLink.style.backgroundColor = '#e0a800';"
                f"  updateLink.onmouseleave = () => updateLink.style.backgroundColor = '#ffc107';"
                f"  container.appendChild(updateLink);"
                f"}} else {{"
                f"  const updateDiv = document.createElement('div');"
                f"  updateDiv.style.position = 'fixed';"
                f"  updateDiv.style.top = '16px';"
                f"  updateDiv.style.right = '16px';"
                f"  updateDiv.style.backgroundColor = '#ffc107';"
                f"  updateDiv.style.color = '#000';"
                f"  updateDiv.style.padding = '14px 18px';"
                f"  updateDiv.style.borderRadius = '8px';"
                f"  updateDiv.style.zIndex = '9999';"
                f"  updateDiv.style.boxShadow = '0 10px 15px -3px rgba(0,0,0,0.1), 0 4px 6px -2px rgba(0,0,0,0.05)';"
                f"  updateDiv.style.fontFamily = 'sans-serif';"
                f"  updateDiv.style.fontSize = '14px';"
                f"  updateDiv.style.borderLeft = '4px solid #d39e00';"
                f"  updateDiv.innerHTML = '<strong>Yeni Sürüm Mevcut! ({remote_version})</strong><br><span style=\"font-size: 12px; color: #333;\">Lütfen en son paketi indirip güncelleyin.</span>';"
                f"  document.body.appendChild(updateDiv);"
                f"  setTimeout(() => {{ updateDiv.style.transition = \"opacity 1s\"; updateDiv.style.opacity = \"0\"; setTimeout(() => updateDiv.remove(), 1000); }}, 8000);"
                f"}}"
            )
            window.evaluate_js(js_code)
    except Exception as exc:
        print(f"[UpdateCheck] Update check failed (offline/error): {exc}", flush=True)


def main():
    import webview

    configure_offline_environment()
    
    port = find_free_port()
    
    # Start FastAPI Backend in background thread
    server_thread = ServerThread(port)
    server_thread.start()

    # Wait until FastAPI server is ready
    retries = 30
    while retries > 0 and not is_server_ready(port):
        time.sleep(0.1)
        retries -= 1

    # Create PyWebView window pointing to the local FastAPI app
    window = webview.create_window(
        "PDF Soru Kesim Otomasyonu",
        f"http://127.0.0.1:{port}",
        width=1280,
        height=850,
        min_size=(1024, 768),
        background_color="#ffffff"
    )

    def on_closed():
        print("Desktop app window closed. Exiting server...", flush=True)
        server_thread.stop()
        os._exit(0)

    window.events.closed += on_closed

    # Start update checking when webview loop starts
    update_thread = threading.Thread(target=check_for_updates, args=(window,))
    update_thread.daemon = True

    webview.start(lambda: update_thread.start())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--processor":
        configure_offline_environment()
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from soru_kesim_pdf_only import main as processor_main

        processor_main()
    else:
        main()
