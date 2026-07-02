from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from starlette.background import BackgroundTask

from archive import ARCHIVE_STATUSES, ArchiveManager
from crop_feedback import CropFeedbackStore, ManualFeedback
from import_feedback_reports import import_feedback_report_paths
from binary_resolver import get_binary_path


def sanitize_filename(filename: str) -> str:
    path = Path(filename)
    stem = path.stem
    suffix = path.suffix
    
    translation_table = str.maketrans(
        "ğĞıİöÖüÜşŞçÇ",
        "gGiIoOuUsScC"
    )
    stem = stem.translate(translation_table)
    stem = unicodedata.normalize('NFKD', stem).encode('ascii', 'ignore').decode('ascii')
    
    stem = re.sub(r'[\s\(\)\[\]\{\}\\\/]+', '_', stem)
    stem = re.sub(r'[^\w\.-]', '', stem)
    stem = re.sub(r'_{2,}', '_', stem)
    stem = stem.strip('_-')
    
    if not stem:
        stem = "document"
        
    return f"{stem}{suffix}"



APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("PDF_CUT_DATA_ROOT") or APP_ROOT).expanduser().resolve()
JOBS_ROOT = DATA_ROOT / "web_jobs"
LOCAL_JOBS_ROOT = DATA_ROOT / "local-jobs"
OFFLINE_RELEASES_ROOT = Path(
    os.getenv("PDF_CUT_OFFLINE_RELEASES_DIR") or (APP_ROOT / "offline_releases")
).expanduser().resolve()
SOURCE_SCRIPT_PATH = APP_ROOT / "soru_kesim_pdf_only.py"
BYTECODE_SCRIPT_PATH = APP_ROOT / "__pycache__" / "soru_kesim_pdf_only.cpython-313.pyc"
SCRIPT_PATH = SOURCE_SCRIPT_PATH if SOURCE_SCRIPT_PATH.exists() else BYTECODE_SCRIPT_PATH
VERSION_FILE = APP_ROOT / "VERSION"
DEPLOY_INFO_FILE = APP_ROOT / "DEPLOY_INFO.json"
JOB_META_NAME = "job.json"
JOB_LOG_NAME = "process_log.txt"
ERROR_MEMORY_FILE = DATA_ROOT / "crop_memory" / "error_memory.json"
ARCHIVE_STATE_FILE = DATA_ROOT / "crop_memory" / "archive_state.json"
MAX_ERROR_EVENTS = 240
MAX_ERROR_SIGNATURES = 180
JOB_TTL_SECONDS = int(os.getenv("PDF_CUT_JOB_TTL_SECONDS", "1800"))
LOCAL_JOB_TTL_SECONDS = 7 * 24 * 60 * 60
ERROR_JOB_TTL_SECONDS = 900
CLEANUP_SWEEP_SECONDS = 15
TOUCH_MIN_INTERVAL_SECONDS = int(os.getenv("PDF_CUT_TOUCH_MIN_INTERVAL_SECONDS", "8"))
LOCAL_MODE = os.getenv("PDF_CUT_LOCAL_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
FEEDBACK_MODE = LOCAL_MODE or os.getenv("PDF_CUT_FEEDBACK_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
FEEDBACK_JOB_TTL_SECONDS = int(os.getenv("PDF_CUT_FEEDBACK_JOB_TTL_SECONDS", "1800"))
ADMIN_USERNAME = os.getenv("PDF_CUT_ADMIN_USERNAME", "ozanaliarslan")
ADMIN_PASSWORD = os.getenv("PDF_CUT_ADMIN_PASSWORD", "112358!")
ADMIN_SECRET = os.getenv("PDF_CUT_ADMIN_SECRET", f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}:pdf-cut-auto")
ADMIN_COOKIE_NAME = "pdf_cut_admin"
_cleanup_thread_started = False
MAX_CONCURRENT_PROCESSORS = max(1, int(os.getenv("PDF_CUT_MAX_CONCURRENT_PROCESSORS", "1")))
PROCESSOR_NICE_LEVEL = max(0, min(19, int(os.getenv("PDF_CUT_PROCESSOR_NICE", "10"))))
_processor_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PROCESSORS)

ALLOWED_FEEDBACK_ISSUES = {
    "bottom_cut",
    "top_cut",
    "left_cut",
    "right_cut",
    "next_question_leak",
    "missing_common_stem",
    "wrong_split",
    "extra_blank",
    "other",
}
BULK_BOUNDS_OPERATIONS = {
    "bottom_expand": {"issue": "bottom_cut", "label": "Alt sınır genişletildi"},
    "bottom_shrink": {"issue": "extra_blank", "label": "Alt sınır daraltıldı"},
    "top_expand": {"issue": "top_cut", "label": "Üst sınır açıldı"},
    "left_expand": {"issue": "left_cut", "label": "Sol sınır açıldı"},
    "right_expand": {"issue": "right_cut", "label": "Sağ sınır açıldı"},
}

app = FastAPI(title="PDF Test Kesim")


def read_app_version() -> str:
    env_version = (os.getenv("PDF_CUT_AUTO_VERSION") or "").strip()
    if env_version:
        return env_version
    try:
        file_version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        file_version = ""
    return file_version or "2026.04.13.1"


def read_deploy_info(app_version: str) -> dict[str, str]:
    default = {
        "version": app_version,
        "deployed_at": "",
        "deployed_at_label": "",
    }
    try:
        payload = json.loads(DEPLOY_INFO_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default

    version = str(payload.get("version") or app_version).strip() or app_version
    deployed_at = str(payload.get("deployed_at") or "").strip()
    deployed_at_label = str(payload.get("deployed_at_label") or "").strip()
    return {
        "version": version,
        "deployed_at": deployed_at,
        "deployed_at_label": deployed_at_label,
    }


def active_job_ttl_seconds() -> int:
    if LOCAL_MODE:
        return LOCAL_JOB_TTL_SECONDS
    if FEEDBACK_MODE:
        return FEEDBACK_JOB_TTL_SECONDS
    return JOB_TTL_SECONDS


def duration_label(seconds: int) -> str:
    if seconds % (24 * 60 * 60) == 0:
        days = seconds // (24 * 60 * 60)
        return f"{days} gün"
    if seconds % (60 * 60) == 0:
        hours = seconds // (60 * 60)
        return f"{hours} saat"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} dakika"
    return f"{seconds} saniye"


APP_VERSION = read_app_version()
DEPLOY_INFO = read_deploy_info(APP_VERSION)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def page_shell(title: str, body: str) -> HTMLResponse:
    footer_text = f"Sürüm {DEPLOY_INFO['version']}"
    if DEPLOY_INFO["deployed_at_label"]:
        footer_text = f"Son güncelleme {DEPLOY_INFO['deployed_at_label']} | Sürüm {DEPLOY_INFO['version']}"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #c0c0c0;
      --bg-strong: #d4d0c8;
      --card: #f1f1f1;
      --ink: #111;
      --muted: #4a4a4a;
      --line: #808080;
      --accent: #000080;
      --accent-2: #0b4ea2;
      --good: #0a6d2a;
      --warn: #8c5b00;
      --danger: #7f0000;
      --shadow: none;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Tahoma, "MS Sans Serif", Arial, sans-serif;
      color: var(--ink);
      background: var(--bg);
      min-height: 100vh;
    }}
    body::before {{
      display: none;
    }}
    .wrap {{
      width: min(1040px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 56px;
    }}
    .hero {{
      margin-bottom: 24px;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 3px;
      border: 2px solid;
      border-color: #fff #808080 #808080 #fff;
      background: #d4d0c8;
      color: var(--ink);
      font-size: 0.88rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 16px 0 10px;
      max-width: 14ch;
      font-size: clamp(2.4rem, 5vw, 4.5rem);
      line-height: 0.96;
      letter-spacing: -0.05em;
      color: var(--ink);
      text-shadow: none;
    }}
    .sub {{
      max-width: 46rem;
      margin: 0;
      color: var(--muted);
      font-size: 1.08rem;
      line-height: 1.65;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.5fr) minmax(280px, 0.9fr);
      gap: 18px;
      align-items: start;
    }}
    .card {{
      background: var(--card);
      border: 2px solid;
      border-color: #fff #808080 #808080 #fff;
      border-radius: 4px;
      padding: 24px;
      box-shadow: var(--shadow);
    }}
    .card h2 {{
      margin: 0 0 10px;
      font-size: 1.4rem;
      letter-spacing: -0.03em;
    }}
    .card p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }}
    form {{
      display: grid;
      gap: 14px;
      margin-top: 18px;
    }}
    .upload {{
      display: grid;
      gap: 8px;
      padding: 18px;
      border-radius: 4px;
      border: 2px solid;
      border-color: #808080 #fff #fff #808080;
      background: #fff;
    }}
    .upload strong {{
      font-size: 1.04rem;
    }}
    .upload span {{
      color: var(--muted);
      font-size: 0.95rem;
    }}
    input[type=file] {{
      width: 100%;
      font: inherit;
      color: var(--ink);
    }}
    .note {{
      padding: 14px 16px;
      border-radius: 4px;
      background: #efefef;
      border: 1px solid #a0a0a0;
      color: var(--ink);
      font-size: 0.95rem;
      line-height: 1.5;
    }}
    button, .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 40px;
      padding: 0 20px;
      border: 2px solid;
      border-color: #fff #808080 #808080 #fff;
      border-radius: 3px;
      font: inherit;
      font-weight: 700;
      text-decoration: none;
      color: var(--ink);
      background: #d4d0c8;
      box-shadow: none;
      cursor: pointer;
      transition: none;
    }}
    button:hover, .btn:hover {{
      background: #e8e8e8;
    }}
    button:active, .btn:active {{
      border-color: #808080 #fff #fff #808080;
    }}
    .btn.secondary {{
      color: #fff;
      background: #0b4ea2;
      border-color: #6f9ad8 #022e68 #022e68 #6f9ad8;
    }}
    .btn.ghost {{
      color: var(--ink);
      background: #ece9d8;
    }}
    .btn[aria-disabled="true"] {{
      pointer-events: none;
      opacity: 0.45;
      box-shadow: none;
    }}
    .stack {{
      display: grid;
      gap: 12px;
    }}
    .steps {{
      display: grid;
      gap: 10px;
      margin-top: 16px;
    }}
    .step {{
      display: grid;
      grid-template-columns: 40px 1fr;
      gap: 12px;
      align-items: start;
      padding: 12px 0;
      border-top: 1px solid rgba(112, 82, 45, 0.14);
    }}
    .step:first-child {{
      border-top: 0;
      padding-top: 0;
    }}
    .step-no {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 40px;
      height: 40px;
      border-radius: 50%;
      background: rgba(23, 20, 17, 0.06);
      font-weight: 700;
    }}
    .step strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 1rem;
    }}
    .step span {{
      color: var(--muted);
      line-height: 1.5;
    }}
    .status {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: center;
      padding: 18px 20px;
      border-radius: 4px;
      background: #e9e9e9;
      border: 2px solid;
      border-color: #808080 #fff #fff #808080;
      margin-bottom: 18px;
    }}
    .progress-card {{
      display: grid;
      gap: 18px;
    }}
    .meter {{
      height: 12px;
      overflow: hidden;
      border-radius: 3px;
      background: #fff;
      border: 1px solid #808080;
    }}
    .meter span {{
      display: block;
      width: 8%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #3a6ea5, #0b4ea2);
      transition: width .35s ease;
    }}
    .processing-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .metric {{
      min-height: 96px;
      padding: 16px;
      border-radius: 4px;
      border: 1px solid #9e9e9e;
      background: #f7f7f7;
    }}
    .metric strong {{
      display: block;
      margin-top: 8px;
      font-size: 1.08rem;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }}
    .log-preview {{
      max-height: 220px;
      overflow: auto;
      white-space: pre-wrap;
      padding: 16px;
      border-radius: 4px;
      background: #fff;
      color: #111;
      border: 1px solid #9e9e9e;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.82rem;
      line-height: 1.5;
    }}
    .hidden {{
      display: none !important;
    }}
    .label {{
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 0.84rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .countdown {{
      font-size: clamp(2rem, 5vw, 3.2rem);
      line-height: 0.95;
      letter-spacing: -0.06em;
      color: var(--good);
    }}
    .countdown.expiring {{
      color: var(--warn);
    }}
    .countdown.expired {{
      color: var(--danger);
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .meta {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.95rem;
      line-height: 1.6;
    }}
    .list {{
      display: grid;
      gap: 12px;
      margin: 18px 0 0;
      padding: 0;
      list-style: none;
    }}
    .row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 14px;
      align-items: center;
      padding: 16px 18px;
      border-radius: 4px;
      border: 1px solid #9e9e9e;
      background: #f7f7f7;
    }}
    .row strong {{
      display: block;
      font-size: 1rem;
      margin-bottom: 4px;
    }}
    .row span {{
      color: var(--muted);
      font-size: 0.92rem;
      word-break: break-word;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 3px;
      font-size: 0.88rem;
      font-weight: 700;
      background: #e3e3e3;
      color: #222;
      border: 1px solid #9e9e9e;
    }}
    .error {{
      white-space: pre-wrap;
      overflow-x: auto;
      padding: 16px;
      border-radius: 4px;
      background: #fff0f0;
      color: #5d0000;
      border: 1px solid #cc9999;
      font-size: 0.92rem;
      line-height: 1.5;
    }}
    .footer {{
      margin-top: 20px;
      color: var(--muted);
      font-size: 0.9rem;
      text-align: center;
    }}
    @media (max-width: 860px) {{
      .layout,
      .status,
      .row,
      .processing-grid {{
        grid-template-columns: 1fr;
      }}
      h1 {{
        max-width: none;
      }}
    }}
  </style>
</head>
<body>
  <main class="wrap">
    {body}
    <div class="footer">{html.escape(footer_text)}</div>
  </main>
  <script>
    let expiresAt = 0;
    const timerNode = document.querySelector("[data-expires]");
    if (timerNode) {{
      expiresAt = Number(timerNode.dataset.expires || "0");
      const noteNode = document.querySelector("[data-expire-note]");
      const downloadNodes = Array.from(document.querySelectorAll("[data-download]"));

      const redirectOnExpire = timerNode.dataset.expireRedirect || "";
      let expireRedirectScheduled = false;

      const setExpired = () => {{
        timerNode.textContent = "SÜRE DOLDU";
        timerNode.classList.remove("expiring");
        timerNode.classList.add("expired");
        if (noteNode) {{
          noteNode.textContent = "Süre doldu. Dosyalar sunucudan siliniyor veya silinmiş durumda.";
        }}
        downloadNodes.forEach((node) => {{
          node.setAttribute("aria-disabled", "true");
        }});
        if (redirectOnExpire && !expireRedirectScheduled) {{
          expireRedirectScheduled = true;
          window.setTimeout(() => {{
            window.location.replace(redirectOnExpire);
          }}, 1400);
        }}
      }};

      const render = () => {{
        const secondsLeft = Math.max(0, Math.ceil(expiresAt - (Date.now() / 1000)));
        if (secondsLeft <= 0) {{
          setExpired();
          return true;
        }}
        const minutes = String(Math.floor(secondsLeft / 60)).padStart(2, "0");
        const seconds = String(secondsLeft % 60).padStart(2, "0");
        timerNode.textContent = `${{minutes}}:${{seconds}}`;
        timerNode.classList.toggle("expiring", secondsLeft <= 60);
        return false;
      }};

      if (!render()) {{
        const intervalId = window.setInterval(() => {{
          if (render()) {{
            window.clearInterval(intervalId);
          }}
        }}, 250);
      }}
    }}

    const autoRedirectNode = document.querySelector("[data-auto-redirect]");
    if (autoRedirectNode) {{
      const redirectUrl = autoRedirectNode.dataset.autoRedirect || "";
      const delay = Number(autoRedirectNode.dataset.autoRedirectDelay || "0");
      if (redirectUrl) {{
        window.setTimeout(() => {{
          window.location.replace(redirectUrl);
        }}, Number.isFinite(delay) && delay >= 0 ? delay : 0);
      }}
    }}

    const autoDeleteNode = document.querySelector("[data-auto-delete-job]");
    if (autoDeleteNode) {{
      const deleteUrl = autoDeleteNode.dataset.autoDeleteJob || "";
      let skipAutoDelete = false;
      let sentAutoDelete = false;

      document.querySelectorAll("[data-preserve-job]").forEach((node) => {{
        node.addEventListener("click", () => {{
          skipAutoDelete = true;
          window.setTimeout(() => {{
            skipAutoDelete = false;
          }}, 1500);
        }});
      }});

      const sendAutoDelete = () => {{
        if (!deleteUrl || skipAutoDelete || sentAutoDelete) return;
        sentAutoDelete = true;
        if (navigator.sendBeacon) {{
          try {{
            if (navigator.sendBeacon(deleteUrl, new Blob([], {{ type: "text/plain" }}))) return;
          }} catch (error) {{}}
        }}
        try {{
          fetch(deleteUrl, {{ method: "POST", keepalive: true, cache: "no-store" }}).catch(() => {{}});
        }} catch (error) {{}}
      }};

      window.addEventListener("pagehide", sendAutoDelete);
      window.addEventListener("beforeunload", sendAutoDelete);
    }}

    const keepAliveNode = document.querySelector("[data-job-keepalive]");
    if (keepAliveNode) {{
      const keepAliveUrl = keepAliveNode.dataset.jobKeepalive || "";
      const keepAliveFallbackUrl = keepAliveNode.dataset.jobKeepaliveFallback || "/";
      let keepAliveStopped = false;

      const keepAliveTick = async () => {{
        if (keepAliveStopped || !keepAliveUrl) return;
        try {{
          const response = await fetch(keepAliveUrl, {{ method: "POST", cache: "no-store", keepalive: true }});
          if (response.status === 410 || response.status === 404) {{
            keepAliveStopped = true;
            window.location.replace(keepAliveFallbackUrl);
            return;
          }}
          const payload = await response.json();
          const nextExpiresAt = Number(payload.expires_at || "0");
          if (nextExpiresAt > 0) {{
            const nextTimerNode = document.querySelector("[data-expires]");
            if (nextTimerNode) {{
              expiresAt = nextExpiresAt;
              nextTimerNode.dataset.expires = String(nextExpiresAt);
            }}
          }}
        }} catch (error) {{}}
        if (!keepAliveStopped) {{
          window.setTimeout(keepAliveTick, 20000);
        }}
      }};

      window.setTimeout(keepAliveTick, 20000);
      window.addEventListener("pagehide", () => {{
        keepAliveStopped = true;
      }});
      window.addEventListener("beforeunload", () => {{
        keepAliveStopped = true;
      }});
    }}

    const progressNode = document.querySelector("[data-job-progress]");
    if (progressNode) {{
      const jobId = progressNode.dataset.jobProgress;
      const statusNode = document.querySelector("[data-progress-status]");
      const testNode = document.querySelector("[data-progress-test]");
      const questionNode = document.querySelector("[data-progress-question]");
      const pageNode = document.querySelector("[data-progress-page]");
      const barNode = document.querySelector("[data-progress-bar]");
      const logNode = document.querySelector("[data-progress-log]");
      const errorNode = document.querySelector("[data-progress-error]");
      const errorTextNode = document.querySelector("[data-progress-error-text]");
      const logLinkNode = document.querySelector("[data-progress-log-link]");

      const setText = (node, value, fallback = "Bekleniyor") => {{
        if (node) node.textContent = value || fallback;
      }};

      const renderProgress = (payload) => {{
        setText(statusNode, payload.message || payload.status);
        setText(testNode, payload.current_test);
        setText(questionNode, payload.current_question);
        setText(pageNode, payload.current_page);
        if (barNode) {{
          const percent = Math.max(6, Math.min(100, Number(payload.progress_percent || 0)));
          barNode.style.width = `${{percent}}%`;
        }}
        if (logNode) {{
          logNode.textContent = payload.log_tail || "";
          logNode.scrollTop = logNode.scrollHeight;
        }}
        if (payload.status === "completed") {{
          window.location.href = `/jobs/${{jobId}}`;
          return true;
        }}
        if (payload.status === "error" || payload.status === "empty" || payload.status === "cancelled") {{
          if (errorNode) errorNode.classList.remove("hidden");
          if (errorTextNode) errorTextNode.textContent = payload.error || "İşlem başarısız oldu.";
          if (logLinkNode && payload.log_download_url) logLinkNode.href = payload.log_download_url;
          return true;
        }}
        return false;
      }};

      const poll = async () => {{
        try {{
          const response = await fetch(`/jobs/${{jobId}}/status`, {{ cache: "no-store" }});
          const payload = await response.json();
          if (renderProgress(payload)) return;
        }} catch (error) {{
          setText(statusNode, "Durum alınamadı, yeniden deneniyor...");
        }}
        window.setTimeout(poll, 900);
      }};

      poll();
    }}
  </script>
</body>
</html>"""
    )


def collect_result_pdfs(out_dir: Path) -> list[Path]:
    return sorted(out_dir.rglob("*.pdf"))


def build_zip(bundle_paths: list[Path], zip_path: Path, base_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for bundle in bundle_paths:
            archive.write(bundle, bundle.relative_to(base_dir))


def clear_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


TEST_START_RE = re.compile(r"Test basliyor:\s*(.+?)\s*\((\d+)\s+soru\)")
QUESTION_PROGRESS_RE = re.compile(r"Soru\s+(\d+)/(\d+):\s*(.+?)\s+soru\s+(\d+)")
PAGE_PROGRESS_RE = re.compile(r"Sayfa\s+(\d+):\s*(\d+)\s+soru kutusu")
TOTAL_DETECTED_RE = re.compile(r"Toplam\s+(\d+)\s+soru tespit edildi")


def processor_command(
    pdf_path: Path,
    out_dir: Path,
    *,
    hide_question_number: bool = False,
    single_question_pdfs: bool = False,
    module_activity_mode: bool = True,
    pages: str | None = None,
) -> list[str]:
    desktop_executable = (os.getenv("PDF_CUT_DESKTOP_EXECUTABLE") or "").strip()
    if desktop_executable:
        cmd = [desktop_executable, "--processor"]
    else:
        if not SCRIPT_PATH.exists():
            raise HTTPException(status_code=500, detail="PDF işleme betiği bulunamadı")
        cmd = [sys.executable, str(SCRIPT_PATH)]
    cmd.extend([
        "--pdf",
        str(pdf_path),
        "--out",
        str(out_dir),
        "--bundle-only",
    ])
    if module_activity_mode:
        cmd.append("--module-activity-mode")
    if single_question_pdfs:
        cmd.append("--single-question-pdfs")
    if hide_question_number:
        cmd.append("--hide-question-number")
    if pages:
        cmd.extend(["--pages", pages])
    return cmd


def processor_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


def health_dependencies() -> dict[str, bool]:
    return {
        "pdftohtml": shutil.which("pdftohtml") is not None,
        "pdftocairo": shutil.which("pdftocairo") is not None,
        "tesseract": shutil.which("tesseract") is not None,
    }


def health_disk() -> dict[str, int | float]:
    usage = shutil.disk_usage(APP_ROOT)
    used = max(0, usage.used)
    total = max(1, usage.total)
    return {
        "total_bytes": int(total),
        "used_bytes": int(used),
        "free_bytes": int(usage.free),
        "used_percent": round((used / total) * 100.0, 2),
    }


def run_processor(
    pdf_path: Path,
    out_dir: Path,
    *,
    hide_question_number: bool = False,
    single_question_pdfs: bool = False,
    module_activity_mode: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        processor_command(
            pdf_path,
            out_dir,
            hide_question_number=hide_question_number,
            single_question_pdfs=single_question_pdfs,
            module_activity_mode=module_activity_mode,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=processor_env(),
    )


def processed_question_count(output: str) -> int | None:
    for line in output.splitlines():
        if "Toplam islenen soru:" not in line:
            continue
        try:
            return int(line.rsplit(":", 1)[1].strip())
        except ValueError:
            return None
    return None


def now_ts() -> int:
    return int(time.time())


def job_meta_path(job_dir: Path) -> Path:
    return job_dir / JOB_META_NAME


def job_log_path(job_dir: Path) -> Path:
    return job_dir / JOB_LOG_NAME


def write_job_meta(job_dir: Path, pdf_name: str) -> dict:
    meta = read_job_meta(job_dir)
    completed_at = now_ts()
    ttl = active_job_ttl_seconds()
    meta.update({
        "pdf_name": pdf_name,
        "status": "completed",
        "message": "İşlem tamamlandı",
        "completed_at": completed_at,
        "expires_at": completed_at + ttl,
        "progress_percent": 100,
        "processor_pid": 0,
    })
    job_meta_path(job_dir).write_text(json.dumps(meta), encoding="utf-8")
    return meta


def create_job_meta(job_dir: Path, pdf_name: str) -> dict:
    created_at = now_ts()
    meta: dict = {
        "pdf_name": pdf_name,
        "created_at": created_at,
        "completed_at": 0,
        "expires_at": 0,
        "status": "queued",
        "message": "PDF yüklendi, işlem başlatılıyor",
        "current_test": "",
        "current_question": "",
        "current_page": "",
        "progress_percent": 4,
        "processor_pid": 0,
        "last_touch_at": 0,
    }
    job_meta_path(job_dir).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def update_job_meta(job_dir: Path, **updates) -> dict:
    meta = read_job_meta(job_dir)
    meta.update(updates)
    job_meta_path(job_dir).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def read_job_meta(job_dir: Path) -> dict:
    meta_file = job_meta_path(job_dir)
    if meta_file.exists():
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            created_at = int(payload.get("created_at") or int(job_dir.stat().st_mtime))
            expires_at = int(payload.get("expires_at") or 0)
            status = str(payload.get("status") or "completed")
            if status == "completed":
                completed_at = int(payload.get("completed_at") or 0)
                if completed_at > 0:
                    expires_at = min(expires_at or completed_at + active_job_ttl_seconds(), completed_at + active_job_ttl_seconds())
            return {
                "pdf_name": str(payload.get("pdf_name") or "PDF"),
                "created_at": created_at,
                "completed_at": int(payload.get("completed_at") or 0),
                "expires_at": expires_at,
                "status": status,
                "message": str(payload.get("message") or ""),
                "current_test": str(payload.get("current_test") or ""),
                "current_question": str(payload.get("current_question") or ""),
                "current_page": str(payload.get("current_page") or ""),
                "progress_percent": int(payload.get("progress_percent") or 0),
                "processor_pid": int(payload.get("processor_pid") or 0),
                "error": str(payload.get("error") or ""),
                "last_touch_at": int(payload.get("last_touch_at") or 0),
                "selected_pages": str(payload.get("selected_pages") or ""),
                "hide_question_number": bool(payload.get("hide_question_number", False)),
                "workflow_mode": str(payload.get("workflow_mode") or "automatic"),
            }
        except (ValueError, TypeError):
            cleanup_job_dir(job_dir)
            raise HTTPException(status_code=404, detail="İş bilgisi bozuldu")

    created_at = int(job_dir.stat().st_mtime)
    return {
        "pdf_name": "PDF",
        "created_at": created_at,
        "completed_at": 0,
        "expires_at": created_at + JOB_TTL_SECONDS,
        "status": "completed",
        "message": "",
        "current_test": "",
        "current_question": "",
        "current_page": "",
        "progress_percent": 100,
        "processor_pid": 0,
        "error": "",
        "last_touch_at": 0,
        "selected_pages": "",
        "hide_question_number": False,
        "workflow_mode": "automatic",
    }


def seconds_left(meta: dict) -> int:
    return max(0, int(meta["expires_at"]) - now_ts())


def is_job_expired(meta: dict) -> bool:
    if str(meta.get("status") or "") in {"queued", "processing"}:
        return False
    expires_at = int(meta.get("expires_at") or 0)
    return expires_at > 0 and seconds_left(meta) <= 0


def cleanup_job_dir(job_dir: Path) -> None:
    shutil.rmtree(job_dir, ignore_errors=True)


def cleanup_intermediate_files(job_dir: Path, out_dir: Path) -> None:
    if FEEDBACK_MODE:
        return
    input_dir = job_dir / "input"
    if input_dir.exists():
        shutil.rmtree(input_dir, ignore_errors=True)
    for json_file in out_dir.rglob("*.json"):
        json_file.unlink(missing_ok=True)


def purge_expired_jobs() -> None:
    ensure_dir(JOBS_ROOT)
    for child in JOBS_ROOT.iterdir():
        if not child.is_dir():
            continue
        try:
            meta = read_job_meta(child)
        except HTTPException:
            continue
        if is_job_expired(meta):
            cleanup_job_dir(child)


def cleanup_worker() -> None:
    while True:
        purge_expired_jobs()
        time.sleep(CLEANUP_SWEEP_SECONDS)


def ensure_cleanup_worker_started() -> None:
    global _cleanup_thread_started
    if _cleanup_thread_started:
        return
    purge_expired_jobs()
    thread = threading.Thread(target=cleanup_worker, name="pdf-cut-auto-cleanup", daemon=True)
    thread.start()
    _cleanup_thread_started = True


def safe_job_dir(job_id: str) -> Path:
    if not job_id.isalnum():
        raise HTTPException(status_code=404, detail="Geçersiz iş kimliği")
    job_dir = (JOBS_ROOT / job_id).resolve()
    ensure_dir(JOBS_ROOT)
    if JOBS_ROOT.resolve() not in job_dir.parents or not job_dir.exists():
        raise HTTPException(status_code=404, detail="İş bulunamadı")
    return job_dir


def get_active_job(job_id: str) -> tuple[Path, dict]:
    job_dir = safe_job_dir(job_id)
    meta = read_job_meta(job_dir)
    if is_job_expired(meta):
        cleanup_job_dir(job_dir)
        raise HTTPException(status_code=410, detail="Bu işlem için bekleme süresi doldu")
    return job_dir, meta


def append_log(job_dir: Path, text: str) -> None:
    with job_log_path(job_dir).open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(text)


def log_tail(job_dir: Path, max_chars: int = 6000) -> str:
    path = job_log_path(job_dir)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def apply_progress_line(job_dir: Path, line: str) -> None:
    clean_line = line.replace("[KESIM]", "").strip()
    if not clean_line:
        return

    total_match = TOTAL_DETECTED_RE.search(clean_line)
    if total_match:
        update_job_meta(
            job_dir,
            status="processing",
            message=f"{total_match.group(1)} soru bulundu, PDF'ler hazırlanıyor",
            progress_percent=35,
        )
        return

    test_match = TEST_START_RE.search(clean_line)
    if test_match:
        update_job_meta(
            job_dir,
            status="processing",
            message="Test PDF'i hazırlanıyor",
            current_test=test_match.group(1),
            current_question=f"0/{test_match.group(2)}",
            progress_percent=45,
        )
        return

    question_match = QUESTION_PROGRESS_RE.search(clean_line)
    if question_match:
        index = int(question_match.group(1))
        total = max(1, int(question_match.group(2)))
        percent = 45 + int((index / total) * 45)
        update_job_meta(
            job_dir,
            status="processing",
            message="Sorular kesiliyor",
            current_test=question_match.group(3),
            current_question=f"{index}/{total} - Soru {question_match.group(4)}",
            progress_percent=min(92, percent),
        )
        return

    page_match = PAGE_PROGRESS_RE.search(clean_line)
    if page_match:
        update_job_meta(
            job_dir,
            status="processing",
            message="Sayfa yapısı okunuyor",
            current_page=f"Sayfa {page_match.group(1)} - {page_match.group(2)} soru kutusu",
            progress_percent=22,
        )
        return

    if "Export basliyor" in clean_line:
        update_job_meta(job_dir, status="processing", message="Kesim dosyaları oluşturuluyor", progress_percent=40)
    elif "Test PDF yazildi" in clean_line:
        update_job_meta(job_dir, status="processing", message="Bir test PDF'i tamamlandı", progress_percent=94)


def run_processor_stream(
    job_dir: Path,
    pdf_path: Path,
    out_dir: Path,
    *,
    hide_question_number: bool,
    single_question_pdfs: bool,
    module_activity_mode: bool,
    label: str,
    pages: str | None = None,
) -> tuple[int, str]:
    clear_dir(out_dir)
    ensure_dir(out_dir)
    append_log(job_dir, f"\n[{label}]\n")
    update_job_meta(job_dir, status="processing", message=f"{label} çalışıyor", progress_percent=10)

    command = processor_command(
        pdf_path,
        out_dir,
        hide_question_number=hide_question_number,
        single_question_pdfs=single_question_pdfs,
        module_activity_mode=module_activity_mode,
        pages=pages,
    )
    nice_binary = shutil.which("nice") if PROCESSOR_NICE_LEVEL > 0 else None
    if nice_binary:
        command = [nice_binary, "-n", str(PROCESSOR_NICE_LEVEL), *command]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=processor_env(),
        bufsize=1,
    )
    update_job_meta(job_dir, processor_pid=int(process.pid))

    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        captured.append(line)
        append_log(job_dir, line)
        apply_progress_line(job_dir, line)

    return_code = process.wait()
    update_job_meta(job_dir, processor_pid=0)
    output = "".join(captured).strip()
    append_log(job_dir, f"\n[{label} exit_code={return_code}]\n")
    return return_code, output


def should_retry_processor_once(output: str) -> bool:
    text = output or ""
    return (
        "FileNotFoundError" in text
        and "No such file or directory" in text
        and "/output/" in text
    )


def auto_retry_limit_for_output(output: str) -> int:
    if not should_retry_processor_once(output):
        return 0
    memory = load_error_memory()
    kinds = memory.get("kinds", {})
    if not isinstance(kinds, dict):
        return 1
    output_missing = kinds.get("output_path_missing", {})
    if isinstance(output_missing, dict) and int(output_missing.get("count", 0)) >= 3:
        return 2
    return 1


def diagnose_processor_output(output: str) -> str:
    text = output or ""
    if should_retry_processor_once(text):
        return (
            "Teşhis: Çıktı klasörü işlem sırasında erişilemez hale geldi.\n"
            "Yapılan: Sistem bunu otomatik tekrar denedi. Hâlâ başarısız olduysa iş klasörü/izinleri kontrol edilmeli."
        )
    if "Toplam 0 soru tespit edildi" in text or "0 soru tespit edildi" in text:
        return (
            "Teşhis: PDF içinde kesilecek soru kutuları çıkarılamadı.\n"
            "Muhtemel neden: sayfa yapısı farklı, metin katmanı bozuk/eksik veya tarama kalitesi düşük."
        )
    if "PdfReadError" in text and ("encrypted" in text.lower() or "password" in text.lower()):
        return (
            "Teşhis: PDF şifreli/korumalı görünüyor.\n"
            "Çözüm: PDF'i şifresiz kopya olarak dışa aktarıp tekrar yükleyin."
        )
    if "tesseract" in text.lower() and ("not found" in text.lower() or "No such file" in text):
        return (
            "Teşhis: OCR bağımlılığı (tesseract) erişilemiyor.\n"
            "Çözüm: Sunucu bağımlılıklarını kontrol edin."
        )
    return ""


def finish_job_error(job_dir: Path, message: str, detail: str, *, status: str = "error") -> None:
    # cleanup_intermediate_files(job_dir, job_dir / "output")
    expires_at = now_ts() + ERROR_JOB_TTL_SECONDS
    meta = read_job_meta(job_dir)
    try:
        record_error_learning(job_dir, meta, message, detail, status)
    except (OSError, ValueError, TypeError) as exc:
        append_log(job_dir, f"\n[ERROR LEARNING]\n{type(exc).__name__}: {exc}\n")
    update_job_meta(
        job_dir,
        status=status,
        message=message,
        error=detail or message,
        expires_at=expires_at,
        progress_percent=100,
        processor_pid=0,
    )


def process_job(
    job_dir: Path,
    pdf_path: Path,
    out_dir: Path,
    *,
    hide_question_number: bool,
    single_question_pdfs: bool,
    pages: str | None = None,
) -> None:
    slot_acquired = _processor_slots.acquire(blocking=False)
    if not slot_acquired:
        update_job_meta(
            job_dir,
            status="queued",
            message="Önceki PDF işlemi tamamlanınca analiz başlayacak",
            progress_percent=5,
            processor_pid=0,
        )
        _processor_slots.acquire()
        slot_acquired = True

    try:
        current_meta = read_job_meta(job_dir)
        if str(current_meta.get("status") or "") == "cancelled":
            return
        return_code, output = run_processor_stream(
            job_dir,
            pdf_path,
            out_dir,
            hide_question_number=hide_question_number,
            single_question_pdfs=single_question_pdfs or FEEDBACK_MODE,
            module_activity_mode=True,
            label="module-activity-mode",
            pages=pages,
        )
        question_count = processed_question_count(output)

        if return_code == 2:
            append_log(job_dir, "\n[web_app] Kalite kontrol kritik hatalar buldu (exit_code=2). Kullanıcı düzenlemesi için işleme devam ediliyor.\n")
            return_code = 0

        if return_code == 0 and question_count == 0:
            fallback_code, fallback_output = run_processor_stream(
                job_dir,
                pdf_path,
                out_dir,
                hide_question_number=hide_question_number,
                single_question_pdfs=single_question_pdfs or FEEDBACK_MODE,
                module_activity_mode=False,
                label="normal mode",
                pages=pages,
            )
            fallback_questions = processed_question_count(fallback_output)
            return_code = fallback_code
            output = fallback_output
            question_count = fallback_questions

        if return_code != 0:
            retry_limit = auto_retry_limit_for_output(output)
            retry_count = 0
            while return_code != 0 and retry_count < retry_limit:
                retry_count += 1
                append_log(
                    job_dir,
                    f"\n[auto-retry]\nÇıktı klasörü hatası algılandı, işlem yeniden deneniyor ({retry_count}/{retry_limit}).\n",
                )
                retry_code, retry_output = run_processor_stream(
                    job_dir,
                    pdf_path,
                    out_dir,
                    hide_question_number=hide_question_number,
                    single_question_pdfs=single_question_pdfs or FEEDBACK_MODE,
                    module_activity_mode=True,
                    label=f"auto-retry-{retry_count}",
                    pages=pages,
                )
                output = retry_output or output
                return_code = retry_code

        if return_code != 0:
            current_meta = read_job_meta(job_dir)
            if str(current_meta.get("status") or "") == "cancelled":
                return
            diagnosis = diagnose_processor_output(output)
            detail = output or "Bilinmeyen hata"
            if diagnosis:
                detail = f"{diagnosis}\n\n[Teknik detay]\n{detail}"
            finish_job_error(job_dir, "PDF işlenemedi", detail)
            return

        cleanup_intermediate_files(job_dir, out_dir)
        result_pdfs = collect_result_pdfs(out_dir)
        if not result_pdfs:
            finish_job_error(
                job_dir,
                "PDF üretilemedi",
                output or "İşlem tamamlandı ama indirilebilir PDF üretilmedi.",
                status="empty",
            )
            return

        meta = read_job_meta(job_dir)
        write_job_meta(job_dir, str(meta["pdf_name"]))
    except Exception as exc:
        append_log(job_dir, f"\n[WEB ERROR]\n{type(exc).__name__}: {exc}\n")
        finish_job_error(job_dir, "Beklenmeyen hata", f"{type(exc).__name__}: {exc}")
    finally:
        if slot_acquired:
            _processor_slots.release()


def expired_page() -> HTMLResponse:
    retention = duration_label(active_job_ttl_seconds())
    body = f"""
    <section class="hero" data-auto-redirect="/" data-auto-redirect-delay="1400">
      <span class="eyebrow">İşlem kapandı</span>
      <h1>Dosyalar silindi.</h1>
    </section>
    <section class="card">
      <p class="meta">Bekleme süresi dolduğu için iş kapatıldı. Ana sayfaya yönlendiriliyorsunuz…</p>
      <div class="actions">
        <a class="btn" href="/">Yeni PDF yükle</a>
      </div>
    </section>
    """
    return page_shell("Süre doldu", body)


def progress_page(job_id: str, pdf_name: str) -> HTMLResponse:
    log_href = f"/jobs/{quote(job_id)}/download/log"
    body = f"""
    <section class="hero">
      <span class="eyebrow">İşlem sürüyor</span>
      <h1>PDF kesiliyor.</h1>
    </section>
    <section class="card progress-card" data-job-progress="{html.escape(job_id)}">
      <div>
        <p class="label">Durum</p>
        <h2 data-progress-status>PDF yüklendi, işlem başlatılıyor</h2>
        <div class="meter" aria-hidden="true"><span data-progress-bar></span></div>
      </div>
      <form action="/jobs/{quote(job_id)}/cancel" method="post" style="margin:0 0 12px 0;">
        <button class="btn ghost" type="submit">İşlemi iptal et</button>
      </form>
      <div class="processing-grid">
        <div class="metric">
          <p class="label">Test</p>
          <strong data-progress-test>Bekleniyor</strong>
        </div>
        <div class="metric">
          <p class="label">Soru</p>
          <strong data-progress-question>Bekleniyor</strong>
        </div>
        <div class="metric">
          <p class="label">Sayfa</p>
          <strong data-progress-page>Bekleniyor</strong>
        </div>
      </div>
      <div>
        <p class="label">Canlı log</p>
        <div class="log-preview" data-progress-log>Log bekleniyor...</div>
      </div>
      <div class="hidden" data-progress-error>
        <div class="note" data-progress-error-text>İşlem başarısız oldu.</div>
        <div class="actions" style="margin-top:14px;">
          <a class="btn secondary" data-progress-log-link href="{log_href}">Hata logunu indir</a>
          <a class="btn ghost" href="/">Yeni PDF dene</a>
        </div>
      </div>
    </section>
    """
    return page_shell("İşleniyor", body)


def job_error_page(job_id: str, meta: dict) -> HTMLResponse:
    log_href = f"/jobs/{quote(job_id)}/download/log"
    status = str(meta.get("status") or "")
    if status == "cancelled":
        title = "İşlem iptal edildi."
    elif status == "error":
        title = "PDF işlenemedi."
    else:
        title = "PDF üretilemedi."
    body = f"""
    <section class="hero">
      <span class="eyebrow">İşlem başarısız</span>
      <h1>{html.escape(title)}</h1>
    </section>
    <section class="card">
      <div class="error">{html.escape(str(meta.get("error") or "Bilinmeyen hata"))}</div>
      <div class="actions" style="margin-top:16px;">
        <a class="btn secondary" href="{log_href}">Hata logunu indir</a>
        <a class="btn ghost" href="/">Yeni PDF dene</a>
      </div>
    </section>
    """
    return page_shell("Hata", body)


def result_page(job_id: str, pdf_name: str, result_pdfs: list[Path], out_dir: Path, expires_at: int) -> HTMLResponse:
    if not result_pdfs:
        body = f"""
        <section class="hero">
          <span class="eyebrow">Sonuç yok</span>
          <h1>PDF üretilemedi.</h1>
        </section>
        <section class="card">
          <div class="actions">
            <a class="btn secondary" href="/">Yeni PDF yükle</a>
          </div>
        </section>
        """
        return page_shell("Sonuç yok", body)

    zip_link = f"/jobs/{quote(job_id)}/download/all"
    delete_action = f"/jobs/{quote(job_id)}/delete"
    retention = duration_label(active_job_ttl_seconds())
    review_action = ""
    easy_editor_action = ""
    retention_note = f"Dosyalar {retention} boyunca indirilebilir kalır. Sonrasında otomatik temizlenir."
    if FEEDBACK_MODE:
        easy_editor_action = f'<a class="btn" style="background-color:#10b981; color:white;" data-preserve-job href="/jobs/{quote(job_id)}/easy-editor">Kolay Düzenleyici</a>'
        retention_note = f"Dosyalar {retention} boyunca indirilebilir kalır."
    quality_summary = result_quality_summary_html(out_dir)
    test_pdf_items: list[str] = []
    question_pdf_groups: dict[str, list[str]] = {}
    for result_pdf in result_pdfs:
        rel = result_pdf.relative_to(out_dir).as_posix()
        href = f"/jobs/{quote(job_id)}/download/file?path={quote(rel)}"
        item_html = f"""
        <li class="row">
          <div>
            <strong>{html.escape(result_pdf.stem)}</strong>
            <span>{html.escape(rel)}</span>
          </div>
          <div class="actions">
            <a class="btn secondary" data-download data-preserve-job href="{href}">PDF indir</a>
          </div>
        </li>
        """
        if "_questions/" in rel:
            group_name = Path(rel).parent.name
            if group_name.endswith("_questions"):
                group_name = group_name[: -len("_questions")]
            question_pdf_groups.setdefault(group_name or "Sorular", []).append(item_html)
        else:
            test_pdf_items.append(item_html)

    question_sections: list[str] = []
    for group_name, group_items in sorted(question_pdf_groups.items()):
        question_sections.append(
            f"""
            <details class="result-accordion">
              <summary>
                <span>{html.escape(group_name)}</span>
                <small>{len(group_items)} soru PDF</small>
              </summary>
              <ul class="list">
                {''.join(group_items)}
              </ul>
            </details>
            """
        )

    body = f"""
    <style>
      .quality-summary {{
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 10px;
        margin: 16px 0 8px;
      }}
      .quality-summary .metric {{
        min-height: 78px;
        border-radius: 8px;
      }}
      .quality-summary .metric strong {{
        font-size: 1.55rem;
      }}
      .quality-critical {{
        border-color: rgba(142, 47, 29, 0.32);
        background: rgba(142, 47, 29, 0.06);
      }}
      .quality-critical strong {{
        color: var(--danger);
      }}
      .quality-warning {{
        border-color: rgba(154, 90, 16, 0.32);
        background: rgba(154, 90, 16, 0.07);
      }}
      .quality-warning strong {{
        color: var(--warn);
      }}
      .quality-clean {{
        border-color: rgba(36, 97, 59, 0.28);
        background: rgba(36, 97, 59, 0.06);
      }}
      .quality-clean strong {{
        color: var(--good);
      }}
      .quality-autofix {{
        border-color: rgba(36, 86, 122, 0.28);
        background: rgba(36, 86, 122, 0.06);
      }}
      .quality-autofix strong {{
        color: var(--accent-2);
      }}
      .quality-learning {{
        border-color: rgba(201, 101, 50, 0.28);
        background: rgba(201, 101, 50, 0.07);
      }}
      .quality-learning strong {{
        color: var(--accent);
      }}
      .quality-memory {{
        border-color: rgba(93, 65, 169, 0.28);
        background: rgba(93, 65, 169, 0.07);
      }}
      .quality-memory strong {{
        color: #5d41a9;
      }}
      .quality-note {{
        margin-top: 0;
      }}
      .result-accordion {{
        border: 1px solid var(--line);
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.74);
        overflow: hidden;
      }}
      .result-accordion + .result-accordion {{
        margin-top: 12px;
      }}
      .result-accordion > summary {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        min-height: 56px;
        padding: 0 16px;
        cursor: pointer;
        font-weight: 700;
        list-style: none;
      }}
      .result-accordion > summary::-webkit-details-marker {{
        display: none;
      }}
      .result-accordion > summary::after {{
        content: "+";
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 auto;
        width: 28px;
        height: 28px;
        border-radius: 999px;
        background: rgba(36, 86, 122, 0.08);
        color: var(--accent-2);
      }}
      .result-accordion[open] > summary::after {{
        content: "-";
      }}
      .result-accordion > summary small {{
        margin-left: auto;
        color: var(--muted);
        white-space: nowrap;
      }}
      .result-accordion > .list {{
        margin: 0;
        padding: 0 12px 12px;
      }}
      @media (max-width: 1100px) {{
        .quality-summary {{
          grid-template-columns: repeat(3, minmax(0, 1fr));
        }}
      }}
      @media (max-width: 760px) {{
        .quality-summary {{
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }}
      }}
      @media (max-width: 520px) {{
        .quality-summary {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
    <section class="hero">
      <span class="eyebrow">İşlem tamamlandı</span>
      <h1>PDF'ler hazır.</h1>
    </section>
    <section class="card" data-auto-delete-job="{delete_action}">
      <div class="status">
        <div>
          <p class="label">Silinmesine kalan süre</p>
          <div class="countdown" data-expires="{expires_at}" data-expire-redirect="/">{html.escape(retention)}</div>
          <p class="meta" data-expire-note>Dosyalar {html.escape(retention)} boyunca indirilebilir kalır. Sonrasında otomatik temizlenir.</p>
        </div>
        <div class="actions">
          <a class="btn" data-download data-preserve-job href="{zip_link}">Tümünü ZIP indir</a>
          {easy_editor_action}
          {review_action}
          <form action="{delete_action}" method="post" style="margin:0; display:inline-flex;">
            <button class="btn ghost" type="submit">Yeni iş başlat</button>
          </form>
        </div>
      </div>
      <div class="pill">{len(result_pdfs)} adet PDF hazır</div>
      {quality_summary}
      <p class="meta">{html.escape(retention_note)}</p>
      <h2 style="margin:14px 0 8px;">Test PDF'leri</h2>
      {f'<ul class="list">{"".join(test_pdf_items)}</ul>' if test_pdf_items else '<div class="note">Test PDF bulunamadı.</div>'}
      <h2 style="margin:16px 0 8px;">Soru PDF'leri</h2>
      {''.join(question_sections) if question_sections else '<div class="note">Soru PDF bulunamadı.</div>'}
    </section>
    """
    return page_shell("Hazır", body)


def processor_error_page(title: str, subtitle: str, detail: str) -> HTMLResponse:
    body = f"""
    <section class="hero">
      <span class="eyebrow">İşlem başarısız</span>
      <h1>{html.escape(title)}</h1>
      <p class="sub">{html.escape(subtitle)}</p>
    </section>
    <section class="card">
      <div class="error">{html.escape(detail or 'Bilinmeyen hata')}</div>
      <div class="actions" style="margin-top:16px;">
        <a class="btn secondary" href="/">Yeni PDF dene</a>
      </div>
    </section>
    """
    return page_shell("Hata", body)


def admin_token() -> str:
    raw = f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}:{ADMIN_SECRET}".encode("utf-8")
    return hmac.new(ADMIN_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def admin_logged_in(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE_NAME, "")
    return bool(token) and hmac.compare_digest(token, admin_token())


def require_admin(request: Request) -> None:
    if not admin_logged_in(request):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


def local_feedback_dirs() -> list[Path]:
    if not LOCAL_JOBS_ROOT.exists():
        return []
    return sorted(path for path in LOCAL_JOBS_ROOT.glob("*/feedback/*") if path.is_dir())


def feedback_dir_created_at(path: Path) -> int:
    prefix = path.name.split("_", 1)[0].strip()
    if prefix.isdigit():
        return int(prefix)
    report_path = path / "report.json"
    report = read_json_file(report_path) if report_path.exists() else {}
    if isinstance(report, dict):
        return int(report.get("created_at") or 0)
    return 0


def parse_admin_date_filters(start_date: str | None, end_date: str | None) -> dict[str, str | int | None]:
    start_value = (start_date or "").strip()
    end_value = (end_date or "").strip()
    start_ts: int | None = None
    end_ts: int | None = None
    error = ""
    if start_value:
        try:
            start_dt = datetime.strptime(start_value, "%Y-%m-%d")
            start_ts = int(start_dt.timestamp())
        except ValueError:
            error = "Başlangıç tarihi geçersiz."
    if end_value and not error:
        try:
            end_dt = datetime.strptime(end_value, "%Y-%m-%d") + timedelta(days=1) - timedelta(seconds=1)
            end_ts = int(end_dt.timestamp())
        except ValueError:
            error = "Bitiş tarihi geçersiz."
    if start_ts is not None and end_ts is not None and start_ts > end_ts and not error:
        error = "Başlangıç tarihi bitiş tarihinden büyük olamaz."
    return {
        "start_date": start_value,
        "end_date": end_value,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "error": error,
    }


def filter_feedback_dirs(feedback_dirs: list[Path], start_ts: int | None, end_ts: int | None) -> list[Path]:
    filtered: list[Path] = []
    for path in feedback_dirs:
        created_at = feedback_dir_created_at(path)
        if start_ts is not None and created_at < start_ts:
            continue
        if end_ts is not None and created_at > end_ts:
            continue
        filtered.append(path)
    return filtered


def admin_filter_query(start_date: str, end_date: str) -> str:
    parts: list[str] = []
    if start_date:
        parts.append(f"start_date={quote(start_date)}")
    if end_date:
        parts.append(f"end_date={quote(end_date)}")
    return "&".join(parts)


def admin_archive_redirect_url(start_date: str, end_date: str, message: str = "") -> str:
    parts: list[str] = []
    filter_query = admin_filter_query(start_date, end_date)
    if filter_query:
        parts.append(filter_query)
    if message:
        parts.append(f"message={quote(message)}")
    return f"/admin/archive?{'&'.join(parts)}" if parts else "/admin/archive"


def manual_learning_summary() -> dict[str, int]:
    store = CropFeedbackStore()
    profiles = store.data.get("profiles", {})
    if not isinstance(profiles, dict):
        return {
            "profiles": 0,
            "events": 0,
            "manual_bounds": 0,
            "manual_common_stems": 0,
        }
    event_count = 0
    bounds_count = 0
    stem_count = 0
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        events = profile.get("events", [])
        question_bounds = profile.get("question_bounds", {})
        question_common_stems = profile.get("question_common_stems", {})
        if isinstance(events, list):
            event_count += len(events)
        if isinstance(question_bounds, dict):
            bounds_count += len(question_bounds)
        if isinstance(question_common_stems, dict):
            stem_count += len(question_common_stems)
    return {
        "profiles": len(profiles),
        "events": event_count,
        "manual_bounds": bounds_count,
        "manual_common_stems": stem_count,
    }


def remove_feedback_dir_and_empty_parents(path: Path) -> bool:
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    feedback_parent = path.parent
    job_dir = feedback_parent.parent
    if feedback_parent.exists():
        try:
            if not any(feedback_parent.iterdir()):
                feedback_parent.rmdir()
        except OSError:
            pass
    if job_dir.exists():
        try:
            remaining = [child for child in job_dir.iterdir() if child.name != ".DS_Store"]
            if not remaining:
                job_dir.rmdir()
        except OSError:
            pass
    return True


def archive_manager() -> ArchiveManager:
    return ArchiveManager(LOCAL_JOBS_ROOT, ARCHIVE_STATE_FILE)


def dir_size(path: Path) -> int:
    total = 0
    for file_path in path.rglob("*"):
        if file_path.is_file():
            total += file_path.stat().st_size
    return total


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{size} B"


def format_ts(ts: int) -> str:
    if ts <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def error_excerpt(text: str, limit: int = 700) -> str:
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "…"


def classify_processor_error(detail: str) -> dict[str, str]:
    text = detail or ""
    lowered = text.lower()
    exception_match = re.search(r"\b([A-Za-z_]*Error)\b", text)
    exception_name = exception_match.group(1) if exception_match else "RuntimeError"

    if "filenotfounderror" in lowered and "/output/" in lowered:
        return {
            "kind": "output_path_missing",
            "label": "Çıktı klasörü erişim hatası",
            "suggestion": "İşlem sırasında output klasörü kayboldu. Sistem bir kez otomatik tekrar dener; devam ederse dosya sistemi/izin kontrolü yapın.",
            "fingerprint": "FileNotFoundError:/output/",
        }
    if "toplam 0 soru tespit edildi" in lowered or "0 soru tespit edildi" in lowered:
        return {
            "kind": "no_question_detected",
            "label": "Soru kutusu tespit edilemedi",
            "suggestion": "PDF yapısı bu şablona uymuyor olabilir. Daha temiz PDF, farklı baskı veya OCR destekli sürüm deneyin.",
            "fingerprint": "0-question-detected",
        }
    if "pdfreaderror" in lowered and ("encrypted" in lowered or "password" in lowered or "şifre" in lowered):
        return {
            "kind": "encrypted_pdf",
            "label": "Şifreli/Korumalı PDF",
            "suggestion": "PDF'i şifresiz olarak dışa aktarın ve tekrar yükleyin.",
            "fingerprint": "PdfReadError:encrypted",
        }
    if "no such file or directory" in lowered and any(tool in lowered for tool in ("pdftohtml", "pdftocairo", "tesseract")):
        return {
            "kind": "missing_dependency",
            "label": "Eksik sistem bağımlılığı",
            "suggestion": "Sunucu bağımlılıkları eksik. /health çıktısını kontrol edip eksik aracı kurun.",
            "fingerprint": "missing-system-tool",
        }
    if "xml.etree.elementtree.parseerror" in lowered:
        return {
            "kind": "pdf_parse_error",
            "label": "PDF parse hatası",
            "suggestion": "PDF metin katmanı bozuk olabilir. Yeniden dışa aktarılmış sürümü deneyin.",
            "fingerprint": "ElementTree.ParseError",
        }
    return {
        "kind": "unknown_processor_error",
        "label": "Bilinmeyen işlem hatası",
        "suggestion": "Teknik detayı inceleyip aynı hata tekrar ediyorsa bu imza üzerinden iyileştirme yapılmalı.",
        "fingerprint": exception_name,
    }


def load_error_memory() -> dict[str, object]:
    if not ERROR_MEMORY_FILE.exists():
        return {
            "version": 1,
            "updated_at": 0,
            "total_events": 0,
            "kinds": {},
            "signatures": {},
            "events": [],
        }
    try:
        payload = json.loads(ERROR_MEMORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {
            "version": 1,
            "updated_at": 0,
            "total_events": 0,
            "kinds": {},
            "signatures": {},
            "events": [],
        }
    if not isinstance(payload, dict):
        return {
            "version": 1,
            "updated_at": 0,
            "total_events": 0,
            "kinds": {},
            "signatures": {},
            "events": [],
        }
    payload.setdefault("version", 1)
    payload.setdefault("updated_at", 0)
    payload.setdefault("total_events", 0)
    payload.setdefault("kinds", {})
    payload.setdefault("signatures", {})
    payload.setdefault("events", [])
    return payload


def save_error_memory(memory: dict[str, object]) -> None:
    ensure_dir(ERROR_MEMORY_FILE.parent)
    ERROR_MEMORY_FILE.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_error_memory(memory: dict[str, object]) -> None:
    events = memory.get("events", [])
    if isinstance(events, list) and len(events) > MAX_ERROR_EVENTS:
        del events[:-MAX_ERROR_EVENTS]

    signatures = memory.get("signatures", {})
    if not isinstance(signatures, dict):
        return
    if len(signatures) <= MAX_ERROR_SIGNATURES:
        return
    ranked = sorted(
        signatures.items(),
        key=lambda item: (
            int(item[1].get("count", 0)) if isinstance(item[1], dict) else 0,
            int(item[1].get("last_seen", 0)) if isinstance(item[1], dict) else 0,
        ),
        reverse=True,
    )
    keep = {key for key, _value in ranked[:MAX_ERROR_SIGNATURES]}
    for key in list(signatures):
        if key not in keep:
            signatures.pop(key, None)


def record_error_learning(job_dir: Path, meta: dict, message: str, detail: str, status: str) -> None:
    if status not in {"error", "empty"}:
        return
    error_info = classify_processor_error(detail)
    now = now_ts()
    memory = load_error_memory()
    kinds = memory.get("kinds", {})
    signatures = memory.get("signatures", {})
    events = memory.get("events", [])
    if not isinstance(kinds, dict) or not isinstance(signatures, dict) or not isinstance(events, list):
        return

    kind = str(error_info["kind"])
    fingerprint = str(error_info["fingerprint"])
    signature_key = f"{kind}|{fingerprint}"
    kind_row = kinds.setdefault(
        kind,
        {
            "label": str(error_info["label"]),
            "suggestion": str(error_info["suggestion"]),
            "count": 0,
            "last_seen": 0,
        },
    )
    if isinstance(kind_row, dict):
        kind_row["label"] = str(error_info["label"])
        kind_row["suggestion"] = str(error_info["suggestion"])
        kind_row["count"] = int(kind_row.get("count", 0)) + 1
        kind_row["last_seen"] = now

    signature_row = signatures.setdefault(
        signature_key,
        {
            "kind": kind,
            "label": str(error_info["label"]),
            "fingerprint": fingerprint,
            "count": 0,
            "first_seen": now,
            "last_seen": now,
            "suggestion": str(error_info["suggestion"]),
            "last_job_id": job_dir.name,
            "last_pdf_name": str(meta.get("pdf_name") or "PDF"),
            "last_detail_excerpt": "",
        },
    )
    if isinstance(signature_row, dict):
        signature_row["count"] = int(signature_row.get("count", 0)) + 1
        signature_row["last_seen"] = now
        signature_row["last_job_id"] = job_dir.name
        signature_row["last_pdf_name"] = str(meta.get("pdf_name") or "PDF")
        signature_row["last_detail_excerpt"] = error_excerpt(detail)

    events.append(
        {
            "at": now,
            "job_id": job_dir.name,
            "pdf_name": str(meta.get("pdf_name") or "PDF"),
            "status": status,
            "message": message,
            "kind": kind,
            "label": str(error_info["label"]),
            "fingerprint": fingerprint,
            "suggestion": str(error_info["suggestion"]),
            "detail_excerpt": error_excerpt(detail),
        }
    )
    memory["updated_at"] = now
    memory["total_events"] = int(memory.get("total_events", 0)) + 1
    prune_error_memory(memory)
    save_error_memory(memory)


def load_job_history(limit: int = 30) -> list[dict[str, str | int | float]]:
    if not JOBS_ROOT.exists():
        return []
    feedback_store = CropFeedbackStore()
    rows: list[dict[str, str | int | float]] = []
    job_dirs = sorted(
        (path for path in JOBS_ROOT.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for job_dir in job_dirs:
        try:
            meta = read_job_meta(job_dir)
        except HTTPException:
            continue
        out_dir = job_dir / "output"
        quality = result_quality_summary(out_dir) if out_dir.exists() else None
        profiles = load_learning_profiles(out_dir) if out_dir.exists() else {}
        profile_key = ""
        feedback_count = 0
        if profiles:
            first_profile = next(iter(profiles.values()))
            profile_key = str(first_profile.get("profile_key") or "")
        if profile_key:
            feedback_profile = feedback_store.get_profile(profile_key) or {}
            events = feedback_profile.get("events", [])
            if isinstance(events, list):
                feedback_count = len(events)
        total = int(quality["total"]) if quality else 0
        clean = int(quality["clean"]) if quality else 0
        success_rate = round((clean / total) * 100.0, 1) if total > 0 else 0.0
        rows.append(
            {
                "job_id": job_dir.name,
                "pdf_name": str(meta.get("pdf_name") or "PDF"),
                "status": str(meta.get("status") or "completed"),
                "created_at": int(meta.get("created_at") or 0),
                "completed_at": int(meta.get("completed_at") or 0),
                "profile_key": profile_key,
                "success_rate": success_rate,
                "feedback_count": feedback_count,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def build_feedback_zip(
    zip_path: Path,
    feedback_dirs: list[Path] | None = None,
    *,
    include_error_memory: bool = True,
) -> int:
    ensure_dir(LOCAL_JOBS_ROOT)
    count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if feedback_dirs is None:
            for file_path in sorted(LOCAL_JOBS_ROOT.rglob("*")):
                if not file_path.is_file() or file_path.name == ".DS_Store":
                    continue
                archive.write(file_path, file_path.relative_to(LOCAL_JOBS_ROOT))
                count += 1
        else:
            for feedback_dir in sorted(feedback_dirs):
                if not feedback_dir.exists():
                    continue
                for file_path in sorted(feedback_dir.rglob("*")):
                    if not file_path.is_file() or file_path.name == ".DS_Store":
                        continue
                    archive.write(file_path, file_path.relative_to(LOCAL_JOBS_ROOT))
                    count += 1
        if include_error_memory and ERROR_MEMORY_FILE.exists():
            archive.write(ERROR_MEMORY_FILE, Path("error_reports") / ERROR_MEMORY_FILE.name)
            count += 1
    return count


def admin_login_page(error: str = "") -> HTMLResponse:
    error_html = f'<div class="note">{html.escape(error)}</div>' if error else ""
    body = f"""
    <section class="hero">
      <span class="eyebrow">Admin</span>
      <h1>Hata arşivi girişi.</h1>
    </section>
    <section class="card">
      {error_html}
      <form action="/admin/login" method="post">
        <label class="upload">
          <strong>Kullanıcı adı</strong>
          <input name="username" autocomplete="username" required>
        </label>
        <label class="upload">
          <strong>Şifre</strong>
          <input name="password" type="password" autocomplete="current-password" required>
        </label>
        <button type="submit">Giriş yap</button>
      </form>
    </section>
    """
    return page_shell("Admin Giriş", body)


def admin_page(
    request: Request,
    *,
    deleted: bool = False,
    deleted_count: int | None = None,
    learned_events: int | None = None,
    learned_bounds: int | None = None,
    learned_common_stems: int | None = None,
    learned_deleted_count: int | None = None,
    start_date: str = "",
    end_date: str = "",
    filter_error: str = "",
) -> HTMLResponse:
    require_admin(request)
    all_feedback_dirs = sorted(local_feedback_dirs(), key=feedback_dir_created_at)
    filters = parse_admin_date_filters(start_date, end_date)
    if not filter_error and filters["error"]:
        filter_error = str(filters["error"])
    filter_start = str(filters["start_date"] or "")
    filter_end = str(filters["end_date"] or "")
    has_filter = bool(filter_start or filter_end)
    feedback_dirs = (
        all_feedback_dirs
        if filter_error
        else filter_feedback_dirs(all_feedback_dirs, filters["start_ts"], filters["end_ts"])
    )
    total_size = sum(dir_size(path) for path in feedback_dirs)
    error_memory = load_error_memory()
    learning_summary = manual_learning_summary()
    rows = []
    for path in feedback_dirs[-60:]:
        report_path = path / "report.json"
        report = read_json_file(report_path) if report_path.exists() else {}
        if not isinstance(report, dict):
            report = {}
        rel = path.relative_to(LOCAL_JOBS_ROOT).as_posix()
        rows.append(
            f"""
            <li class="row">
              <div>
                <strong>{html.escape(rel)}</strong>
                <span>{html.escape(format_ts(feedback_dir_created_at(path)))} | Test {html.escape(str(report.get('test_no', '-')))} | Soru {html.escape(str(report.get('soru_no', '-')))} | {html.escape(str(report.get('issue_code', '-')))}</span>
              </div>
              <span class="pill">{human_size(dir_size(path))}</span>
            </li>
            """
        )
    deleted_note = ""
    if deleted_count is not None:
        deleted_note = f'<div class="note">Filtreye göre {deleted_count} hata kaydı silindi.</div>'
    elif deleted:
        deleted_note = '<div class="note">Hata arşivi temizlendi.</div>'
    learned_note = ""
    if (
        learned_events is not None
        or learned_bounds is not None
        or learned_common_stems is not None
        or learned_deleted_count is not None
    ):
        learned_note = (
            f'<div class="note">Öğrenme tamamlandı: '
            f'hata kaydı {int(learned_events or 0)}, '
            f'manuel sınır {int(learned_bounds or 0)}, '
            f'ortak kök {int(learned_common_stems or 0)}. '
            f'Arşivden silinen kayıt: {int(learned_deleted_count or 0)}.</div>'
        )
    filter_error_note = f'<div class="note">{html.escape(filter_error)}</div>' if filter_error else ""
    filter_result_note = ""
    if has_filter and not filter_error:
        filter_result_note = (
            f'<div class="note">Filtre sonucu: {len(feedback_dirs)} / {len(all_feedback_dirs)} kayıt gösteriliyor.</div>'
        )
    empty_note = ""
    if not feedback_dirs:
        empty_note = (
            '<div class="note">Seçilen tarih aralığı için kayıt bulunamadı.</div>'
            if has_filter
            else '<div class="note">Henüz kayıtlı hata raporu yok.</div>'
        )
    filter_query = admin_filter_query(filter_start, filter_end)
    download_action = f"/admin/download?{filter_query}" if filter_query else "/admin/download"
    delete_hidden_filters = ""
    if filter_start:
        delete_hidden_filters += f'<input type="hidden" name="start_date" value="{html.escape(filter_start)}">'
    if filter_end:
        delete_hidden_filters += f'<input type="hidden" name="end_date" value="{html.escape(filter_end)}">'
    kind_rows: list[tuple[str, dict[str, object]]] = []
    raw_kinds = error_memory.get("kinds", {})
    if isinstance(raw_kinds, dict):
        for key, value in raw_kinds.items():
            if isinstance(value, dict):
                kind_rows.append((str(key), value))
    kind_rows.sort(key=lambda item: int(item[1].get("count", 0)), reverse=True)
    kind_items = []
    for _kind, info in kind_rows[:8]:
        kind_items.append(
            f"""
            <li class="row">
              <div>
                <strong>{html.escape(str(info.get('label') or 'Hata'))}</strong>
                <span>{html.escape(str(info.get('suggestion') or '-'))}</span>
              </div>
              <span class="pill">{int(info.get("count", 0))} kez</span>
            </li>
            """
        )
    recent_events = error_memory.get("events", [])
    recent_items = []
    if isinstance(recent_events, list):
        for event in reversed(recent_events[-20:]):
            if not isinstance(event, dict):
                continue
            recent_items.append(
                f"""
                <li class="row">
                  <div>
                    <strong>{html.escape(str(event.get('pdf_name') or 'PDF'))}</strong>
                    <span>{html.escape(str(event.get('label') or '-'))} | İş: {html.escape(str(event.get('job_id') or '-'))} | {html.escape(format_ts(int(event.get('at') or 0)))}</span>
                    <span>{html.escape(str(event.get('detail_excerpt') or '-'))}</span>
                  </div>
                </li>
                """
            )
    kind_empty = '<div class="note">Henüz öğrenilmiş hata paterni yok.</div>' if not kind_items else ""
    recent_empty = '<div class="note">Henüz hata olayı kaydı yok.</div>' if not recent_items else ""
    body = f"""
    <section class="hero">
      <span class="eyebrow">Admin</span>
      <h1>Hata arşivi.</h1>
    </section>
    <section class="card">
      {deleted_note}
      {learned_note}
      {filter_error_note}
      {filter_result_note}
      {empty_note}
      <form action="/admin" method="get" style="margin-bottom:16px;">
        <div class="actions" style="margin-bottom:0;">
          <label class="upload" style="min-width:200px; margin:0;">
            <strong>Başlangıç tarihi</strong>
            <input type="date" name="start_date" value="{html.escape(filter_start)}">
          </label>
          <label class="upload" style="min-width:200px; margin:0;">
            <strong>Bitiş tarihi</strong>
            <input type="date" name="end_date" value="{html.escape(filter_end)}">
          </label>
          <button type="submit">Filtrele</button>
          <a class="btn ghost" href="/admin">Temizle</a>
          <span class="pill">Kayıt: {len(feedback_dirs)}</span>
        </div>
      </form>
      <div class="actions" style="margin-bottom:16px;">
        <a class="btn secondary" href="{download_action}">ZIP indir</a>
        <a class="btn secondary" href="{admin_archive_redirect_url(filter_start, filter_end)}">Archive paneli</a>
        <form action="/admin/delete" method="post" style="margin:0;">
          {delete_hidden_filters}
          <button type="submit">Arşivi sil</button>
        </form>
        <form action="/admin/logout" method="post" style="margin:0;">
          <button class="btn ghost" type="submit">Çıkış</button>
        </form>
      </div>
      <ul class="list">
        {''.join(rows)}
      </ul>
    </section>
    <section class="card" style="margin-top:16px;">
      <h2>İşlem hata özeti</h2>
      <p class="meta">Toplam olay: {int(error_memory.get("total_events", 0))} | Son güncelleme: {format_ts(int(error_memory.get("updated_at", 0)))}</p>
      {kind_empty}
      <ul class="list">
        {''.join(kind_items)}
      </ul>
    </section>
    <section class="card" style="margin-top:16px;">
      <h2>Manuel öğrenme özeti</h2>
      <p class="meta">Profil: {learning_summary["profiles"]} | Hata kaydı: {learning_summary["events"]} | Manuel sınır: {learning_summary["manual_bounds"]} | Ortak kök: {learning_summary["manual_common_stems"]}</p>
      <p class="meta">Not: Buradaki öğrenme, arşivdeki report.json kayıtlarının manual_feedback.json içine işlenmesiyle oluşur; işlem hatası özetinden ayrıdır.</p>
    </section>
    <section class="card" style="margin-top:16px;">
      <h2>Son hata kayıtları</h2>
      {recent_empty}
      <ul class="list">
        {''.join(recent_items)}
      </ul>
    </section>
    """
    return page_shell("Admin", body)


def admin_archive_page(
    request: Request,
    *,
    start_date: str = "",
    end_date: str = "",
    message: str = "",
    filter_error: str = "",
) -> HTMLResponse:
    require_admin(request)
    all_feedback_dirs = sorted(local_feedback_dirs(), key=feedback_dir_created_at)
    filters = parse_admin_date_filters(start_date, end_date)
    if not filter_error and filters["error"]:
        filter_error = str(filters["error"])
    filter_start = str(filters["start_date"] or "")
    filter_end = str(filters["end_date"] or "")
    has_filter = bool(filter_start or filter_end)
    feedback_dirs = (
        all_feedback_dirs
        if filter_error
        else filter_feedback_dirs(all_feedback_dirs, filters["start_ts"], filters["end_ts"])
    )

    manager = archive_manager()
    all_rel_paths = {path.relative_to(LOCAL_JOBS_ROOT).as_posix() for path in all_feedback_dirs}
    manager.prune_missing(all_rel_paths)
    records = manager.list_records(feedback_dirs)
    counts = manager.counts_by_status(records)
    total_records = len(all_feedback_dirs)

    info_note = f'<div class="note">{html.escape(message)}</div>' if message else ""
    filter_error_note = f'<div class="note">{html.escape(filter_error)}</div>' if filter_error else ""
    summary_note = ""
    if has_filter and not filter_error:
        summary_note = (
            f'<div class="note">Filtre sonucu: {len(records)} kayıt gösteriliyor / {total_records} toplam kayıt.</div>'
        )
    elif not filter_error:
        summary_note = f'<div class="note">Toplam {total_records} kayıt var.</div>'
    empty_note = (
        '<div class="note">Seçilen tarih aralığında kayıt yok.</div>'
        if has_filter and not records
        else ('<div class="note">Henüz kayıtlı hata raporu yok.</div>' if not records else "")
    )
    hidden_filters = ""
    if filter_start:
        hidden_filters += f'<input type="hidden" name="start_date" value="{html.escape(filter_start)}">'
    if filter_end:
        hidden_filters += f'<input type="hidden" name="end_date" value="{html.escape(filter_end)}">'

    status_options = [
        ("new", "Yeni"),
        ("in_review", "İncelemede"),
        ("learned", "Öğrenildi"),
    ]
    rows = []
    for record in records[:120]:
        option_html = "".join(
            (
                f'<option value="{value}"{" selected" if record.status == value else ""}>{label}</option>'
                if value in ARCHIVE_STATUSES
                else ""
            )
            for value, label in status_options
        )
        
        preview_html = ""
        if record.false_cut or record.true_cut:
            preview_html = '<div class="archive-previews" style="display:flex; gap:16px; margin-top:12px; margin-bottom:12px; flex-wrap:wrap;">'
            is_easy = record.kind == "easy_editor"
            img_style = "max-height: 400px;" if is_easy else "max-height: 200px;"
            if record.false_cut:
                false_url = f"/admin/archive/image?rel_path={quote(record.rel_path)}&filename={quote(record.false_cut)}"
                lbl = "Yanlış Hali (Eski / Hatalı)" if is_easy else "Eski Kesim (Yanlış Hali)"
                preview_html += f"""
                <div style="display:flex; flex-direction:column; gap:4px;">
                  <span style="font-size:12px; font-weight:bold; color:#ef4444;">{lbl}</span>
                  <a href="{false_url}" target="_blank" rel="noopener">
                    <img src="{false_url}" alt="Eski Hali" style="{img_style} object-fit:contain; border:1px solid #ddd; border-radius:4px; max-width:100%; background:#f8fafc;">
                  </a>
                </div>
                """
            if record.true_cut:
                true_url = f"/admin/archive/image?rel_path={quote(record.rel_path)}&filename={quote(record.true_cut)}"
                lbl = "Doğru Hali (Yeni / Düzeltilmiş)" if is_easy else "Yeni Kesim (Doğru Hali)"
                preview_html += f"""
                <div style="display:flex; flex-direction:column; gap:4px;">
                  <span style="font-size:12px; font-weight:bold; color:#10b981;">{lbl}</span>
                  <a href="{true_url}" target="_blank" rel="noopener">
                    <img src="{true_url}" alt="Yeni Hali" style="{img_style} object-fit:contain; border:1px solid #ddd; border-radius:4px; max-width:100%; background:#f8fafc;">
                  </a>
                </div>
                """
            preview_html += '</div>'

        q_label = f"Soru {record.soru_no}" if record.soru_no > 0 else f"Sayfa {record.page_idx + 1} Düzeltmesi"
        issue_label = "Kolay Düzenleyici" if record.kind == "easy_editor" else (record.issue_code or '-')

        rows.append(
            f"""
            <li class="row">
              <div style="width:100%;">
                <strong>{html.escape(record.rel_path)}</strong>
                <span>{html.escape(format_ts(record.created_at))} | Test {record.test_no or '-'} | {q_label} | Hata/Kaynak: {html.escape(issue_label)}</span>
                <span>PDF: {html.escape(record.source_pdf or '-')} | Profil: {html.escape(record.profile_key[:20] or '-')}</span>
                <span>Not: {html.escape(record.note or '-')}</span>
                {preview_html}
                <form action="/admin/archive/update" method="post" style="margin-top:8px; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                  {hidden_filters}
                  <input type="hidden" name="rel_path" value="{html.escape(record.rel_path)}">
                  <label style="display:flex; gap:6px; align-items:center;">
                    <strong>Durum</strong>
                    <select name="status">{option_html}</select>
                  </label>
                  <input name="dev_note" value="{html.escape(record.dev_note)}" placeholder="Kod geliştirme notu (örn: crop bottom +12 denenecek)" style="min-width:320px; flex:1;">
                  <button type="submit">Kaydet</button>
                </form>
                <form action="/admin/archive/import" method="post" style="margin-top:6px;">
                  {hidden_filters}
                  <input type="hidden" name="rel_path" value="{html.escape(record.rel_path)}">
                  <button class="btn secondary" type="submit">Bu kaydı öğrenmeye işle</button>
                </form>
              </div>
              <span class="pill">{html.escape(record.status)}</span>
            </li>
            """
        )

    body = f"""
    <section class="hero">
      <span class="eyebrow">Admin</span>
      <h1>Archive çalışma paneli.</h1>
      <p class="sub">Hata kayıtlarını buradan inceleyip durumlayabilir, öğrenmeye işleyip öğrenildi kayıtları temizleyebilirsiniz.</p>
    </section>
    <section class="card">
      {info_note}
      {filter_error_note}
      {summary_note}
      {empty_note}
      <form action="/admin/archive" method="get" style="margin-bottom:16px;">
        <div class="actions" style="margin-bottom:0;">
          <label class="upload" style="min-width:200px; margin:0;">
            <strong>Başlangıç tarihi</strong>
            <input type="date" name="start_date" value="{html.escape(filter_start)}">
          </label>
          <label class="upload" style="min-width:200px; margin:0;">
            <strong>Bitiş tarihi</strong>
            <input type="date" name="end_date" value="{html.escape(filter_end)}">
          </label>
          <button type="submit">Filtrele</button>
          <a class="btn ghost" href="/admin/archive">Temizle</a>
          <a class="btn ghost" href="/admin">Admin ana ekran</a>
        </div>
      </form>
      <div class="actions" style="margin-bottom:16px;">
        <span class="pill">Toplam: {total_records}</span>
        <span class="pill">Yeni: {counts["new"]}</span>
        <span class="pill">İncelemede: {counts["in_review"]}</span>
        <span class="pill">Öğrenildi: {counts["learned"]}</span>
        <form action="/admin/archive/delete-learned" method="post" style="margin:0;">
          {hidden_filters}
          <button class="btn secondary" type="submit">Öğrenildi kayıtlarını sil</button>
        </form>
      </div>
      <ul class="list">
        {''.join(rows)}
      </ul>
    </section>
    """
    return page_shell("Archive", body)


def local_sanitize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip()
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"[^\w.\-]+", "_", normalized, flags=re.UNICODE)
    normalized = re.sub(r"_+", "_", normalized)
    return normalized.strip("._") or "untitled"


def read_json_file(path: Path) -> dict | list:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_learning_profiles(out_dir: Path) -> dict[str, dict[str, str]]:
    profiles: dict[str, dict[str, str]] = {}
    for profile_path in out_dir.glob("*_learning_profile.json"):
        payload = read_json_file(profile_path)
        if not isinstance(payload, dict):
            continue
        source_pdf = str(payload.get("source_pdf") or "")
        profile_key = str(payload.get("profile_key") or "")
        if source_pdf and profile_key:
            profiles[source_pdf] = {"profile_key": profile_key, "profile_file": profile_path.name}
    return profiles


def load_quality_entries(out_dir: Path) -> dict[tuple[str, int, int], dict[str, str | int]]:
    quality: dict[tuple[str, int, int], dict[str, str | int]] = {}
    for report_path in out_dir.glob("*_quality_report.json"):
        payload = read_json_file(report_path)
        if not isinstance(payload, dict):
            continue
        source_pdf = str(payload.get("source_pdf") or "")
        for entry in payload.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            key = (source_pdf, int(entry.get("test_no") or 0), int(entry.get("soru_no") or 0))
            issues = ", ".join(
                str(issue.get("code") or "")
                for issue in entry.get("issues", []) or []
                if isinstance(issue, dict) and issue.get("code")
            )
            issue_codes = [
                str(issue.get("code") or "")
                for issue in entry.get("issues", []) or []
                if isinstance(issue, dict) and issue.get("code")
            ]
            quality[key] = {
                "score": int(entry.get("score") or 0),
                "issues": issues or "temiz",
                "issue_codes": "|".join(issue_codes),
            }
    return quality


CRITICAL_REVIEW_ISSUES = {
    "crop_out_of_page",
    "next_question_leak",
    "too_narrow",
    "too_short",
    "missing_page",
    "neighbor_activity_heading",
    "visual_content_outside_crop",
    "content_cut_at_edge",
}
WARNING_REVIEW_ISSUES = {
    "anchor_on_left_edge",
    "missing_question_anchor",
    "possible_header_residue",
    "too_tall",
}
REVIEW_RISK_LABELS = {
    "critical": "Kritik",
    "warning": "Uyarı",
    "clean": "Temiz",
}
REVIEW_RISK_ORDER = {
    "critical": 0,
    "warning": 1,
    "clean": 2,
}
PROFILE_FEEDBACK_RISK_THRESHOLD = 3
QUESTION_FEEDBACK_RISK_THRESHOLD = 2


def review_feedback_counts(
    feedback_profile: dict[str, Any],
    test_no: int,
    soru_no: int,
) -> tuple[int, int]:
    issue_counts = feedback_profile.get("issue_counts", {})
    profile_count = (
        sum(max(0, int(count or 0)) for count in issue_counts.values())
        if isinstance(issue_counts, dict)
        else 0
    )
    question_issue_counts = feedback_profile.get("question_issue_counts", {})
    question_counts = (
        question_issue_counts.get(f"{test_no}:{soru_no}", {})
        if isinstance(question_issue_counts, dict)
        else {}
    )
    question_count = (
        sum(max(0, int(count or 0)) for count in question_counts.values())
        if isinstance(question_counts, dict)
        else 0
    )
    return profile_count, question_count


def review_risk_payload(
    score: int,
    issue_codes_text: str,
    manual_bounds: bool,
    *,
    profile_feedback_count: int = 0,
    question_feedback_count: int = 0,
) -> dict[str, str | int]:
    issue_codes = {code for code in issue_codes_text.split("|") if code}
    reasons: list[str] = []
    clean_score = max(0, min(100, int(score)))
    missing_quality_report = not issue_codes and clean_score <= 0
    risk_score = 45 if missing_quality_report else 100 - clean_score

    if issue_codes & CRITICAL_REVIEW_ISSUES or (issue_codes and score < 72):
        level = "critical"
        risk_score = max(risk_score, 80)
    elif (
        issue_codes & WARNING_REVIEW_ISSUES
        or (0 < clean_score < 80)
        or missing_quality_report
        or profile_feedback_count >= PROFILE_FEEDBACK_RISK_THRESHOLD
        or question_feedback_count >= QUESTION_FEEDBACK_RISK_THRESHOLD
    ):
        level = "warning"
        risk_score = max(risk_score, 40)
    else:
        level = "clean"

    if issue_codes & WARNING_REVIEW_ISSUES:
        risk_score = max(risk_score, 45)
    if 0 < clean_score < 80:
        risk_score = max(risk_score, 40 + min(20, 80 - clean_score))
    if profile_feedback_count >= PROFILE_FEEDBACK_RISK_THRESHOLD:
        risk_score += min(15, 4 + (profile_feedback_count // 2))
    if question_feedback_count >= QUESTION_FEEDBACK_RISK_THRESHOLD:
        risk_score += min(20, 5 + (question_feedback_count * 3))
    risk_score = min(100, risk_score)

    if "crop_out_of_page" in issue_codes:
        reasons.append("Sayfa dışına taşma")
    if "next_question_leak" in issue_codes:
        reasons.append("Sonraki soru sızmış")
    if "too_short" in issue_codes:
        reasons.append("Kısa kesim")
    if "too_narrow" in issue_codes:
        reasons.append("Dar kesim")
    if "neighbor_activity_heading" in issue_codes:
        reasons.append("Komşu alıştırma başlığı sızmış")
    if "visual_content_outside_crop" in issue_codes:
        reasons.append("Görsel içerik kesim dışında")
    if "content_cut_at_edge" in issue_codes:
        reasons.append("İçerik kenarda kesiliyor")
    if "missing_question_anchor" in issue_codes:
        reasons.append("Soru numarası bulunamadı")
    if "anchor_on_left_edge" in issue_codes:
        reasons.append("Sol kenar riski")
    if "possible_header_residue" in issue_codes:
        reasons.append("Üst başlık riski")
    if "too_tall" in issue_codes:
        reasons.append("Fazla uzun")
    if manual_bounds:
        reasons.append("Manuel sınır")
    if question_feedback_count >= QUESTION_FEEDBACK_RISK_THRESHOLD:
        reasons.append(f"Aynı soru düzeninde {question_feedback_count} manuel hata")
    if profile_feedback_count >= PROFILE_FEEDBACK_RISK_THRESHOLD:
        reasons.append(f"Bu profilde {profile_feedback_count} manuel hata")
    if missing_quality_report:
        reasons.append("Kalite raporu yok")
    if not reasons and level == "warning":
        reasons.append("Düşük kalite skoru")
    if not reasons:
        reasons.append("Temiz")

    return {
        "risk_level": level,
        "risk_label": REVIEW_RISK_LABELS[level],
        "risk_reasons": ", ".join(reasons),
        "risk_rank": REVIEW_RISK_ORDER[level],
        "risk_score": risk_score,
    }


AUTOFIX_REASON_LABELS = {
    "crop_out_of_page": "Sayfa sınırı düzeltildi",
    "next_question_leak": "Sonraki soru sızıntısı azaltıldı",
    "too_short": "Alt sınır genişletildi",
    "left_edge_or_narrow": "Sol sınır açıldı",
}


MANUAL_LEARNING_REASON_LABELS = {
    "manual_bounds": "Önceki manuel sınır",
    "manual_common_stem": "Önceki ortak kök",
    "top_pad": "Üst sınır genişletildi",
    "left_pad": "Sol sınır açıldı",
    "right_pad": "Sağ sınır açıldı",
    "bottom_pad": "Alt sınır genişletildi",
    "bottom_shrink": "Alt sınır daraltıldı",
}


def autofix_reason_label(reason_text: str) -> str:
    reasons = [reason for reason in reason_text.split(",") if reason]
    labels = [AUTOFIX_REASON_LABELS.get(reason, reason) for reason in reasons]
    return ", ".join(labels) if labels else "Otomatik düzeltme"


def manual_learning_reason_label(reasons: Any) -> str:
    if isinstance(reasons, list):
        clean_reasons = [str(reason) for reason in reasons if str(reason)]
    else:
        clean_reasons = [reason for reason in str(reasons or "").split(",") if reason]
    labels = [MANUAL_LEARNING_REASON_LABELS.get(reason, reason) for reason in clean_reasons]
    return ", ".join(labels) if labels else "Önceki öğrenme"


def normalize_report_bounds(bounds: Any) -> dict[str, float] | None:
    if not isinstance(bounds, dict):
        return None
    try:
        clean_bounds = {
            "crop_left": float(bounds["crop_left"]),
            "crop_top": float(bounds["crop_top"]),
            "crop_right": float(bounds["crop_right"]),
            "crop_bottom": float(bounds["crop_bottom"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if clean_bounds["crop_right"] <= clean_bounds["crop_left"] + 2.0:
        return None
    if clean_bounds["crop_bottom"] <= clean_bounds["crop_top"] + 2.0:
        return None
    return clean_bounds


def adjustment_compare_payload(out_dir: Path, row: dict[str, str | int], kind: str) -> dict[str, Any] | None:
    if kind == "autofix":
        report_paths = sorted(out_dir.glob("*_autofix_report.json"))
        label = "Otomatik düzeltme"
    elif kind == "learning":
        report_paths = sorted(out_dir.glob("*_manual_feedback_applied.json"))
        label = "Öğrenme"
    else:
        return None

    test_no = int(row.get("test_no") or 0)
    soru_no = int(row.get("soru_no") or 0)
    page_idx = int(row.get("page_idx") or 0)
    for report_path in report_paths:
        payload = read_json_file(report_path)
        adjustments = payload.get("adjustments", []) if isinstance(payload, dict) else payload
        if not isinstance(adjustments, list):
            continue
        for adjustment in adjustments:
            if not isinstance(adjustment, dict):
                continue
            if (
                int(adjustment.get("test_no") or 0) != test_no
                or int(adjustment.get("soru_no") or 0) != soru_no
                or int(adjustment.get("page_idx") or 0) != page_idx
            ):
                continue
            before = normalize_report_bounds(adjustment.get("before"))
            after = normalize_report_bounds(adjustment.get("after"))
            if before is None or after is None:
                return None
            if kind == "autofix":
                reason_label = autofix_reason_label(str(adjustment.get("reason") or ""))
            else:
                reason_label = manual_learning_reason_label(adjustment.get("reasons", []))
            return {
                "kind": kind,
                "title": f"{label}: {reason_label}",
                "before": before,
                "after": after,
            }
    return None


def load_autofix_entries(out_dir: Path) -> dict[tuple[int, int, int], dict[str, str | int]]:
    autofix: dict[tuple[int, int, int], dict[str, str | int]] = {}
    for report_path in sorted(out_dir.glob("*_autofix_report.json")):
        payload = read_json_file(report_path)
        if not isinstance(payload, dict):
            continue
        for adjustment in payload.get("adjustments", []) or []:
            if not isinstance(adjustment, dict):
                continue
            key = (
                int(adjustment.get("test_no") or 0),
                int(adjustment.get("soru_no") or 0),
                int(adjustment.get("page_idx") or 0),
            )
            reason = str(adjustment.get("reason") or "")
            autofix[key] = {
                "autofix": 1,
                "autofix_reason": reason,
                "autofix_label": autofix_reason_label(reason),
            }
    return autofix


def count_autofix_adjustments(out_dir: Path) -> int:
    total = 0
    for report_path in sorted(out_dir.glob("*_autofix_report.json")):
        payload = read_json_file(report_path)
        if not isinstance(payload, dict):
            continue
        try:
            total += int(payload.get("applied_count") or 0)
        except (TypeError, ValueError):
            total += len([item for item in payload.get("adjustments", []) or [] if isinstance(item, dict)])
    return total


def load_manual_learning_entries(out_dir: Path) -> dict[tuple[int, int, int], dict[str, str | int]]:
    learning: dict[tuple[int, int, int], dict[str, str | int]] = {}
    for report_path in sorted(out_dir.glob("*_manual_feedback_applied.json")):
        payload = read_json_file(report_path)
        if not isinstance(payload, list):
            continue
        for adjustment in payload:
            if not isinstance(adjustment, dict):
                continue
            key = (
                int(adjustment.get("test_no") or 0),
                int(adjustment.get("soru_no") or 0),
                int(adjustment.get("page_idx") or 0),
            )
            reasons = adjustment.get("reasons", [])
            learning[key] = {
                "manual_learning": 1,
                "manual_learning_reasons": ",".join(str(reason) for reason in reasons) if isinstance(reasons, list) else str(reasons or ""),
                "manual_learning_label": manual_learning_reason_label(reasons),
            }
    return learning


def count_manual_learning_adjustments(out_dir: Path) -> int:
    total = 0
    for report_path in sorted(out_dir.glob("*_manual_feedback_applied.json")):
        payload = read_json_file(report_path)
        if isinstance(payload, list):
            total += len([item for item in payload if isinstance(item, dict)])
    return total


def _report_quality_breakdown(payload: Any) -> dict[str, int | float] | None:
    if not isinstance(payload, dict):
        return None
    entries = payload.get("entries", [])
    if not isinstance(entries, list):
        return None
    total = 0
    score_total = 0
    critical = 0
    warning = 0
    clean = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        total += 1
        score = int(entry.get("score") or 0)
        score_total += score
        issue_codes = "|".join(
            str(issue.get("code") or "")
            for issue in entry.get("issues", []) or []
            if isinstance(issue, dict) and issue.get("code")
        )
        risk = str(review_risk_payload(score, issue_codes, False)["risk_level"])
        if risk == "critical":
            critical += 1
        elif risk == "warning":
            warning += 1
        else:
            clean += 1
    if total <= 0:
        return None
    return {
        "total": total,
        "score_total": score_total,
        "critical": critical,
        "warning": warning,
        "clean": clean,
        "average_score": round(score_total / total, 2),
    }


def result_autofix_quality_delta(out_dir: Path) -> dict[str, int | float] | None:
    before_reports: dict[str, dict[str, int | float]] = {}
    for report_path in sorted(out_dir.glob("*_quality_before_autofix.json")):
        payload = read_json_file(report_path)
        summary = _report_quality_breakdown(payload)
        source_pdf = str(payload.get("source_pdf") or "") if isinstance(payload, dict) else ""
        if summary is None or not source_pdf:
            continue
        before_reports[source_pdf] = summary
    if not before_reports:
        return None

    after_reports: dict[str, dict[str, int | float]] = {}
    for report_path in sorted(out_dir.glob("*_quality_report.json")):
        payload = read_json_file(report_path)
        summary = _report_quality_breakdown(payload)
        source_pdf = str(payload.get("source_pdf") or "") if isinstance(payload, dict) else ""
        if summary is None or not source_pdf:
            continue
        after_reports[source_pdf] = summary
    for report_path in sorted(out_dir.glob("*_quality_after_autofix.json")):
        payload = read_json_file(report_path)
        summary = _report_quality_breakdown(payload)
        source_pdf = str(payload.get("source_pdf") or "") if isinstance(payload, dict) else ""
        if summary is None or not source_pdf:
            continue
        after_reports[source_pdf] = summary

    before_total = 0
    before_score_total = 0
    before_critical = 0
    before_warning = 0
    before_clean = 0
    after_total = 0
    after_score_total = 0
    after_critical = 0
    after_warning = 0
    after_clean = 0
    compared_sources = 0

    for source_pdf, before in before_reports.items():
        after = after_reports.get(source_pdf)
        if not after:
            continue
        compared_sources += 1
        before_total += int(before["total"])
        before_score_total += int(before["score_total"])
        before_critical += int(before["critical"])
        before_warning += int(before["warning"])
        before_clean += int(before["clean"])
        after_total += int(after["total"])
        after_score_total += int(after["score_total"])
        after_critical += int(after["critical"])
        after_warning += int(after["warning"])
        after_clean += int(after["clean"])

    if compared_sources <= 0 or before_total <= 0 or after_total <= 0:
        return None

    before_average = round(before_score_total / before_total, 2)
    after_average = round(after_score_total / after_total, 2)
    return {
        "sources": compared_sources,
        "before_average_score": before_average,
        "after_average_score": after_average,
        "average_delta": round(after_average - before_average, 2),
        "before_critical": before_critical,
        "after_critical": after_critical,
        "critical_delta": after_critical - before_critical,
        "before_warning": before_warning,
        "after_warning": after_warning,
        "warning_delta": after_warning - before_warning,
        "before_clean": before_clean,
        "after_clean": after_clean,
        "clean_delta": after_clean - before_clean,
    }


def result_quality_summary(out_dir: Path) -> dict[str, int | float] | None:
    total = 0
    score_total = 0
    critical = 0
    warning = 0
    clean = 0

    for report_path in sorted(out_dir.glob("*_quality_report.json")):
        payload = read_json_file(report_path)
        if not isinstance(payload, dict):
            continue
        for entry in payload.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            total += 1
            score = int(entry.get("score") or 0)
            score_total += score
            issue_codes = "|".join(
                str(issue.get("code") or "")
                for issue in entry.get("issues", []) or []
                if isinstance(issue, dict) and issue.get("code")
            )
            risk = str(review_risk_payload(score, issue_codes, False)["risk_level"])
            if risk == "critical":
                critical += 1
            elif risk == "warning":
                warning += 1
            else:
                clean += 1

    if total <= 0:
        return None
    autofix_delta = result_autofix_quality_delta(out_dir)
    error_memory = load_error_memory()
    signatures = error_memory.get("signatures", {})
    knowledge_signatures = len(signatures) if isinstance(signatures, dict) else 0
    return {
        "total": total,
        "critical": critical,
        "warning": warning,
        "clean": clean,
        "average_score": round(score_total / total, 1),
        "autofix_applied": count_autofix_adjustments(out_dir),
        "manual_learning_applied": count_manual_learning_adjustments(out_dir),
        "autofix_delta_avg": float(autofix_delta["average_delta"]) if autofix_delta else 0.0,
        "autofix_delta_critical": int(autofix_delta["critical_delta"]) if autofix_delta else 0,
        "autofix_delta_warning": int(autofix_delta["warning_delta"]) if autofix_delta else 0,
        "autofix_delta_clean": int(autofix_delta["clean_delta"]) if autofix_delta else 0,
        "autofix_delta_sources": int(autofix_delta["sources"]) if autofix_delta else 0,
        "knowledge_signatures": knowledge_signatures,
    }


def result_quality_summary_html(out_dir: Path) -> str:
    summary = result_quality_summary(out_dir)
    if not summary:
        return ""
    total = int(summary["total"])
    critical = int(summary["critical"])
    warning = int(summary["warning"])
    clean = int(summary["clean"])
    average_score = float(summary["average_score"])
    autofix_applied = int(summary["autofix_applied"])
    manual_learning_applied = int(summary["manual_learning_applied"])
    autofix_delta_avg = float(summary["autofix_delta_avg"])
    autofix_delta_critical = int(summary["autofix_delta_critical"])
    autofix_delta_warning = int(summary["autofix_delta_warning"])
    autofix_delta_clean = int(summary["autofix_delta_clean"])
    autofix_delta_sources = int(summary["autofix_delta_sources"])
    knowledge_signatures = int(summary["knowledge_signatures"])
    autofix_delta_sign = "+" if autofix_delta_avg > 0 else ""
    autofix_delta_summary = (
        f"Öncesi/sonrası kalite farkı: {autofix_delta_sign}{autofix_delta_avg:.2f} | "
        f"Kritik {autofix_delta_critical:+d}, Uyarı {autofix_delta_warning:+d}, Temiz {autofix_delta_clean:+d}"
        if autofix_delta_sources > 0
        else "Otomatik düzeltme öncesi/sonrası karşılaştırma verisi yok."
    )
    risk_note = "Kontrol önerilir." if critical or warning else "Kesimler temiz görünüyor."
    return f"""
      <div class="quality-summary">
        <div class="metric">
          <p class="label">Toplam soru</p>
          <strong>{total}</strong>
        </div>
        <div class="metric quality-critical">
          <p class="label">Kritik</p>
          <strong>{critical}</strong>
        </div>
        <div class="metric quality-warning">
          <p class="label">Uyarı</p>
          <strong>{warning}</strong>
        </div>
        <div class="metric quality-clean">
          <p class="label">Temiz</p>
          <strong>{clean}</strong>
        </div>
        <div class="metric">
          <p class="label">Ortalama kalite</p>
          <strong>{average_score:.1f}</strong>
        </div>
        <div class="metric quality-autofix">
          <p class="label">Otomatik düzeltme</p>
          <strong>{autofix_applied}</strong>
        </div>
        <div class="metric quality-learning">
          <p class="label">Öğrenme uygulandı</p>
          <strong>{manual_learning_applied}</strong>
        </div>
        <div class="metric quality-memory">
          <p class="label">Kesim bilgi deposu</p>
          <strong>{knowledge_signatures}</strong>
        </div>
      </div>
      <p class="meta quality-note">{html.escape(autofix_delta_summary)}</p>
      <p class="meta quality-note">{html.escape(risk_note)}</p>
    """


def find_question_pdf(out_dir: Path, entry: dict) -> str:
    configured_path = str(entry.get("question_pdf") or "").strip()
    if configured_path:
        resolved_out_dir = out_dir.resolve()
        configured = (resolved_out_dir / configured_path).resolve()
        try:
            configured.relative_to(resolved_out_dir)
        except ValueError:
            configured = resolved_out_dir / ""
        if configured.suffix.lower() == ".pdf":
            return configured.relative_to(resolved_out_dir).as_posix()
    soru_no = int(entry.get("soru_no") or 0)
    test_name = str(entry.get("test_name") or "")
    expected_parent = f"{local_sanitize(test_name)}_questions"
    candidates = sorted(out_dir.rglob(f"{soru_no:02d}.pdf"))
    for candidate in candidates:
        if candidate.parent.name == expected_parent:
            return candidate.relative_to(out_dir).as_posix()
    return candidates[0].relative_to(out_dir).as_posix() if candidates else ""


def safe_output_pdf(job_id: str, path: str) -> tuple[Path, Path]:
    job_dir, _ = get_active_job(job_id)
    out_dir = (job_dir / "output").resolve()
    file_path = (out_dir / path).resolve()
    if out_dir not in file_path.parents or not file_path.exists() or file_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    return job_dir, file_path


def preview_image_for_pdf(job_id: str, pdf_rel_path: str) -> Path:
    job_dir, pdf_path = safe_output_pdf(job_id, pdf_rel_path)
    cache_dir = job_dir / "preview_cache"
    ensure_dir(cache_dir)
    digest = hashlib.sha1(pdf_rel_path.encode("utf-8")).hexdigest()[:20]
    out_prefix = cache_dir / digest
    image_path = out_prefix.with_suffix(".png")
    if image_path.exists():
        return image_path

    result = subprocess.run(
        [
            get_binary_path("pdftocairo"),
            "-png",
            "-singlefile",
            "-r",
            "130",
            "-f",
            "1",
            "-l",
            "1",
            str(pdf_path),
            str(out_prefix),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=processor_env(),
    )
    if result.returncode != 0 or not image_path.exists():
        raise HTTPException(status_code=500, detail=result.stderr or result.stdout or "Önizleme üretilemedi")
    return image_path


def source_pdf_path_for_row(job_dir: Path, row: dict[str, str | int]) -> Path:
    input_dir = job_dir / "input"
    source_pdf = str(row.get("source_pdf") or "")
    direct = input_dir / source_pdf
    if direct.exists():
        return direct
    import unicodedata
    name_nfc = unicodedata.normalize("NFC", source_pdf)
    name_nfd = unicodedata.normalize("NFD", source_pdf)
    if (input_dir / name_nfc).exists():
        return input_dir / name_nfc
    if (input_dir / name_nfd).exists():
        return input_dir / name_nfd
    matches = sorted(input_dir.glob("*.pdf"))
    if len(matches) == 1:
        return matches[0]
    raise HTTPException(status_code=404, detail="Orijinal PDF bulunamadı")


def source_page_layout_for_row(job_dir: Path, row: dict[str, str | int], page_idx: int) -> dict[str, float | int | str]:
    pdf_path = source_pdf_path_for_row(job_dir, row)
    page_no = int(page_idx) + 1
    cache_dir = job_dir / "preview_cache"
    ensure_dir(cache_dir)
    xml_path = cache_dir / f"source_page_{page_no}.xml"
    if not xml_path.exists():
        result = subprocess.run(
            [
                "pdftohtml",
                "-xml",
                "-i",
                "-f",
                str(page_no),
                "-l",
                str(page_no),
                str(pdf_path),
                str(xml_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=processor_env(),
        )
        if result.returncode != 0 or not xml_path.exists():
            raise HTTPException(status_code=500, detail=result.stderr or result.stdout or "Sayfa bilgisi okunamadı")
    root = ET.parse(xml_path).getroot()
    page_el = root.find("page")
    if page_el is None:
        raise HTTPException(status_code=500, detail="Sayfa bilgisi bulunamadı")
    return {
        "row_id": -1,
        "page_no": page_no,
        "width_px": float(page_el.attrib["width"]),
        "height_px": float(page_el.attrib["height"]),
        "source_pdf": str(row.get("source_pdf") or ""),
    }


def source_page_layout(job_id: str, row_id: int) -> dict[str, float | int | str]:
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    return source_page_layout_for_row(job_dir, row, int(row["page_idx"]))


def source_page_image_for_row(job_dir: Path, row: dict[str, str | int], page_idx: int, dpi: int = 150) -> Path:
    pdf_path = source_pdf_path_for_row(job_dir, row)
    page_no = int(page_idx) + 1
    cache_dir = job_dir / "preview_cache"
    ensure_dir(cache_dir)
    out_prefix = cache_dir / f"source_page_{page_no}_{dpi}"
    image_path = out_prefix.with_suffix(".png")
    if image_path.exists():
        return image_path
    result = subprocess.run(
        [
            get_binary_path("pdftocairo"),
            "-png",
            "-singlefile",
            "-r",
            str(dpi),
            "-f",
            str(page_no),
            "-l",
            str(page_no),
            str(pdf_path),
            str(out_prefix),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=processor_env(),
    )
    if result.returncode != 0 or not image_path.exists():
        raise HTTPException(status_code=500, detail=result.stderr or result.stdout or "Sayfa önizlemesi üretilemedi")
    return image_path


def source_page_image(job_id: str, row_id: int) -> Path:
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    return source_page_image_for_row(job_dir, row, int(row["page_idx"]), dpi=150)


def source_page_image_at_dpi(job_id: str, row_id: int, dpi: int) -> Path:
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    return source_page_image_for_row(job_dir, row, int(row["page_idx"]), dpi=dpi)


def crop_source_page_to_image(job_id: str, row_id: int, *, dpi: int = 150) -> Image.Image:
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    layout = source_page_layout(job_id, row_id)
    page_image = source_page_image_at_dpi(job_id, row_id, dpi)
    page_width = float(layout["width_px"])
    page_height = float(layout["height_px"])
    bounds = {
        "crop_left": float(row["crop_left"]),
        "crop_top": float(row["crop_top"]),
        "crop_right": float(row["crop_right"]),
        "crop_bottom": float(row["crop_bottom"]),
    }
    return crop_bounds_to_image(job_id, row_id, bounds, dpi=dpi)


def crop_bounds_to_image(job_id: str, row_id: int, bounds: dict[str, float], *, dpi: int = 150) -> Image.Image:
    layout = source_page_layout(job_id, row_id)
    page_image = source_page_image_at_dpi(job_id, row_id, dpi)
    page_width = float(layout["width_px"])
    page_height = float(layout["height_px"])
    with Image.open(page_image) as image:
        scale_x = image.width / page_width
        scale_y = image.height / page_height
        left = max(0, min(image.width - 1, int(bounds["crop_left"] * scale_x)))
        top = max(0, min(image.height - 1, int(bounds["crop_top"] * scale_y)))
        right = max(left + 2, min(image.width, int(bounds["crop_right"] * scale_x)))
        bottom = max(top + 2, min(image.height, int(bounds["crop_bottom"] * scale_y)))
        return image.crop((left, top, right, bottom)).convert("RGB")


def crop_row_page_bounds_to_image(
    job_dir: Path,
    row: dict[str, str | int],
    page_idx: int,
    bounds: dict[str, float],
    *,
    dpi: int = 150,
) -> Image.Image:
    layout = source_page_layout_for_row(job_dir, row, page_idx)
    page_image = source_page_image_for_row(job_dir, row, page_idx, dpi=dpi)
    page_width = float(layout["width_px"])
    page_height = float(layout["height_px"])
    with Image.open(page_image) as image:
        scale_x = image.width / page_width
        scale_y = image.height / page_height
        left = max(0, min(image.width - 1, int(float(bounds["crop_left"]) * scale_x)))
        top = max(0, min(image.height - 1, int(float(bounds["crop_top"]) * scale_y)))
        right = max(left + 2, min(image.width, int(float(bounds["crop_right"]) * scale_x)))
        bottom = max(top + 2, min(image.height, int(float(bounds["crop_bottom"]) * scale_y)))
        return image.crop((left, top, right, bottom)).convert("RGB")


def compose_compare_image(job_id: str, row_id: int, kind: str) -> Image.Image:
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    compare = adjustment_compare_payload(job_dir / "output", row, kind)
    if compare is None:
        raise HTTPException(status_code=404, detail="Karşılaştırma verisi bulunamadı")

    before = crop_bounds_to_image(job_id, row_id, compare["before"], dpi=150)
    after = crop_bounds_to_image(job_id, row_id, compare["after"], dpi=150)
    max_preview_height = 640
    resized: list[Image.Image] = []
    for image in (before, after):
        if image.height > max_preview_height:
            ratio = max_preview_height / image.height
            image = image.resize((max(1, int(image.width * ratio)), max_preview_height))
        resized.append(image)

    before, after = resized
    title_height = 42
    gap = 18
    pad = 16
    width = before.width + after.width + gap + (pad * 2)
    height = max(before.height, after.height) + title_height + (pad * 2)
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, 12), "Önce", fill=(23, 20, 17))
    draw.text((pad + before.width + gap, 12), "Sonra", fill=(23, 20, 17))
    top = title_height
    canvas.paste(before, (pad, top))
    canvas.paste(after, (pad + before.width + gap, top))
    draw.line((pad + before.width + (gap // 2), title_height, pad + before.width + (gap // 2), height - pad), fill=(210, 210, 210), width=2)
    return canvas


def compare_image_path(job_id: str, row_id: int, kind: str) -> Path:
    job_dir, _ = get_active_job(job_id)
    if kind not in {"autofix", "learning"}:
        raise HTTPException(status_code=404, detail="Karşılaştırma bulunamadı")
    cache_dir = job_dir / "preview_cache"
    ensure_dir(cache_dir)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    compare = adjustment_compare_payload(job_dir / "output", row, kind)
    if compare is None:
        raise HTTPException(status_code=404, detail="Karşılaştırma verisi bulunamadı")
    digest = hashlib.sha1(
        json.dumps(
            {
                "row_id": row_id,
                "kind": kind,
                "test_no": int(row.get("test_no") or 0),
                "soru_no": int(row.get("soru_no") or 0),
                "page_idx": int(row.get("page_idx") or 0),
                "autofix": int(row.get("autofix") or 0),
                "learning": int(row.get("manual_learning") or 0),
                "before": compare["before"],
                "after": compare["after"],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:20]
    target_path = cache_dir / f"compare_{digest}.png"
    if not target_path.exists():
        compose_compare_image(job_id, row_id, kind).save(target_path)
    return target_path


def source_page_text_items(job_dir: Path, row: dict[str, str | int], page_idx: int) -> list[dict[str, float | str]]:
    source_page_layout_for_row(job_dir, row, page_idx)
    xml_path = job_dir / "preview_cache" / f"source_page_{int(page_idx) + 1}.xml"
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return []
    items: list[dict[str, float | str]] = []
    for text_el in root.findall(".//text"):
        raw_text = "".join(text_el.itertext())
        text = raw_text.strip()
        if not text:
            continue
        try:
            left = float(text_el.attrib.get("left", "0"))
            top = float(text_el.attrib.get("top", "0"))
            width = float(text_el.attrib.get("width", "0"))
            height = float(text_el.attrib.get("height", "0"))
        except ValueError:
            continue

        # Sondaki boşlukların genişliğini orantısal olarak düşür
        stripped_right = raw_text.rstrip()
        if len(raw_text) > 0 and len(stripped_right) < len(raw_text) and width > 0:
            width = width * (len(stripped_right) / len(raw_text))

        items.append({
            "text": text,
            "left": left,
            "top": top,
            "right": left + width,
            "bottom": top + height,
            "height": height,
        })
    return items


def get_adjusted_crop_left(
    job_dir: Path,
    row: dict[str, str | int],
    bounds: dict[str, float],
    *,
    page_idx: int | None = None,
) -> float:
    soru_no = int(row.get("soru_no") or 0)
    if soru_no <= 0:
        return bounds["crop_left"]
    page_idx = int(row.get("page_idx") or 0) if page_idx is None else int(page_idx)
    crop_left = float(bounds["crop_left"])
    crop_top = float(bounds["crop_top"])
    crop_right = float(bounds["crop_right"])
    crop_bottom = float(bounds["crop_bottom"])
    label_prefixes = (f"{soru_no}.", f"{soru_no} .", f"{soru_no})")
    
    text_items = source_page_text_items(job_dir, row, page_idx)
    candidates = []
    for item in text_items:
        text = str(item["text"]).strip()
        if not text.startswith(label_prefixes) and text != str(soru_no):
            continue
        left = float(item["left"])
        top = float(item["top"])
        right = float(item["right"])
        bottom = float(item["bottom"])
        if right <= crop_left or left >= crop_right or bottom <= crop_top or top >= crop_bottom:
            continue
        candidates.append(item)

    if not candidates:
        return crop_left

    item = min(candidates, key=lambda candidate: (abs(float(candidate["top"]) - crop_top), float(candidate["left"])))
    item_left = float(item["left"])
    item_right = float(item["right"])
    item_width = item_right - item_left
    item_height = max(8.0, float(item["height"]))
    
    # Sondaki boşlukları hesaba katarak genişliği orantılı olarak küçült
    raw_text = str(item["text"])
    stripped_text = raw_text.rstrip()
    if len(raw_text) > 0 and len(stripped_text) < len(raw_text):
        item_width = item_width * (len(stripped_text) / len(raw_text))
        item_right = item_left + item_width

    text = raw_text.strip()
    if text.startswith(label_prefixes):
        is_pure_label = bool(re.match(r"^[0-9.\s()]+$", text))
        if not is_pure_label:
            digit_count = len(str(soru_no))
            item_right = min(item_right, item_left + item_height * (0.50 + digit_count * 0.30))
    # Calibrated pad of 1.5 points instead of max(3.0, item_height * 0.18)
    pad = 1.5
    new_left = item_right + pad
    if new_left >= crop_right - 20:
        return crop_left

    # Check for text overlap (do not cut off question text)
    for other in text_items:
        if other is item:
            continue
        other_left = float(other["left"])
        other_top = float(other["top"])
        other_right = float(other["right"])
        other_bottom = float(other["bottom"])
        if other_right <= crop_left or other_left >= crop_right:
            continue
        if other_bottom <= crop_top or other_top >= crop_bottom:
            continue
        other_text = str(other["text"]).strip()
        if not other_text:
            continue
        # Check if other item is part of the question label (e.g. dot or spaces or digits)
        is_label_part = False
        if other_text in {".", ")", "-", "a", "b", "c", "d"} or other_text.isdigit():
            dist_x = abs(other_left - item_right)
            dist_y = abs(other_top - float(item["top"]))
            if dist_x < 20.0 and dist_y < 10.0:
                is_label_part = True
        if is_label_part or other_text.startswith(label_prefixes) or other_text == str(soru_no):
            continue
        # If this item starts to the left of new_left, shifting would cut it off!
        if other_left < new_left - 2.0:
            return crop_left

    return new_left


def mask_question_number_on_crop(
    job_dir: Path,
    row: dict[str, str | int],
    crop: Image.Image,
    bounds: dict[str, float],
    *,
    page_idx: int | None = None,
) -> None:
    soru_no = int(row.get("soru_no") or 0)
    if soru_no <= 0:
        return
    page_idx = int(row.get("page_idx") or 0) if page_idx is None else int(page_idx)
    crop_left = float(bounds["crop_left"])
    crop_top = float(bounds["crop_top"])
    crop_right = float(bounds["crop_right"])
    crop_bottom = float(bounds["crop_bottom"])
    scale_x = crop.width / max(1.0, crop_right - crop_left)
    scale_y = crop.height / max(1.0, crop_bottom - crop_top)
    label_prefixes = (f"{soru_no}.", f"{soru_no} .", f"{soru_no})")
    candidates = []
    for item in source_page_text_items(job_dir, row, page_idx):
        text = str(item["text"]).strip()
        if not text.startswith(label_prefixes) and text != str(soru_no):
            continue
        left = float(item["left"])
        top = float(item["top"])
        right = float(item["right"])
        bottom = float(item["bottom"])
        if right <= crop_left or left >= crop_right or bottom <= crop_top or top >= crop_bottom:
            continue
        candidates.append(item)

    if not candidates:
        return

    draw = ImageDraw.Draw(crop)

    item = min(candidates, key=lambda candidate: (abs(float(candidate["top"]) - crop_top), float(candidate["left"])))
    item_left = float(item["left"])
    item_top = float(item["top"])
    item_right = float(item["right"])
    item_bottom = float(item["bottom"])
    item_height = max(8.0, float(item["height"]))
    text = str(item["text"]).strip()
    if text.startswith(label_prefixes):
        is_pure_label = bool(re.match(r"^[0-9.\s()]+$", text))
        if not is_pure_label:
            digit_count = len(str(soru_no))
            item_right = min(item_right, item_left + item_height * (0.50 + digit_count * 0.30))
    pad = max(3.0, item_height * 0.18)
    left = max(0, int((item_left - pad - crop_left) * scale_x))
    top = max(0, int((item_top - pad - crop_top) * scale_y))
    right = min(crop.width, int((item_right + pad - crop_left) * scale_x))
    bottom = min(crop.height, int((item_bottom + pad - crop_top) * scale_y))
    if right > left and bottom > top:
        draw.rectangle((left, top, right, bottom), fill="white")


def row_common_stem_payload(row: dict[str, str | int]) -> dict[str, float | int | str] | None:
    try:
        page_idx = int(row.get("common_stem_page_idx"))
        bounds = {
            "crop_left": float(row["common_stem_left"]),
            "crop_top": float(row["common_stem_top"]),
            "crop_right": float(row["common_stem_right"]),
            "crop_bottom": float(row["common_stem_bottom"]),
        }
    except (TypeError, ValueError, KeyError):
        return None
    if bounds["crop_right"] <= bounds["crop_left"] + 24.0 or bounds["crop_bottom"] <= bounds["crop_top"] + 24.0:
        return None
    return {
        "page_idx": page_idx,
        "placement": "left" if str(row.get("common_stem_placement") or "") == "left" else "top",
        **bounds,
    }


def compose_question_image_from_row(job_id: str, row_id: int, *, dpi: int = 450) -> Image.Image:
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    question_bounds = {
        "crop_left": float(row["crop_left"]),
        "crop_top": float(row["crop_top"]),
        "crop_right": float(row["crop_right"]),
        "crop_bottom": float(row["crop_bottom"]),
    }

    if int(row.get("question_number_hidden") or 0):
        saved_anchor_right = row.get("anchor_right")
        if saved_anchor_right is not None:
            adjusted_left = float(saved_anchor_right)
        else:
            adjusted_left = get_adjusted_crop_left(job_dir, row, question_bounds)
            
        if adjusted_left > question_bounds["crop_left"]:
            question_bounds["crop_left"] = adjusted_left
            question_crop = crop_row_page_bounds_to_image(job_dir, row, int(row["page_idx"]), question_bounds, dpi=dpi)
        else:
            question_crop = crop_row_page_bounds_to_image(job_dir, row, int(row["page_idx"]), question_bounds, dpi=dpi)
            mask_question_number_on_crop(job_dir, row, question_crop, question_bounds)
    else:
        question_crop = crop_row_page_bounds_to_image(job_dir, row, int(row["page_idx"]), question_bounds, dpi=dpi)
        
    sub_crops = row.get("sub_crops") or []
    for sub in sub_crops:
        sub_bounds = {
            "crop_left": float(sub["crop_left"]),
            "crop_top": float(sub["crop_top"]),
            "crop_right": float(sub["crop_right"]),
            "crop_bottom": float(sub["crop_bottom"]),
        }
        sub_page = int(sub.get("page_idx") or row["page_idx"])
        sub_crop = crop_row_page_bounds_to_image(job_dir, row, sub_page, sub_bounds, dpi=dpi)
        placement = sub.get("placement") or "left"
        gap = max(18, int(dpi * 0.06))
        if placement == "left":
            canvas = Image.new("RGB", (question_crop.width + gap + sub_crop.width, max(question_crop.height, sub_crop.height)), "white")
            canvas.paste(question_crop, (0, 0))
            canvas.paste(sub_crop, (question_crop.width + gap, 0))
            question_crop = canvas
        else:
            canvas = Image.new("RGB", (max(question_crop.width, sub_crop.width), question_crop.height + gap + sub_crop.height), "white")
            canvas.paste(question_crop, (0, 0))
            canvas.paste(sub_crop, (0, question_crop.height + gap))
            question_crop = canvas

    common_stem = row_common_stem_payload(row)
    if common_stem is None:
        return question_crop

    stem_bounds = {
        "crop_left": float(common_stem["crop_left"]),
        "crop_top": float(common_stem["crop_top"]),
        "crop_right": float(common_stem["crop_right"]),
        "crop_bottom": float(common_stem["crop_bottom"]),
    }
    stem_crop = crop_row_page_bounds_to_image(job_dir, row, int(common_stem["page_idx"]), stem_bounds, dpi=dpi)
    gap = max(18, int(dpi * 0.06))
    if common_stem["placement"] == "left":
        canvas = Image.new("RGB", (stem_crop.width + gap + question_crop.width, max(stem_crop.height, question_crop.height)), "white")
        canvas.paste(stem_crop, (0, 0))
        canvas.paste(question_crop, (stem_crop.width + gap, 0))
        return canvas

    canvas = Image.new("RGB", (max(stem_crop.width, question_crop.width), stem_crop.height + gap + question_crop.height), "white")
    canvas.paste(stem_crop, (0, 0))
    canvas.paste(question_crop, (0, stem_crop.height + gap))
    return canvas


def source_crop_image(job_id: str, row_id: int) -> Path:
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    common_stem = row_common_stem_payload(row)
    
    saved_anchor_right = row.get("anchor_right")
    adjusted_left = float(row["crop_left"])
    if int(row.get("question_number_hidden") or 0):
        if saved_anchor_right is not None:
            adjusted_left = float(saved_anchor_right)
        else:
            adjusted_left = get_adjusted_crop_left(job_dir, row, {
                "crop_left": float(row["crop_left"]),
                "crop_top": float(row["crop_top"]),
                "crop_right": float(row["crop_right"]),
                "crop_bottom": float(row["crop_bottom"]),
            })
            
    bounds = {
        "crop_left": adjusted_left,
        "crop_top": float(row["crop_top"]),
        "crop_right": float(row["crop_right"]),
        "crop_bottom": float(row["crop_bottom"]),
    }
    sub_crops = row.get("sub_crops") or []
    digest = hashlib.sha1(
        json.dumps(
            {
                "row_id": row_id,
                "bounds": bounds,
                "question_number_hidden": int(row.get("question_number_hidden") or 0),
                "common_stem": common_stem,
                "sub_crops": sub_crops,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:20]
    cache_dir = job_dir / "preview_cache"
    ensure_dir(cache_dir)
    target_path = cache_dir / f"manual_crop_{digest}.png"
    if not target_path.exists():
        crop = crop_bounds_to_image(job_id, row_id, bounds, dpi=150)
        if int(row.get("question_number_hidden") or 0) and adjusted_left == float(row["crop_left"]):
            mask_question_number_on_crop(job_dir, row, crop, bounds)
        
        for sub in sub_crops:
            sub_bounds = {
                "crop_left": float(sub["crop_left"]),
                "crop_top": float(sub["crop_top"]),
                "crop_right": float(sub["crop_right"]),
                "crop_bottom": float(sub["crop_bottom"]),
            }
            sub_page = int(sub.get("page_idx") or row["page_idx"])
            sub_crop = crop_row_page_bounds_to_image(job_dir, row, sub_page, sub_bounds, dpi=150)
            placement = sub.get("placement") or "left"
            gap = max(12, int(150 * 0.06))
            if placement == "left":
                canvas = Image.new("RGB", (crop.width + gap + sub_crop.width, max(crop.height, sub_crop.height)), "white")
                canvas.paste(crop, (0, 0))
                canvas.paste(sub_crop, (crop.width + gap, 0))
            else:
                canvas = Image.new("RGB", (max(crop.width, sub_crop.width), crop.height + gap + sub_crop.height), "white")
                canvas.paste(crop, (0, 0))
                canvas.paste(sub_crop, (0, crop.height + gap))
            crop = canvas
            
        if common_stem is not None:
            stem_bounds = {
                "crop_left": float(common_stem["crop_left"]),
                "crop_top": float(common_stem["crop_top"]),
                "crop_right": float(common_stem["crop_right"]),
                "crop_bottom": float(common_stem["crop_bottom"]),
            }
            stem_crop = crop_row_page_bounds_to_image(job_id, row, int(common_stem["page_idx"]), stem_bounds, dpi=150)
            gap = max(12, int(150 * 0.06))
            if common_stem["placement"] == "left":
                canvas = Image.new("RGB", (stem_crop.width + gap + crop.width, max(stem_crop.height, crop.height)), "white")
                canvas.paste(stem_crop, (0, 0))
                canvas.paste(crop, (stem_crop.width + gap, 0))
            else:
                canvas = Image.new("RGB", (max(stem_crop.width, crop.width), stem_crop.height + gap + crop.height), "white")
                canvas.paste(stem_crop, (0, 0))
                canvas.paste(crop, (0, stem_crop.height + gap))
            crop = canvas
        crop.save(target_path)
    return target_path


def update_manifest_bounds(job_dir: Path, row: dict[str, str | int], bounds: dict[str, float]) -> None:
    out_dir = job_dir / "output"
    for manifest_path in out_dir.glob("*_crop_manifest.json"):
        manifest = read_json_file(manifest_path)
        if not isinstance(manifest, list):
            continue
        changed = False
        for entry in manifest:
            if not isinstance(entry, dict):
                continue
            if (
                str(entry.get("source_pdf") or "") == str(row.get("source_pdf") or "")
                and int(entry.get("test_no") or 0) == int(row.get("test_no") or 0)
                and int(entry.get("soru_no") or 0) == int(row.get("soru_no") or 0)
                and int(entry.get("page_idx") or 0) == int(row.get("page_idx") or 0)
            ):
                entry.update(bounds)
                method = str(entry.get("method") or "")
                if "manual-current" not in method:
                    entry["method"] = f"{method}+manual-current" if method else "manual-current"
                changed = True
        if changed:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def bulk_adjust_bounds(
    row: dict[str, str | int],
    operation: str,
    amount: float,
    *,
    page_width: float,
    page_height: float,
) -> dict[str, float] | None:
    amount = max(1.0, min(float(amount), 96.0))
    bounds = {
        "crop_left": float(row["crop_left"]),
        "crop_top": float(row["crop_top"]),
        "crop_right": float(row["crop_right"]),
        "crop_bottom": float(row["crop_bottom"]),
    }
    if operation == "bottom_expand":
        bounds["crop_bottom"] += amount
    elif operation == "bottom_shrink":
        bounds["crop_bottom"] -= amount
    elif operation == "top_expand":
        bounds["crop_top"] -= amount
    elif operation == "left_expand":
        bounds["crop_left"] -= amount
    elif operation == "right_expand":
        bounds["crop_right"] += amount
    else:
        return None

    bounds = {
        "crop_left": max(0.0, min(bounds["crop_left"], page_width)),
        "crop_top": max(0.0, min(bounds["crop_top"], page_height)),
        "crop_right": max(0.0, min(bounds["crop_right"], page_width)),
        "crop_bottom": max(0.0, min(bounds["crop_bottom"], page_height)),
    }
    if bounds["crop_right"] <= bounds["crop_left"] + 24.0 or bounds["crop_bottom"] <= bounds["crop_top"] + 24.0:
        return None
    return bounds


def update_manifest_common_stem(
    job_dir: Path,
    source_row: dict[str, str | int],
    target_soru_nos: list[int],
    bounds: dict[str, float],
    placement: str,
) -> None:
    out_dir = job_dir / "output"
    targets = {int(number) for number in target_soru_nos}
    for manifest_path in out_dir.glob("*_crop_manifest.json"):
        manifest = read_json_file(manifest_path)
        if not isinstance(manifest, list):
            continue
        changed = False
        for entry in manifest:
            if not isinstance(entry, dict):
                continue
            if (
                str(entry.get("source_pdf") or "") == str(source_row.get("source_pdf") or "")
                and int(entry.get("test_no") or 0) == int(source_row.get("test_no") or 0)
                and int(entry.get("soru_no") or 0) in targets
            ):
                entry["common_stem_page_idx"] = int(source_row.get("page_idx") or 0)
                entry["common_stem_left"] = float(bounds["crop_left"])
                entry["common_stem_top"] = float(bounds["crop_top"])
                entry["common_stem_right"] = float(bounds["crop_right"])
                entry["common_stem_bottom"] = float(bounds["crop_bottom"])
                entry["common_stem_placement"] = "left" if placement == "left" else "top"
                method = str(entry.get("method") or "")
                if "manual-common-stem" not in method:
                    entry["method"] = f"{method}+manual-common-stem" if method else "manual-common-stem"
                changed = True
        if changed:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def add_manifest_question(
    job_dir: Path,
    source_row: dict[str, str | int],
    soru_no: int,
    bounds: dict[str, float],
) -> str:
    out_dir = job_dir / "output"
    source_pdf = str(source_row.get("source_pdf") or "")
    test_no = int(source_row.get("test_no") or 0)
    test_name = str(source_row.get("test_name") or "")
    page_idx = int(source_row.get("page_idx") or 0)
    if soru_no <= 0:
        raise HTTPException(status_code=400, detail="Soru numarası geçersiz")

    source_question_pdf = str(source_row.get("question_pdf") or "").strip()
    source_question_dir = Path(source_question_pdf).parent
    if source_question_dir.name.endswith("_questions"):
        target_rel_path = (source_question_dir / f"{soru_no:02d}.pdf").as_posix()
    else:
        target_rel_path = f"{local_sanitize(test_name)}_questions/{soru_no:02d}.pdf"
    for manifest_path in sorted(out_dir.glob("*_crop_manifest.json")):
        manifest = read_json_file(manifest_path)
        if not isinstance(manifest, list):
            continue

        template: dict | None = None
        duplicate = False
        for entry in manifest:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("source_pdf") or "") != source_pdf or int(entry.get("test_no") or 0) != test_no:
                continue
            if int(entry.get("soru_no") or 0) == int(soru_no):
                duplicate = True
                break
            if template is None and str(entry.get("test_name") or "") == test_name:
                template = entry
        if duplicate:
            raise HTTPException(status_code=400, detail=f"Soru {soru_no:02d} zaten kayıtlı")
        if template is None:
            continue

        new_entry = dict(template)
        new_entry.update(
            {
                "soru_no": int(soru_no),
                "page_idx": page_idx,
                "crop_left": float(bounds["crop_left"]),
                "crop_top": float(bounds["crop_top"]),
                "crop_right": float(bounds["crop_right"]),
                "crop_bottom": float(bounds["crop_bottom"]),
                "method": "manual-added-question",
                "question_pdf": target_rel_path,
                "question_number_hidden": False,
                "common_stem_page_idx": None,
                "common_stem_left": None,
                "common_stem_top": None,
                "common_stem_right": None,
                "common_stem_bottom": None,
                "common_stem_placement": "top",
            }
        )
        manifest.append(new_entry)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return target_rel_path

    raise HTTPException(status_code=404, detail="Soru eklenecek manifest bulunamadı")


def update_manifest_question_number_hidden(job_dir: Path, row: dict[str, str | int], hidden: bool = True) -> None:
    out_dir = job_dir / "output"
    for manifest_path in out_dir.glob("*_crop_manifest.json"):
        manifest = read_json_file(manifest_path)
        if not isinstance(manifest, list):
            continue
        changed = False
        for entry in manifest:
            if not isinstance(entry, dict):
                continue
            if (
                str(entry.get("source_pdf") or "") == str(row.get("source_pdf") or "")
                and int(entry.get("test_no") or 0) == int(row.get("test_no") or 0)
                and int(entry.get("soru_no") or 0) == int(row.get("soru_no") or 0)
                and int(entry.get("page_idx") or 0) == int(row.get("page_idx") or 0)
            ):
                entry["question_number_hidden"] = bool(hidden)
                method = str(entry.get("method") or "")
                if hidden:
                    if "manual-hide-question-number" not in method:
                        entry["method"] = f"{method}+manual-hide-question-number" if method else "manual-hide-question-number"
                else:
                    if "+manual-hide-question-number" in method:
                        method = method.replace("+manual-hide-question-number", "")
                    elif "manual-hide-question-number+" in method:
                        method = method.replace("manual-hide-question-number+", "")
                    elif method == "manual-hide-question-number":
                        method = ""
                    entry["method"] = method
                changed = True
        if changed:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def review_focus_url(job_id: str, row_id: int | None, *, saved: bool = True) -> str:
    params = []
    if saved:
        params.append("saved=1")
    if row_id is not None and row_id >= 0:
        params.append(f"focus={int(row_id)}")
    query = f"?{'&'.join(params)}" if params else ""
    anchor = f"#row-{int(row_id)}" if row_id is not None and row_id >= 0 else ""
    return f"/jobs/{quote(job_id)}/review{query}{anchor}"


def number_hide_ops_dir(job_dir: Path) -> Path:
    path = job_dir / "number_hide_ops"
    ensure_dir(path)
    return path


def number_hide_op_path(job_dir: Path, op_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", op_id):
        raise HTTPException(status_code=404, detail="İşlem bulunamadı")
    return number_hide_ops_dir(job_dir) / f"{op_id}.json"


def write_number_hide_op(job_dir: Path, op_id: str, payload: dict[str, int | str]) -> None:
    number_hide_op_path(job_dir, op_id).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def read_number_hide_op(job_dir: Path, op_id: str) -> dict[str, int | str]:
    path = number_hide_op_path(job_dir, op_id)
    payload = read_json_file(path)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=404, detail="İşlem bulunamadı")
    return {
        "op_id": op_id,
        "status": str(payload.get("status") or "queued"),
        "message": str(payload.get("message") or ""),
        "current": str(payload.get("current") or ""),
        "done": int(payload.get("done") or 0),
        "total": int(payload.get("total") or 0),
        "progress_percent": int(payload.get("progress_percent") or 0),
        "redirect_url": str(payload.get("redirect_url") or ""),
        "error": str(payload.get("error") or ""),
    }


def run_number_hide_operation(job_id: str, op_id: str, selected: list[int], hidden: bool = True) -> None:
    action_verb = "gizleniyor" if hidden else "gösteriliyor"
    action_past = "gizlendi" if hidden else "gösterildi"
    action_fail = "gizlenemedi" if hidden else "gösterilemedi"

    try:
        job_dir, _ = get_active_job(job_id)
        rows = load_review_rows(job_dir / "output")
        valid_indices = [index for index in selected if 0 <= index < len(rows) and rows[index].get("question_pdf")]
        if not valid_indices:
            write_number_hide_op(job_dir, op_id, {
                "status": "error",
                "message": f"{'Gizlenecek' if hidden else 'Gösterilecektir'} geçerli soru bulunamadı",
                "current": "",
                "done": 0,
                "total": 0,
                "progress_percent": 100,
                "redirect_url": "",
                "error": f"{'Gizlenecek' if hidden else 'Gösterilecektir'} geçerli soru bulunamadı",
            })
            return

        total = len(valid_indices)
        first_focus = valid_indices[0]
        write_number_hide_op(job_dir, op_id, {
            "status": "processing",
            "message": f"Soru numaraları {action_verb}",
            "current": "Hazırlanıyor",
            "done": 0,
            "total": total,
            "progress_percent": 4,
            "redirect_url": "",
            "error": "",
        })
        for done, index in enumerate(valid_indices, start=1):
            current_row = load_review_rows(job_dir / "output")[index]
            label = f"{current_row.get('test_name') or 'Test'} - Soru {int(current_row.get('soru_no') or 0)}"
            write_number_hide_op(job_dir, op_id, {
                "status": "processing",
                "message": f"Soru numarası {action_verb}",
                "current": str(label),
                "done": done - 1,
                "total": total,
                "progress_percent": max(6, int(((done - 1) / total) * 90)),
                "redirect_url": "",
                "error": "",
            })
            update_manifest_question_number_hidden(job_dir, current_row, hidden)
            refresh_current_question_pdf(job_id, index)
            write_number_hide_op(job_dir, op_id, {
                "status": "processing",
                "message": f"Soru numarası {action_verb}",
                "current": str(label),
                "done": done,
                "total": total,
                "progress_percent": min(96, int((done / total) * 96)),
                "redirect_url": "",
                "error": "",
            })

        write_number_hide_op(job_dir, op_id, {
            "status": "completed",
            "message": f"Soru numaraları {action_past}",
            "current": "Tamamlandı",
            "done": total,
            "total": total,
            "progress_percent": 100,
            "redirect_url": review_focus_url(job_id, first_focus, saved=True),
            "error": "",
        })
    except Exception as exc:
        try:
            job_dir = safe_job_dir(job_id)
            write_number_hide_op(job_dir, op_id, {
                "status": "error",
                "message": f"Soru numaraları {action_fail}",
                "current": "",
                "done": 0,
                "total": 0,
                "progress_percent": 100,
                "redirect_url": "",
                "error": f"{type(exc).__name__}: {exc}",
            })
        except Exception:
            pass


def refresh_current_question_pdf(job_id: str, row_id: int) -> None:
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    question_pdf = str(row.get("question_pdf") or "")
    if not question_pdf:
        return
    out_dir = (job_dir / "output").resolve()
    pdf_path = (out_dir / question_pdf).resolve()
    if out_dir not in pdf_path.parents or pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="Dosya yolu geçersiz")
    crop = compose_question_image_from_row(job_id, row_id, dpi=450)
    ensure_dir(pdf_path.parent)
    crop.save(pdf_path, "PDF", resolution=450)

    cache_dir = job_dir / "preview_cache"
    old_preview = cache_dir / f"{hashlib.sha1(question_pdf.encode('utf-8')).hexdigest()[:20]}.png"
    old_preview.unlink(missing_ok=True)
    rebuild_test_pdf_from_questions(pdf_path.parent)


def rebuild_test_pdf_from_questions(question_dir: Path) -> None:
    if not question_dir.exists() or not question_dir.name.endswith("_questions"):
        return
    question_pdfs = sorted(question_dir.glob("*.pdf"))
    if not question_pdfs:
        return
    target_name = question_dir.name[: -len("_questions")] + "_pages.pdf"
    target_path = question_dir.parent / target_name
    writer = PdfWriter()
    for question_pdf in question_pdfs:
        reader = PdfReader(str(question_pdf))
        for page in reader.pages:
            writer.add_page(page)
    with target_path.open("wb") as handle:
        writer.write(handle)


def local_job_archive_dir(job_id: str) -> Path:
    target = LOCAL_JOBS_ROOT / job_id
    ensure_dir(target)
    return target


def archive_feedback_event(
    job_id: str,
    row: dict[str, str | int],
    issue_code: str,
    note: str,
    *,
    kind: str,
    row_id: int | None = None,
    bounds: dict[str, float] | None = None,
    common_stem: dict | None = None,
) -> None:
    if not FEEDBACK_MODE:
        return
    now = int(time.time())
    test_no = int(row.get("test_no") or 0)
    soru_no = int(row.get("soru_no") or 0)
    record_id = f"{now}-{test_no:02d}-{soru_no:02d}"
    folder_name = f"{now}_test{test_no:02d}_soru{soru_no:02d}_{local_sanitize(issue_code)}"
    archive_dir = local_job_archive_dir(job_id) / "feedback" / folder_name
    ensure_dir(archive_dir)

    image_files: list[str] = []
    question_pdf = str(row.get("question_pdf") or row.get("output_path") or "")
    if question_pdf:
        try:
            preview = preview_image_for_pdf(job_id, question_pdf)
            false_cut_path = archive_dir / f"false-cut-{record_id}.png"
            shutil.copy2(preview, false_cut_path)
            image_files.append(false_cut_path.name)
        except Exception as exc:
            (archive_dir / "false_cut_error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")

    if row_id is not None:
        try:
            page_path = archive_dir / f"page-{record_id}.png"
            page_image = source_page_image(job_id, row_id)
            shutil.copy2(page_image, page_path)
            image_files.append(page_path.name)
        except Exception as exc:
            (archive_dir / "page_error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")

    if row_id is not None and bounds:
        try:
            image = crop_bounds_to_image(job_id, row_id, bounds, dpi=150)
            true_cut_path = archive_dir / f"true-cut-{record_id}.png"
            image.save(true_cut_path)
            image_files.append(true_cut_path.name)
        except Exception as exc:
            (archive_dir / "true_cut_error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")

    report = {
        "record_id": record_id,
        "job_id": job_id,
        "created_at": now,
        "kind": kind,
        "issue_code": issue_code,
        "note": note.strip()[:500],
        "source_pdf": str(row.get("source_pdf") or ""),
        "test_no": test_no,
        "test_name": str(row.get("test_name") or ""),
        "soru_no": soru_no,
        "page_idx": int(row.get("page_idx") or 0),
        "profile_key": str(row.get("profile_key") or ""),
        "question_pdf": question_pdf,
        "false_cut": f"false-cut-{record_id}.png" if any(name == f"false-cut-{record_id}.png" for name in image_files) else "",
        "true_cut": f"true-cut-{record_id}.png" if any(name == f"true-cut-{record_id}.png" for name in image_files) else "",
        "page_image": f"page-{record_id}.png" if any(name == f"page-{record_id}.png" for name in image_files) else "",
        "bounds": bounds,
        "common_stem": common_stem,
        "images": image_files,
    }
    (archive_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (archive_dir / "report.md").write_text(
        "\n".join(
            [
                f"# Hata Kaydı - Test {test_no:02d} Soru {soru_no:02d}",
                "",
                f"- Kayıt id: `{record_id}`",
                f"- İş: `{job_id}`",
                f"- Kaynak PDF: `{report['source_pdf']}`",
                f"- Hata nedeni: `{issue_code}`",
                f"- Not: {report['note'] or '-'}",
                f"- Soru PDF: `{question_pdf or '-'}`",
                f"- Otomatik kesim: `{report['false_cut'] or '-'}`",
                f"- Doğru kesim: `{report['true_cut'] or '-'}`",
                f"- Sayfa görseli: `{report['page_image'] or '-'}`",
                f"- Ortak kök: {json.dumps(common_stem, ensure_ascii=False) if common_stem else '-'}",
                f"- Görseller: {', '.join(image_files) if image_files else '-'}",
            ]
        ),
        encoding="utf-8",
    )


def load_review_rows(out_dir: Path) -> list[dict[str, str | int]]:
    profiles = load_learning_profiles(out_dir)
    quality = load_quality_entries(out_dir)
    autofix_entries = load_autofix_entries(out_dir)
    manual_learning_entries = load_manual_learning_entries(out_dir)
    feedback_store = CropFeedbackStore()
    rows: list[dict[str, str | int]] = []

    for manifest_path in sorted(out_dir.glob("*_crop_manifest.json")):
        manifest = read_json_file(manifest_path)
        if not isinstance(manifest, list):
            continue
        for entry in manifest:
            if not isinstance(entry, dict):
                continue
            source_pdf = str(entry.get("source_pdf") or "")
            test_no = int(entry.get("test_no") or 0)
            soru_no = int(entry.get("soru_no") or 0)
            profile = profiles.get(source_pdf) or {}
            profile_key = str(profile.get("profile_key") or "")
            feedback_profile: dict[str, Any] = {}
            manual_bounds = None
            manual_bounds_version = ""
            common_stem = {
                "common_stem_page_idx": entry.get("common_stem_page_idx"),
                "common_stem_left": entry.get("common_stem_left"),
                "common_stem_top": entry.get("common_stem_top"),
                "common_stem_right": entry.get("common_stem_right"),
                "common_stem_bottom": entry.get("common_stem_bottom"),
                "common_stem_placement": entry.get("common_stem_placement") or "top",
            }
            anchor_right = entry.get("anchor_right")
            if profile_key:
                feedback_profile = feedback_store.get_profile(profile_key) or {}
                question_bounds = feedback_profile.get("question_bounds", {})
                if isinstance(question_bounds, dict):
                    bound_record = question_bounds.get(f"{test_no}:{soru_no}")
                    if isinstance(bound_record, dict) and int(bound_record.get("page_idx", entry.get("page_idx") or 0)) == int(entry.get("page_idx") or 0):
                        raw_bounds = bound_record.get("bounds")
                        if isinstance(raw_bounds, dict):
                            manual_bounds = raw_bounds
                            manual_bounds_version = str(bound_record.get("updated_at") or "")
                            if "anchor_right" in raw_bounds:
                                anchor_right = raw_bounds["anchor_right"]
                question_common_stems = feedback_profile.get("question_common_stems", {})
                if isinstance(question_common_stems, dict):
                    stem_record = question_common_stems.get(f"{test_no}:{soru_no}")
                    if isinstance(stem_record, dict):
                        raw_stem_bounds = stem_record.get("bounds")
                        if isinstance(raw_stem_bounds, dict):
                            common_stem = {
                                "common_stem_page_idx": stem_record.get("source_page_idx"),
                                "common_stem_left": raw_stem_bounds.get("crop_left"),
                                "common_stem_top": raw_stem_bounds.get("crop_top"),
                                "common_stem_right": raw_stem_bounds.get("crop_right"),
                                "common_stem_bottom": raw_stem_bounds.get("crop_bottom"),
                                "common_stem_placement": stem_record.get("placement") or "top",
                            }
            quality_entry = quality.get((source_pdf, test_no, soru_no), {})
            question_pdf = find_question_pdf(out_dir, entry)
            crop_left = float(entry.get("crop_left") or 0.0)
            crop_top = float(entry.get("crop_top") or 0.0)
            crop_right = float(entry.get("crop_right") or 0.0)
            crop_bottom = float(entry.get("crop_bottom") or 0.0)
            if manual_bounds:
                try:
                    crop_left = float(manual_bounds["crop_left"])
                    crop_top = float(manual_bounds["crop_top"])
                    crop_right = float(manual_bounds["crop_right"])
                    crop_bottom = float(manual_bounds["crop_bottom"])
                except (KeyError, TypeError, ValueError):
                    manual_bounds = None
            quality_score = int(quality_entry.get("score") or 0)
            quality_issues = str(quality_entry.get("issues") or "")
            quality_issue_codes = str(quality_entry.get("issue_codes") or "")
            row_key = (test_no, soru_no, int(entry.get("page_idx") or 0))
            autofix_entry = autofix_entries.get(row_key, {})
            manual_learning_entry = manual_learning_entries.get(row_key, {})
            compare_kind = ""
            if autofix_entry:
                compare_kind = "autofix"
            elif manual_learning_entry:
                compare_kind = "learning"
            profile_feedback_count, question_feedback_count = review_feedback_counts(
                feedback_profile,
                test_no,
                soru_no,
            )
            risk = review_risk_payload(
                quality_score,
                quality_issue_codes,
                bool(manual_bounds),
                profile_feedback_count=profile_feedback_count,
                question_feedback_count=question_feedback_count,
            )
            rows.append(
                {
                    "profile_key": profile_key,
                    "source_pdf": source_pdf,
                    "test_no": test_no,
                    "test_name": str(entry.get("test_name") or ""),
                    "soru_no": soru_no,
                    "page_idx": int(entry.get("page_idx") or 0),
                    "crop_left": crop_left,
                    "crop_top": crop_top,
                    "crop_right": crop_right,
                    "crop_bottom": crop_bottom,
                    "anchor_right": anchor_right,
                    "manual_bounds": 1 if manual_bounds else 0,
                    "manual_bounds_version": manual_bounds_version,
                    "question_number_hidden": 1 if entry.get("question_number_hidden") else 0,
                    "quality_score": quality_score,
                    "quality_issues": quality_issues,
                    "quality_issue_codes": quality_issue_codes,
                    "profile_feedback_count": profile_feedback_count,
                    "question_feedback_count": question_feedback_count,
                    "autofix": int(autofix_entry.get("autofix") or 0),
                    "autofix_reason": str(autofix_entry.get("autofix_reason") or ""),
                    "autofix_label": str(autofix_entry.get("autofix_label") or ""),
                    "manual_learning": int(manual_learning_entry.get("manual_learning") or 0),
                    "manual_learning_reasons": str(manual_learning_entry.get("manual_learning_reasons") or ""),
                    "manual_learning_label": str(manual_learning_entry.get("manual_learning_label") or ""),
                    "compare_kind": compare_kind,
                    **risk,
                    "question_pdf": question_pdf,
                    **common_stem,
                }
            )
    return rows


def issue_options(selected: str = "bottom_cut") -> str:
    labels = {
        "bottom_cut": "Alt kısım kesilmiş",
        "top_cut": "Üst kısım kesilmiş",
        "left_cut": "Sol kısım kesilmiş",
        "right_cut": "Sağ kısım kesilmiş",
        "next_question_leak": "Sonraki soru sızmış",
        "missing_common_stem": "Ortak kök eksik",
        "wrong_split": "Soru yanlış bölünmüş",
        "extra_blank": "Fazla boşluk var",
        "other": "Diğer",
    }
    return "".join(
        f'<option value="{html.escape(value)}"{" selected" if value == selected else ""}>{html.escape(label)}</option>'
        for value, label in labels.items()
    )


def review_page(
    job_id: str,
    meta: dict[str, int | str],
    saved: bool = False,
    focus_row_id: int | None = None,
) -> HTMLResponse:
    job_dir = safe_job_dir(job_id)
    out_dir = job_dir / "output"
    rows = load_review_rows(out_dir)
    if not rows:
        body = f"""
        <section class="hero">
          <span class="eyebrow">Kontrol</span>
          <h1>İşaretlenecek kayıt yok.</h1>
        </section>
        <section class="card">
          <div class="actions">
            <a class="btn ghost" href="/jobs/{quote(job_id)}">Sonuca dön</a>
          </div>
        </section>
        """
        return page_shell("Kesim Kontrol", body)

    saved_note = '<div class="note">Kaydedildi.</div>' if saved else ""
    grouped_items: dict[str, list[str]] = {}
    group_labels: dict[str, str] = {}
    group_row_ids: dict[str, set[int]] = {}
    group_first_row_ids: dict[str, int] = {}
    group_risk_counts: dict[str, dict[str, int]] = {}
    group_best_risk: dict[str, int] = {}
    risk_counts = {
        "critical": sum(1 for row in rows if str(row.get("risk_level") or "") == "critical"),
        "warning": sum(1 for row in rows if str(row.get("risk_level") or "") == "warning"),
        "clean": sum(1 for row in rows if str(row.get("risk_level") or "") == "clean"),
    }
    manual_count = sum(1 for row in rows if int(row.get("manual_bounds") or 0))
    autofix_count = sum(1 for row in rows if int(row.get("autofix") or 0))
    manual_learning_count = sum(1 for row in rows if int(row.get("manual_learning") or 0))
    number_visible_count = sum(1 for row in rows if not int(row.get("question_number_hidden") or 0))
    default_filter = "risk" if risk_counts["critical"] or risk_counts["warning"] else "all"
    ordered_rows = sorted(
        enumerate(rows),
        key=lambda item: (
            int(item[1].get("risk_rank") or 2),
            -int(item[1].get("risk_score") or 0),
            int(item[1].get("test_no") or 0),
            int(item[1].get("page_idx") or 0),
            int(item[1].get("soru_no") or 0),
        ),
    )
    for index, row in ordered_rows:
        preview = ""
        open_link = ""
        if row["question_pdf"]:
            if int(row.get("manual_bounds") or 0):
                preview_src = (
                    f"/jobs/{quote(job_id)}/preview/crop?row_id={index}"
                    f"&v={quote(str(row.get('manual_bounds_version') or 'manual'))}"
                )
            else:
                preview_src = f"/jobs/{quote(job_id)}/preview/image?path={quote(str(row['question_pdf']))}"
            href = f"/jobs/{quote(job_id)}/download/file?path={quote(str(row['question_pdf']))}"
            manual_badge = '<span class="pill" style="width:max-content;">Manuel sınır</span>' if int(row.get("manual_bounds") or 0) else ""
            preview = f"""
              <button type="button" class="preview-trigger" data-preview="{html.escape(preview_src)}" data-title="{html.escape(str(row['test_name']))} - Soru {int(row['soru_no'])}">
                <img src="{preview_src}" alt="Soru {int(row['soru_no'])} önizleme" loading="lazy">
              </button>
            """
            open_link = f'<a class="btn ghost compact" href="{href}" target="_blank" rel="noopener">PDF</a>'
        else:
            manual_badge = ""
        disabled = "" if row["profile_key"] else "disabled"
        profile_warning = "" if row["profile_key"] else '<span class="review-warning">Profil kaydı bulunamadı; bu kart öğrenmeye yazılamaz.</span>'
        editor_link = f'<a class="btn secondary compact" href="/jobs/{quote(job_id)}/crop-editor?row_id={index}">Sınır düzenle</a>' if row["profile_key"] else ""
        common_stem_link = f'<a class="btn secondary compact" href="/jobs/{quote(job_id)}/common-stem-editor?row_id={index}">Ortak kök</a>' if row["profile_key"] else ""
        page_editor_link = f'<a class="btn secondary compact" href="/jobs/{quote(job_id)}/page-editor?row_id={index}">Sayfa düzenle</a>' if row["profile_key"] else ""
        compare_kind = str(row.get("compare_kind") or "")
        compare_link = (
            f'<button type="button" class="btn ghost compact" data-preview="/jobs/{quote(job_id)}/preview/compare?row_id={index}&kind={html.escape(compare_kind)}" data-title="{html.escape(str(row["test_name"]))} - Soru {int(row["soru_no"])} fark">Fark</button>'
            if compare_kind
            else ""
        )
        group_key = f"{str(row.get('source_pdf') or '')}:{int(row.get('page_idx') or 0)}:{int(row.get('test_no') or 0)}"
        group_labels.setdefault(
            group_key,
            f"{str(row.get('test_name') or f'Test {int(row.get('test_no') or 0)}')} | Sayfa {int(row.get('page_idx') or 0) + 1}",
        )
        group_row_ids.setdefault(group_key, set()).add(index)
        group_first_row_ids.setdefault(group_key, index)
        risk_level = str(row.get("risk_level") or "clean")
        risk_rank = int(row.get("risk_rank") or 2)
        group_counts = group_risk_counts.setdefault(group_key, {"critical": 0, "warning": 0, "clean": 0})
        group_counts[risk_level] = int(group_counts.get(risk_level, 0)) + 1
        group_best_risk[group_key] = min(group_best_risk.get(group_key, risk_rank), risk_rank)
        risk_class = f"risk-{risk_level}"
        risk_label = str(row.get("risk_label") or REVIEW_RISK_LABELS.get(risk_level, "Temiz"))
        risk_reasons = str(row.get("risk_reasons") or "Temiz")
        manual_filter = "1" if int(row.get("manual_bounds") or 0) else "0"
        autofix_filter = "1" if int(row.get("autofix") or 0) else "0"
        manual_learning_filter = "1" if int(row.get("manual_learning") or 0) else "0"
        number_visible_filter = "0" if int(row.get("question_number_hidden") or 0) else "1"
        number_hidden_badge = '<span class="pill" style="width:max-content;">Numara gizli</span>' if int(row.get("question_number_hidden") or 0) else ""
        autofix_badge = (
            f'<span class="pill autofix-pill" style="width:max-content;">Otomatik düzeltme: {html.escape(str(row.get("autofix_label") or "Uygulandı"))}</span>'
            if int(row.get("autofix") or 0)
            else ""
        )
        manual_learning_badge = (
            f'<span class="pill learning-pill" style="width:max-content;">Öğrenme: {html.escape(str(row.get("manual_learning_label") or "Uygulandı"))}</span>'
            if int(row.get("manual_learning") or 0)
            else ""
        )
        toggle_label = "Numarayı Göster" if int(row.get("question_number_hidden") or 0) else "Numarayı Gizle"
        toggle_btn = f"""
        <form action="/jobs/{html.escape(job_id)}/feedback/toggle-number-visibility" method="post" style="display:inline; margin:0;">
          <input type="hidden" name="row_id" value="{index}">
          <button class="btn secondary compact" type="submit" {disabled}>{toggle_label}</button>
        </form>
        """ if row["question_pdf"] else ""

        grouped_items.setdefault(group_key, []).append(
            f"""
            <article
              class="review-card {risk_class}"
              id="row-{index}"
              data-review-card
              data-risk="{html.escape(risk_level)}"
              data-manual="{manual_filter}"
              data-autofix="{autofix_filter}"
              data-learning="{manual_learning_filter}"
              data-number-visible="{number_visible_filter}"
            >
              <label class="review-select">
                <input type="checkbox" name="row_id" value="{index}" {disabled}>
                <span>Seç</span>
              </label>
              {preview}
              <div class="review-meta">
                <strong>{html.escape(str(row['test_name']))} - Soru {int(row['soru_no'])}</strong>
                <span>Sayfa {int(row['page_idx']) + 1} | Kalite: {int(row['quality_score'])}</span>
                <span>{html.escape(str(row['quality_issues']))}</span>
                <span class="risk-pill {risk_class}">{html.escape(risk_label)} · Risk {int(row.get('risk_score') or 0)}/100: {html.escape(risk_reasons)}</span>
                {manual_badge}
                {autofix_badge}
                {manual_learning_badge}
                {number_hidden_badge}
                {profile_warning}
              </div>
              <div class="review-card-actions">
                {open_link}
                {compare_link}
                {page_editor_link}
                {editor_link}
                {common_stem_link}
                {toggle_btn}
                <form action="/jobs/{html.escape(job_id)}/feedback" method="post" style="display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0;">
                <input type="hidden" name="profile_key" value="{html.escape(str(row['profile_key']))}">
                <input type="hidden" name="source_pdf" value="{html.escape(str(row['source_pdf']))}">
                <input type="hidden" name="test_no" value="{int(row['test_no'])}">
                <input type="hidden" name="test_name" value="{html.escape(str(row['test_name']))}">
                <input type="hidden" name="soru_no" value="{int(row['soru_no'])}">
                <input type="hidden" name="page_idx" value="{int(row['page_idx'])}">
                <input type="hidden" name="row_id" value="{index}">
                <input type="hidden" name="output_path" value="{html.escape(str(row['question_pdf']))}">
                <input type="hidden" name="issue_code" value="bottom_cut">
                <button class="btn ghost compact" type="submit" {disabled}>Alt kesik</button>
              </form>
              </div>
            </article>
            """
        )

    accordion_sections = []
    opened_default = False
    sorted_group_items = sorted(grouped_items.items(), key=lambda item: (group_best_risk.get(item[0], 2), group_first_row_ids.get(item[0], 0)))
    for group_key, group_items in sorted_group_items:
        label = group_labels.get(group_key, group_key)
        focus_open = focus_row_id is not None and focus_row_id in group_row_ids.get(group_key, set())
        default_open = focus_row_id is None and (group_best_risk.get(group_key, 2) == 0 or not opened_default)
        open_attr = " open" if focus_open or default_open else ""
        if default_open:
            opened_default = True
        first_row_id = group_first_row_ids.get(group_key, -1)
        page_edit_action = (
            f'<div class="review-page-actions"><a class="btn secondary compact" href="/jobs/{quote(job_id)}/page-editor?row_id={first_row_id}">Bu sayfayı düzenle</a></div>'
            if first_row_id >= 0
            else ""
        )
        counts = group_risk_counts.get(group_key, {})
        group_summary = (
            f"{int(counts.get('critical', 0))} kritik, "
            f"{int(counts.get('warning', 0))} uyarı, "
            f"{int(counts.get('clean', 0))} temiz"
        )
        accordion_sections.append(
            f"""
            <details class="review-accordion"{open_attr} data-review-section>
              <summary>
                <span>{html.escape(label)}</span>
                <small>{html.escape(group_summary)}</small>
              </summary>
              {page_edit_action}
              <div class="review-grid">
                {''.join(group_items)}
              </div>
            </details>
            """
        )

    body = f"""
    <style>
      .review-toolbar {{
        position: sticky;
        top: 0;
        z-index: 5;
        display: grid;
        gap: 12px;
        padding: 14px;
        margin-bottom: 16px;
        border: 1px solid var(--line);
        border-radius: 18px;
        background: rgba(255, 252, 247, 0.96);
        box-shadow: 0 12px 28px rgba(42, 30, 16, 0.08);
      }}
      .review-toolbar-top {{
        display: grid;
        grid-template-columns: minmax(160px, 1fr) minmax(220px, 1.4fr) minmax(220px, 1.5fr) minmax(120px, 0.8fr);
        gap: 10px;
        align-items: stretch;
      }}
      .review-toolbar-actions {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        justify-content: center;
      }}
      .review-toolbar select,
      .review-toolbar input {{
        min-height: 46px;
        border-radius: 999px;
        border: 1px solid var(--line);
        padding: 0 12px;
        font: inherit;
        background: white;
        min-width: 0;
        width: 100%;
      }}
      .review-toolbar-actions button {{
        width: clamp(190px, 16vw, 260px);
      }}
      .review-toolbar-actions .bulk-primary {{
        margin-right: auto;
      }}
      .review-toolbar .toolbar-select-all {{
        min-height: 46px;
        border: 1px solid var(--line);
        border-radius: 999px;
        display: inline-flex;
        align-items: center;
        justify-content: flex-start;
        gap: 8px;
        padding: 0 12px;
        background: white;
      }}
      .review-toolbar .toolbar-select-all input {{
        width: 18px;
        height: 18px;
        margin: 0;
      }}
      .review-toolbar .toolbar-select-all span {{
        white-space: nowrap;
      }}
      .review-summary {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 10px;
        margin-bottom: 14px;
      }}
      .review-summary .metric {{
        min-height: 74px;
        border-radius: 8px;
      }}
      .review-filterbar {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
        gap: 8px;
        margin-bottom: 14px;
      }}
      .filter-chip {{
        min-height: 40px;
        padding: 0 14px;
        border: 1px solid var(--line);
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.86);
        color: var(--ink);
        font: inherit;
        font-weight: 700;
        cursor: pointer;
        text-align: center;
        width: 100%;
      }}
      .filter-chip.active {{
        background: var(--accent-2);
        color: white;
        border-color: var(--accent-2);
      }}
      .review-empty-filter {{
        display: none;
        margin-bottom: 14px;
      }}
      .review-empty-filter.visible {{
        display: block;
      }}
      .review-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
        gap: 14px;
      }}
      .review-accordion {{
        border: 1px solid var(--line);
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.74);
        overflow: hidden;
      }}
      .review-accordion + .review-accordion {{
        margin-top: 12px;
      }}
      .review-accordion.filtered-out,
      .review-card.filtered-out {{
        display: none;
      }}
      .review-accordion > summary {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        min-height: 58px;
        padding: 0 16px;
        cursor: pointer;
        color: var(--ink);
        font-weight: 700;
        list-style: none;
      }}
      .review-accordion > summary::-webkit-details-marker {{
        display: none;
      }}
      .review-accordion > summary::after {{
        content: "+";
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 auto;
        width: 30px;
        height: 30px;
        border-radius: 999px;
        background: rgba(36, 86, 122, 0.08);
        color: var(--accent-2);
      }}
      .review-accordion[open] > summary::after {{
        content: "-";
      }}
      .review-accordion > summary span {{
        min-width: 0;
        overflow-wrap: anywhere;
      }}
      .review-accordion > summary small {{
        margin-left: auto;
        color: var(--muted);
        white-space: nowrap;
      }}
      .review-accordion > .review-grid {{
        padding: 0 16px 16px;
      }}
      .review-page-actions {{
        display: flex;
        justify-content: flex-end;
        padding: 0 16px 12px;
      }}
      .review-card {{
        display: grid;
        gap: 10px;
        min-width: 0;
        padding: 12px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.9);
      }}
      .review-card.risk-critical {{
        border-color: rgba(142, 47, 29, 0.45);
        background: rgba(255, 247, 244, 0.96);
      }}
      .review-card.risk-warning {{
        border-color: rgba(154, 90, 16, 0.36);
        background: rgba(255, 251, 240, 0.96);
      }}
      .review-card.focused {{
        outline: 3px solid rgba(201, 101, 50, 0.45);
        box-shadow: 0 0 0 8px rgba(201, 101, 50, 0.10);
      }}
      .review-select {{
        display: inline-flex;
        gap: 8px;
        align-items: center;
        font-weight: 700;
      }}
      .preview-trigger {{
        display: block;
        width: 100%;
        aspect-ratio: 3 / 4;
        min-height: 0;
        padding: 0;
        overflow: hidden;
        border: 1px solid rgba(112, 82, 45, 0.14);
        border-radius: 8px;
        background: white;
        box-shadow: none;
      }}
      .preview-trigger img {{
        display: block;
        width: 100%;
        height: 100%;
        max-height: none;
        object-fit: contain;
        background: white;
      }}
      .review-meta {{
        display: grid;
        gap: 4px;
        min-height: 74px;
      }}
      .review-meta strong,
      .review-meta span {{
        overflow-wrap: anywhere;
      }}
      .review-meta span,
      .review-warning {{
        color: var(--muted);
        font-size: 0.9rem;
      }}
      .risk-pill {{
        display: inline-flex;
        width: max-content;
        max-width: 100%;
        padding: 5px 9px;
        border-radius: 999px;
        font-weight: 700;
        color: var(--muted);
        background: rgba(36, 86, 122, 0.08);
      }}
      .risk-pill.risk-critical {{
        color: var(--danger);
        background: rgba(142, 47, 29, 0.10);
      }}
      .risk-pill.risk-warning {{
        color: var(--warn);
        background: rgba(154, 90, 16, 0.12);
      }}
      .autofix-pill {{
        color: var(--accent-2);
        background: rgba(36, 86, 122, 0.10);
      }}
      .learning-pill {{
        color: var(--accent);
        background: rgba(201, 101, 50, 0.10);
      }}
      .review-card-actions {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
      }}
      .btn.compact,
      button.compact {{
        min-height: 40px;
        padding: 0 14px;
        font-size: 0.9rem;
      }}
      .preview-modal {{
        position: fixed;
        inset: 0;
        z-index: 20;
        display: none;
        align-items: center;
        justify-content: center;
        padding: 22px;
        background: rgba(23, 20, 17, 0.72);
      }}
      .preview-modal.open {{
        display: flex;
      }}
      .preview-dialog {{
        display: grid;
        grid-template-rows: auto minmax(0, 1fr);
        gap: 10px;
        width: min(980px, 96vw);
        max-height: 94vh;
        padding: 14px;
        border-radius: 8px;
        background: #fff;
      }}
      .preview-head {{
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: center;
      }}
      .preview-head strong {{
        overflow-wrap: anywhere;
      }}
      .preview-modal img {{
        max-width: 100%;
        max-height: calc(94vh - 92px);
        object-fit: contain;
        background: white;
        justify-self: center;
      }}
      @media (max-width: 860px) {{
        .review-toolbar {{
          position: static;
        }}
        .review-toolbar-top {{
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }}
        .review-toolbar-actions .bulk-primary {{
          margin-right: 0;
        }}
      }}
      @media (max-width: 640px) {{
        .review-toolbar-top {{
          grid-template-columns: 1fr;
        }}
        .review-toolbar-actions {{
          justify-content: stretch;
        }}
        .review-toolbar-actions button {{
          width: 100%;
        }}
      }}
    </style>
    <section class="hero">
      <span class="eyebrow">Kesim kontrolü</span>
      <h1>Kesimleri işaretle.</h1>
    </section>
	    <section class="card" data-job-keepalive="/jobs/{quote(job_id)}/touch" data-job-keepalive-fallback="/jobs/{quote(job_id)}">
	      {saved_note}
	      <div class="actions" style="margin-bottom:14px;">
	        <a class="btn" style="background-color:#10b981; color:white;" href="/jobs/{quote(job_id)}/easy-editor">Kolay Düzenleyici</a>
	        <a class="btn ghost" href="/jobs/{quote(job_id)}">Sonuca dön</a>
	        <a class="btn secondary" href="/">Yeni PDF kes</a>
	      </div>
          <div class="status" style="margin-bottom:14px;">
            <div>
              <p class="label">Silinmesine kalan süre</p>
              <div class="countdown" data-expires="{int(meta.get("expires_at") or 0)}" data-expire-redirect="/jobs/{quote(job_id)}">--:--</div>
              <p class="meta" data-expire-note>Kontrol ekranında kaldığınız sürece süre uzatılır.</p>
            </div>
          </div>
	      <div class="review-summary">
	        <div class="metric">
	          <p class="label">Kritik</p>
	          <strong>{risk_counts["critical"]}</strong>
	        </div>
	        <div class="metric">
	          <p class="label">Uyarı</p>
	          <strong>{risk_counts["warning"]}</strong>
	        </div>
	        <div class="metric">
	          <p class="label">Temiz</p>
	          <strong>{risk_counts["clean"]}</strong>
	        </div>
	        <div class="metric">
	          <p class="label">Manuel sınır</p>
	          <strong>{manual_count}</strong>
	        </div>
	        <div class="metric">
	          <p class="label">Otomatik düzeltme</p>
	          <strong>{autofix_count}</strong>
	        </div>
	        <div class="metric">
	          <p class="label">Öğrenme uygulandı</p>
	          <strong>{manual_learning_count}</strong>
	        </div>
	      </div>
	      <div class="review-filterbar" data-default-filter="{default_filter}">
	        <button class="filter-chip" type="button" data-filter="risk">Riskliler ({risk_counts["critical"] + risk_counts["warning"]})</button>
	        <button class="filter-chip" type="button" data-filter="critical">Kritik ({risk_counts["critical"]})</button>
	        <button class="filter-chip" type="button" data-filter="warning">Uyarı ({risk_counts["warning"]})</button>
	        <button class="filter-chip" type="button" data-filter="clean">Temiz ({risk_counts["clean"]})</button>
	        <button class="filter-chip" type="button" data-filter="manual">Manuel düzeltilmiş ({manual_count})</button>
	        <button class="filter-chip" type="button" data-filter="autofix">Otomatik ({autofix_count})</button>
	        <button class="filter-chip" type="button" data-filter="learning">Öğrenme ({manual_learning_count})</button>
	        <button class="filter-chip" type="button" data-filter="number-visible">Soru numarası gizlenmemiş ({number_visible_count})</button>
	        <button class="filter-chip" type="button" data-filter="all">Tümü ({len(rows)})</button>
	      </div>
	      <div class="note review-empty-filter" data-empty-filter>Bu filtrede gösterilecek soru yok.</div>
	      <form class="review-toolbar" action="/jobs/{quote(job_id)}/feedback/bulk" method="post">
          <div class="review-toolbar-top">
	        <label class="review-select toolbar-select-all">
	          <input type="checkbox" data-select-all>
	          <span>Tümünü seç</span>
	        </label>
	        <select name="issue_code">
	          {issue_options()}
	        </select>
	        <input name="note" placeholder="Toplu not">
	        <input name="amount" type="number" min="1" max="96" step="1" value="12" title="Sınır düzeltme pikseli">
          </div>
          <div class="review-toolbar-actions">
	        <button class="bulk-primary" type="submit" data-bulk-action="feedback">Seçilenleri işaretle</button>
	        <button
	          type="submit"
	          formaction="/jobs/{quote(job_id)}/feedback/bulk-bounds"
	          name="operation"
	          value="bottom_expand"
	          data-bulk-action="bounds"
	        >Altı genişlet</button>
	        <button
	          type="submit"
	          formaction="/jobs/{quote(job_id)}/feedback/bulk-bounds"
	          name="operation"
	          value="bottom_shrink"
	          data-bulk-action="bounds"
	        >Altı daralt</button>
	        <button
	          type="submit"
	          formaction="/jobs/{quote(job_id)}/feedback/bulk-bounds"
	          name="operation"
	          value="top_expand"
	          data-bulk-action="bounds"
	        >Üstü aç</button>
	        <button
	          type="submit"
	          formaction="/jobs/{quote(job_id)}/feedback/bulk-bounds"
	          name="operation"
	          value="left_expand"
	          data-bulk-action="bounds"
	        >Solu aç</button>
	        <button
	          type="submit"
	          formaction="/jobs/{quote(job_id)}/feedback/bulk-bounds"
	          name="operation"
	          value="right_expand"
	          data-bulk-action="bounds"
	        >Sağı aç</button>
	        <button
	          type="submit"
	          formaction="/jobs/{quote(job_id)}/feedback/hide-question-numbers"
          name="scope"
          value="selected"
          data-bulk-action="hide-selected"
        >Seçilen numaraları gizle</button>
        <button
          type="submit"
          formaction="/jobs/{quote(job_id)}/feedback/hide-question-numbers"
          name="scope"
          value="all"
          data-bulk-action="hide-all"
        >Tüm numaraları gizle</button>
        <button
          type="submit"
          formaction="/jobs/{quote(job_id)}/feedback/hide-question-numbers"
          name="scope"
          value="selected_show"
          data-bulk-action="show-selected"
        >Seçilen numaraları göster</button>
        <button
          type="submit"
          formaction="/jobs/{quote(job_id)}/feedback/hide-question-numbers"
          name="scope"
          value="all_show"
          data-bulk-action="show-all"
        >Tüm numaraları göster</button>
          </div>
      </form>
      <div class="review-accordion-list" data-review-grid>
        {''.join(accordion_sections)}
      </div>
    </section>
    <div class="preview-modal" data-preview-modal aria-hidden="true">
      <div class="preview-dialog">
        <div class="preview-head">
          <strong data-preview-title>Önizleme</strong>
          <button class="btn ghost compact" type="button" data-preview-close>Kapat</button>
        </div>
        <img data-preview-image alt="Soru önizleme">
      </div>
    </div>
    <script>
      (() => {{
	        const selectAll = document.querySelector("[data-select-all]");
	        const grid = document.querySelector("[data-review-grid]");
	        const bulkForm = document.querySelector(".review-toolbar");
	        const filterbar = document.querySelector("[data-default-filter]");
	        const filterButtons = Array.from(document.querySelectorAll("[data-filter]"));
	        const emptyFilter = document.querySelector("[data-empty-filter]");
	        const modal = document.querySelector("[data-preview-modal]");
	        const modalImage = document.querySelector("[data-preview-image]");
	        const modalTitle = document.querySelector("[data-preview-title]");
	        const modalClose = document.querySelector("[data-preview-close]");
	
	        const visibleCards = () => Array.from(document.querySelectorAll("[data-review-card]:not(.filtered-out)"));
	        const selectedBoxes = () => visibleCards()
	          .map((card) => card.querySelector('input[name="row_id"]:checked'))
	          .filter(Boolean);
	        const cardMatchesFilter = (card, filter) => {{
	          const risk = card.dataset.risk || "clean";
	          if (filter === "all") return true;
	          if (filter === "risk") return risk === "critical" || risk === "warning";
	          if (filter === "critical" || filter === "warning" || filter === "clean") return risk === filter;
	          if (filter === "manual") return card.dataset.manual === "1";
	          if (filter === "autofix") return card.dataset.autofix === "1";
	          if (filter === "learning") return card.dataset.learning === "1";
	          if (filter === "number-visible") return card.dataset.numberVisible === "1";
	          return true;
	        }};
	        const applyFilter = (filter) => {{
	          let visibleCount = 0;
	          document.querySelectorAll("[data-review-section]").forEach((section) => {{
	            let sectionVisible = 0;
	            section.querySelectorAll("[data-review-card]").forEach((card) => {{
	              const visible = cardMatchesFilter(card, filter);
	              card.classList.toggle("filtered-out", !visible);
	              if (!visible) {{
	                const box = card.querySelector('input[name="row_id"]');
	                if (box) box.checked = false;
	              }} else {{
	                sectionVisible += 1;
	                visibleCount += 1;
	              }}
	            }});
	            section.classList.toggle("filtered-out", sectionVisible === 0);
	            if (sectionVisible > 0 && filter !== "all" && (filter === "risk" || filter === "critical")) {{
	              section.open = true;
	            }}
	          }});
	          filterButtons.forEach((button) => {{
	            button.classList.toggle("active", button.dataset.filter === filter);
	          }});
	          if (selectAll) selectAll.checked = false;
	          emptyFilter?.classList.toggle("visible", visibleCount === 0);
	        }};
	
	        selectAll?.addEventListener("change", () => {{
	          visibleCards().forEach((card) => {{
	            const box = card.querySelector('input[name="row_id"]:not(:disabled)');
	            if (!box) return;
	            box.checked = selectAll.checked;
	          }});
	        }});
	        filterButtons.forEach((button) => {{
	          button.addEventListener("click", () => applyFilter(button.dataset.filter || "all"));
	        }});
	
	        bulkForm?.addEventListener("submit", (event) => {{
	          bulkForm.querySelectorAll('input[type="hidden"][data-bulk-row]').forEach((node) => node.remove());
	          const action = event.submitter?.dataset?.bulkAction || "feedback";
          const boxes = selectedBoxes();
          if (action !== "hide-all" && !boxes.length) {{
            event.preventDefault();
            alert("Önce en az bir soru seç.");
            return;
          }}
          boxes.forEach((box) => {{
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = "row_id";
            input.value = box.value;
            input.dataset.bulkRow = "1";
            bulkForm.appendChild(input);
          }});
        }});

	        grid?.addEventListener("click", (event) => {{
	          const trigger = event.target.closest("[data-preview]");
	          if (!trigger) return;
	          modalImage.src = trigger.dataset.preview || "";
          modalTitle.textContent = trigger.dataset.title || "Önizleme";
          modal.classList.add("open");
          modal.setAttribute("aria-hidden", "false");
        }});

        const closeModal = () => {{
          modal.classList.remove("open");
          modal.setAttribute("aria-hidden", "true");
          modalImage.removeAttribute("src");
        }};
        modalClose?.addEventListener("click", closeModal);
        modal?.addEventListener("click", (event) => {{
          if (event.target === modal) closeModal();
        }});
        document.addEventListener("keydown", (event) => {{
          if (event.key === "Escape" && modal?.classList.contains("open")) closeModal();
        }});

        const params = new URLSearchParams(window.location.search);
        const focusValue = params.get("focus") || (window.location.hash || "").replace("#row-", "");
        if (focusValue) {{
          const focusTarget = document.getElementById(`row-${{focusValue}}`);
          if (focusTarget) {{
            const section = focusTarget.closest("details");
            if (section) section.open = true;
            window.setTimeout(() => {{
              focusTarget.scrollIntoView({{ block: "center", inline: "nearest" }});
              focusTarget.classList.add("focused");
              window.setTimeout(() => focusTarget.classList.remove("focused"), 2200);
	            }}, 160);
	          }}
	        }}
	        applyFilter(focusValue ? "all" : (filterbar?.dataset.defaultFilter || "all"));
	      }})();
	    </script>
    """
    return page_shell("Kesim Kontrol", body)


def crop_editor_page(job_id: str, row_id: int) -> HTMLResponse:
    job_dir, meta = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    if not row.get("profile_key"):
        raise HTTPException(status_code=400, detail="Bu soru için öğrenme profili yok")

    layout = source_page_layout(job_id, row_id)
    page_width = float(layout["width_px"])
    page_height = float(layout["height_px"])
    image_src = f"/jobs/{quote(job_id)}/preview/source-page?row_id={row_id}"
    initial_bounds = {
        "crop_left": float(row["crop_left"]),
        "crop_top": float(row["crop_top"]),
        "crop_right": float(row["crop_right"]),
        "crop_bottom": float(row["crop_bottom"]),
    }

    body = f"""
    <style>
      .editor-shell {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) 320px;
        gap: 16px;
        align-items: start;
      }}
      @media (max-width: 900px) {{
        .editor-shell {{
          grid-template-columns: 1fr;
        }}
      }}
      .editor-stage {{
        overflow: auto;
        max-height: 82vh;
        padding: 12px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
      }}
      .editor-canvas {{
        position: relative;
        display: inline-block;
        min-width: 320px;
        max-width: 100%;
        cursor: crosshair;
        user-select: none;
      }}
      .editor-canvas img {{
        display: block;
        width: min(100%, 980px);
        height: auto;
        background: white;
      }}
      .crop-box {{
        position: absolute;
        border: 0.5px dashed #2f6df6;
        background: rgba(47, 109, 246, 0.06);
        box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.18);
        cursor: move;
        pointer-events: auto;
        touch-action: none;
      }}
      .crop-handle {{
        position: absolute;
        width: 14px;
        height: 14px;
        border: 2px solid #fff;
        border-radius: 3px;
        background: #2f6df6;
        box-shadow: 0 1px 6px rgba(0, 0, 0, 0.25);
      }}
      .crop-handle[data-handle="nw"] {{ left: -8px; top: -8px; cursor: nwse-resize; }}
      .crop-handle[data-handle="n"] {{ left: calc(50% - 7px); top: -8px; cursor: ns-resize; }}
      .crop-handle[data-handle="ne"] {{ right: -8px; top: -8px; cursor: nesw-resize; }}
      .crop-handle[data-handle="e"] {{ right: -8px; top: calc(50% - 7px); cursor: ew-resize; }}
      .crop-handle[data-handle="se"] {{ right: -8px; bottom: -8px; cursor: nwse-resize; }}
      .crop-handle[data-handle="s"] {{ left: calc(50% - 7px); bottom: -8px; cursor: ns-resize; }}
      .crop-handle[data-handle="sw"] {{ left: -8px; bottom: -8px; cursor: nesw-resize; }}
      .crop-handle[data-handle="w"] {{ left: -8px; top: calc(50% - 7px); cursor: ew-resize; }}
      .crop-actions {{
        position: absolute;
        left: 50%;
        top: 50%;
        z-index: 2;
        display: flex;
        gap: 10px;
        align-items: center;
        padding: 10px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.94);
        box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
        transform: translate(-50%, -50%);
      }}
      .crop-actions button,
      .crop-actions a {{
        min-height: 40px;
        padding: 0 14px;
        border-radius: 8px;
        white-space: nowrap;
        box-shadow: none;
      }}
      .crop-actions .accept {{
        background: #12b981;
      }}
      .crop-actions .cancel {{
        color: #fff;
        background: #ef4444;
      }}
      .editor-panel {{
        display: grid;
        gap: 12px;
      }}
      .editor-panel select,
      .editor-panel input {{
        min-height: 46px;
        border-radius: 999px;
        border: 1px solid var(--line);
        padding: 0 14px;
        font: inherit;
        background: white;
        width: 100%;
      }}
      .coord-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }}
      .nudge-panel {{
        display: grid;
        gap: 10px;
        padding: 12px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.72);
      }}
      .nudge-panel > summary {{
        cursor: pointer;
        color: var(--accent-2);
        font-weight: 700;
        list-style: none;
      }}
      .nudge-panel > summary::-webkit-details-marker {{
        display: none;
      }}
      .nudge-panel > summary::after {{
        content: "+";
        float: right;
        width: 28px;
        height: 28px;
        border-radius: 999px;
        background: rgba(36, 86, 122, 0.08);
        text-align: center;
        line-height: 28px;
      }}
      .nudge-panel[open] > summary::after {{
        content: "-";
      }}
      .nudge-panel-inner {{
        display: grid;
        gap: 10px;
        margin-top: 12px;
      }}
      .nudge-row {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 8px;
      }}
      .nudge-row button,
      .nudge-wide button {{
        min-height: 38px;
        padding: 0 10px;
        border-radius: 8px;
        box-shadow: none;
      }}
      .nudge-wide {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }}
      .step-field {{
        display: grid;
        gap: 4px;
        color: var(--muted);
        font-size: 0.86rem;
        font-weight: 700;
      }}
      .coord-grid label {{
        display: grid;
        gap: 4px;
        color: var(--muted);
        font-size: 0.86rem;
        font-weight: 700;
      }}
      @media (max-width: 960px) {{
        .editor-shell {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
    <section class="hero">
      <span class="eyebrow">Sınır düzenle</span>
      <h1>{html.escape(str(row['test_name']))} - Soru {int(row['soru_no'])}</h1>
    </section>
    <section class="editor-shell">
      <div class="editor-stage">
        <div class="editor-canvas" data-editor-canvas>
          <img src="{image_src}" alt="Orijinal sayfa" data-page-image draggable="false">
          <div class="crop-box" data-crop-box>
            <span class="crop-handle" data-handle="nw"></span>
            <span class="crop-handle" data-handle="n"></span>
            <span class="crop-handle" data-handle="ne"></span>
            <span class="crop-handle" data-handle="e"></span>
            <span class="crop-handle" data-handle="se"></span>
            <span class="crop-handle" data-handle="s"></span>
            <span class="crop-handle" data-handle="sw"></span>
            <span class="crop-handle" data-handle="w"></span>
            <div class="crop-actions">
              <button class="accept" type="submit" form="crop-form-{row_id}">Kabul Et</button>
              <a class="btn cancel" href="{review_focus_url(job_id, row_id, saved=False)}">İptal</a>
            </div>
          </div>
        </div>
      </div>
      <aside class="card editor-panel">
        <form id="crop-form-{row_id}" action="/jobs/{quote(job_id)}/feedback/bounds" method="post" data-bounds-form>
          <input type="hidden" name="row_id" value="{row_id}">
          <input type="hidden" name="crop_left" value="{initial_bounds['crop_left']:.2f}" data-bound-left>
          <input type="hidden" name="crop_top" value="{initial_bounds['crop_top']:.2f}" data-bound-top>
          <input type="hidden" name="crop_right" value="{initial_bounds['crop_right']:.2f}" data-bound-right>
          <input type="hidden" name="crop_bottom" value="{initial_bounds['crop_bottom']:.2f}" data-bound-bottom>
          <div class="stack">
            <div>
              <p class="label">Hata nedeni</p>
              <select name="issue_code">
                {issue_options()}
              </select>
            </div>
            <details class="nudge-panel">
              <summary>İnce ayar tuşları</summary>
              <div class="nudge-panel-inner">
                <label class="step-field">
                  Adım
                  <select data-nudge-step>
                    <option value="1">1 px</option>
                    <option value="5" selected>5 px</option>
                    <option value="20">20 px</option>
                  </select>
                </label>
                <div>
                  <p class="label">Kutuyu taşı</p>
                  <div class="nudge-row">
                    <span></span>
                    <button type="button" data-nudge="move-up">Yukarı</button>
                    <span></span>
                    <button type="button" data-nudge="move-left">Sol</button>
                    <button type="button" data-nudge="move-down">Aşağı</button>
                    <button type="button" data-nudge="move-right">Sağ</button>
                  </div>
                </div>
                <div>
                  <p class="label">Sınırı oynat</p>
                  <div class="nudge-wide">
                    <button type="button" data-nudge="grow-left">Solu aç</button>
                    <button type="button" data-nudge="shrink-left">Solu daralt</button>
                    <button type="button" data-nudge="grow-top">Üstü aç</button>
                    <button type="button" data-nudge="shrink-top">Üstü daralt</button>
                    <button type="button" data-nudge="grow-right">Sağı aç</button>
                    <button type="button" data-nudge="shrink-right">Sağı daralt</button>
                    <button type="button" data-nudge="grow-bottom">Altı aç</button>
                    <button type="button" data-nudge="shrink-bottom">Altı daralt</button>
                  </div>
                </div>
              </div>
            </details>
            <div>
              <p class="label">Not</p>
              <input name="note" placeholder="İsteğe bağlı">
            </div>
            <div>
              <p class="label">Seçilen sınır</p>
              <div class="coord-grid">
                <label>Sol <input readonly data-read-left></label>
                <label>Üst <input readonly data-read-top></label>
                <label>Sağ <input readonly data-read-right></label>
                <label>Alt <input readonly data-read-bottom></label>
              </div>
            </div>
            <button type="submit">Sınırı kaydet</button>
            <a class="btn ghost" href="{review_focus_url(job_id, row_id, saved=False)}">Kontrole dön</a>
          </div>
        </form>
      </aside>
    </section>
    <script>
      (() => {{
        const pageWidth = {page_width:.6f};
        const pageHeight = {page_height:.6f};
        const initial = {json.dumps(initial_bounds)};
        const canvas = document.querySelector("[data-editor-canvas]");
        const image = document.querySelector("[data-page-image]");
        const box = document.querySelector("[data-crop-box]");
        const fields = {{
          left: document.querySelector("[data-bound-left]"),
          top: document.querySelector("[data-bound-top]"),
          right: document.querySelector("[data-bound-right]"),
          bottom: document.querySelector("[data-bound-bottom]"),
        }};
        const reads = {{
          left: document.querySelector("[data-read-left]"),
          top: document.querySelector("[data-read-top]"),
          right: document.querySelector("[data-read-right]"),
          bottom: document.querySelector("[data-read-bottom]"),
        }};
        let bounds = {{ ...initial }};
        let activeMode = "";
        let activeHandle = "";
        let start = null;
        let startBounds = null;

        const imageRect = () => image.getBoundingClientRect();
        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const eventToPage = (event) => {{
          const rect = imageRect();
          const x = clamp(event.clientX - rect.left, 0, rect.width);
          const y = clamp(event.clientY - rect.top, 0, rect.height);
          return {{
            x: (x / rect.width) * pageWidth,
            y: (y / rect.height) * pageHeight,
          }};
        }};
        const normalize = (a, b) => ({{
          crop_left: Math.min(a.x, b.x),
          crop_top: Math.min(a.y, b.y),
          crop_right: Math.max(a.x, b.x),
          crop_bottom: Math.max(a.y, b.y),
        }});
        const render = () => {{
          const rect = imageRect();
          const left = (bounds.crop_left / pageWidth) * rect.width;
          const top = (bounds.crop_top / pageHeight) * rect.height;
          const right = (bounds.crop_right / pageWidth) * rect.width;
          const bottom = (bounds.crop_bottom / pageHeight) * rect.height;
          box.style.left = `${{left}}px`;
          box.style.top = `${{top}}px`;
          box.style.width = `${{Math.max(2, right - left)}}px`;
          box.style.height = `${{Math.max(2, bottom - top)}}px`;
          fields.left.value = bounds.crop_left.toFixed(2);
          fields.top.value = bounds.crop_top.toFixed(2);
          fields.right.value = bounds.crop_right.toFixed(2);
          fields.bottom.value = bounds.crop_bottom.toFixed(2);
          reads.left.value = fields.left.value;
          reads.top.value = fields.top.value;
          reads.right.value = fields.right.value;
          reads.bottom.value = fields.bottom.value;
        }};
        const validBounds = (next) => (
          next.crop_right - next.crop_left >= 24 &&
          next.crop_bottom - next.crop_top >= 24
        );
        const clampBounds = (next) => ({{
          crop_left: clamp(next.crop_left, 0, pageWidth),
          crop_top: clamp(next.crop_top, 0, pageHeight),
          crop_right: clamp(next.crop_right, 0, pageWidth),
          crop_bottom: clamp(next.crop_bottom, 0, pageHeight),
        }});
        const applyNudge = (action) => {{
          const step = Number(document.querySelector("[data-nudge-step]")?.value || "5");
          let next = {{ ...bounds }};
          if (action === "move-up") {{
            const delta = Math.min(step, next.crop_top);
            next.crop_top -= delta; next.crop_bottom -= delta;
          }} else if (action === "move-down") {{
            const delta = Math.min(step, pageHeight - next.crop_bottom);
            next.crop_top += delta; next.crop_bottom += delta;
          }} else if (action === "move-left") {{
            const delta = Math.min(step, next.crop_left);
            next.crop_left -= delta; next.crop_right -= delta;
          }} else if (action === "move-right") {{
            const delta = Math.min(step, pageWidth - next.crop_right);
            next.crop_left += delta; next.crop_right += delta;
          }} else if (action === "grow-left") {{
            next.crop_left -= step;
          }} else if (action === "shrink-left") {{
            next.crop_left += step;
          }} else if (action === "grow-top") {{
            next.crop_top -= step;
          }} else if (action === "shrink-top") {{
            next.crop_top += step;
          }} else if (action === "grow-right") {{
            next.crop_right += step;
          }} else if (action === "shrink-right") {{
            next.crop_right -= step;
          }} else if (action === "grow-bottom") {{
            next.crop_bottom += step;
          }} else if (action === "shrink-bottom") {{
            next.crop_bottom -= step;
          }}
          next = clampBounds(next);
          if (!validBounds(next)) return;
          bounds = next;
          render();
        }};
        const moveBounds = (origin, startPoint, currentPoint) => {{
          const width = origin.crop_right - origin.crop_left;
          const height = origin.crop_bottom - origin.crop_top;
          const left = clamp(origin.crop_left + (currentPoint.x - startPoint.x), 0, pageWidth - width);
          const top = clamp(origin.crop_top + (currentPoint.y - startPoint.y), 0, pageHeight - height);
          return {{
            crop_left: left,
            crop_top: top,
            crop_right: left + width,
            crop_bottom: top + height,
          }};
        }};
        const resizeBounds = (handle, origin, currentPoint) => {{
          let next = {{ ...origin }};
          if (handle.includes("w")) next.crop_left = currentPoint.x;
          if (handle.includes("e")) next.crop_right = currentPoint.x;
          if (handle.includes("n")) next.crop_top = currentPoint.y;
          if (handle.includes("s")) next.crop_bottom = currentPoint.y;
          next = clampBounds(next);
          return {{
            crop_left: Math.min(next.crop_left, next.crop_right),
            crop_top: Math.min(next.crop_top, next.crop_bottom),
            crop_right: Math.max(next.crop_left, next.crop_right),
            crop_bottom: Math.max(next.crop_top, next.crop_bottom),
          }};
        }};
        const startInteraction = (event, mode, handle = "") => {{
          event.preventDefault();
          activeMode = mode;
          activeHandle = handle;
          start = eventToPage(event);
          startBounds = {{ ...bounds }};
          canvas.setPointerCapture(event.pointerId);
        }};
        image.addEventListener("load", render);
        window.addEventListener("resize", render);
        document.querySelectorAll("[data-nudge]").forEach((button) => {{
          button.addEventListener("click", () => applyNudge(button.dataset.nudge || ""));
        }});
        canvas.addEventListener("pointerdown", (event) => {{
          if (event.target.closest(".crop-actions")) return;
          const handle = event.target.closest("[data-handle]");
          if (handle) {{
            startInteraction(event, "resize", handle.dataset.handle || "");
            return;
          }}
          if (event.target.closest("[data-crop-box]")) {{
            startInteraction(event, "move");
            return;
          }}
          activeMode = "draw";
          start = eventToPage(event);
          startBounds = null;
          bounds = normalize(start, start);
          canvas.setPointerCapture(event.pointerId);
          render();
        }});
        canvas.addEventListener("pointermove", (event) => {{
          if (!activeMode || !start) return;
          event.preventDefault();
          const currentPoint = eventToPage(event);
          if (activeMode === "draw") {{
            bounds = normalize(start, currentPoint);
          }} else if (activeMode === "move" && startBounds) {{
            bounds = moveBounds(startBounds, start, currentPoint);
          }} else if (activeMode === "resize" && startBounds) {{
            const next = resizeBounds(activeHandle, startBounds, currentPoint);
            if (validBounds(next)) bounds = next;
          }}
          render();
        }});
        canvas.addEventListener("pointerup", (event) => {{
          if (!activeMode || !start) return;
          if (activeMode === "draw") {{
            bounds = normalize(start, eventToPage(event));
          }}
          activeMode = "";
          activeHandle = "";
          start = null;
          startBounds = null;
          canvas.releasePointerCapture(event.pointerId);
          render();
        }});
        document.querySelector("[data-bounds-form]").addEventListener("submit", (event) => {{
          if ((bounds.crop_right - bounds.crop_left) < 24 || (bounds.crop_bottom - bounds.crop_top) < 24) {{
            event.preventDefault();
            alert("Seçilen kutu çok küçük.");
          }}
        }});
        if (image.complete) render();
      }})();
    </script>
    """
    return page_shell("Sınır Düzenle", body)


def common_stem_editor_page(job_id: str, row_id: int) -> HTMLResponse:
    job_dir, _meta = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    if not row.get("profile_key"):
        raise HTTPException(status_code=400, detail="Bu soru için öğrenme profili yok")

    layout = source_page_layout(job_id, row_id)
    page_width = float(layout["width_px"])
    page_height = float(layout["height_px"])
    image_src = f"/jobs/{quote(job_id)}/preview/source-page?row_id={row_id}"

    existing_stem = row_common_stem_payload(row)
    if existing_stem is not None and int(existing_stem["page_idx"]) == int(row["page_idx"]):
        initial_bounds = {
            "crop_left": float(existing_stem["crop_left"]),
            "crop_top": float(existing_stem["crop_top"]),
            "crop_right": float(existing_stem["crop_right"]),
            "crop_bottom": float(existing_stem["crop_bottom"]),
        }
        placement = str(existing_stem["placement"])
    else:
        crop_left = float(row["crop_left"])
        crop_top = float(row["crop_top"])
        crop_right = float(row["crop_right"])
        initial_bounds = {
            "crop_left": max(0.0, crop_left),
            "crop_top": max(0.0, crop_top - 220.0),
            "crop_right": min(page_width, max(crop_right, crop_left + 240.0)),
            "crop_bottom": max(24.0, crop_top - 8.0),
        }
        if initial_bounds["crop_bottom"] <= initial_bounds["crop_top"] + 24.0:
            initial_bounds["crop_top"] = max(0.0, crop_top)
            initial_bounds["crop_bottom"] = min(page_height, crop_top + 120.0)
        placement = "top"

    target_rows = [
        (index, target)
        for index, target in enumerate(rows)
        if str(target.get("source_pdf") or "") == str(row.get("source_pdf") or "")
        and int(target.get("test_no") or 0) == int(row.get("test_no") or 0)
    ]
    target_options = []
    existing_targets = {
        int(target.get("soru_no") or 0)
        for _index, target in target_rows
        if row_common_stem_payload(target) is not None
        and int((row_common_stem_payload(target) or {}).get("page_idx") or -1) == int(row["page_idx"])
    }
    if not existing_targets:
        existing_targets = {int(row.get("soru_no") or 0)}
    for _index, target in target_rows:
        soru_no = int(target.get("soru_no") or 0)
        checked = " checked" if soru_no in existing_targets else ""
        target_options.append(
            f"""
            <label class="target-option">
              <input type="checkbox" name="target_soru_no" value="{soru_no}"{checked}>
              <span>Soru {soru_no:02d}</span>
            </label>
            """
        )

    top_selected = " selected" if placement != "left" else ""
    left_selected = " selected" if placement == "left" else ""
    body = f"""
    <style>
      .editor-shell {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) 320px;
        gap: 16px;
        align-items: start;
      }}
      @media (max-width: 900px) {{
        .editor-shell {{
          grid-template-columns: 1fr;
        }}
      }}
      .editor-stage {{
        overflow: auto;
        max-height: 82vh;
        padding: 12px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
      }}
      .editor-canvas {{
        position: relative;
        display: inline-block;
        min-width: 320px;
        max-width: 100%;
        cursor: crosshair;
        user-select: none;
      }}
      .editor-canvas img {{
        display: block;
        width: min(100%, 980px);
        height: auto;
        background: white;
      }}
      .crop-box {{
        position: absolute;
        border: 2px dashed #c96532;
        background: rgba(201, 101, 50, 0.08);
        box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.18);
        cursor: move;
        pointer-events: auto;
        touch-action: none;
      }}
      .crop-handle {{
        position: absolute;
        width: 14px;
        height: 14px;
        border: 2px solid #fff;
        border-radius: 3px;
        background: #c96532;
        box-shadow: 0 1px 6px rgba(0, 0, 0, 0.25);
      }}
      .crop-handle[data-handle="nw"] {{ left: -8px; top: -8px; cursor: nwse-resize; }}
      .crop-handle[data-handle="n"] {{ left: calc(50% - 7px); top: -8px; cursor: ns-resize; }}
      .crop-handle[data-handle="ne"] {{ right: -8px; top: -8px; cursor: nesw-resize; }}
      .crop-handle[data-handle="e"] {{ right: -8px; top: calc(50% - 7px); cursor: ew-resize; }}
      .crop-handle[data-handle="se"] {{ right: -8px; bottom: -8px; cursor: nwse-resize; }}
      .crop-handle[data-handle="s"] {{ left: calc(50% - 7px); bottom: -8px; cursor: ns-resize; }}
      .crop-handle[data-handle="sw"] {{ left: -8px; bottom: -8px; cursor: nesw-resize; }}
      .crop-handle[data-handle="w"] {{ left: -8px; top: calc(50% - 7px); cursor: ew-resize; }}
      .crop-actions {{
        position: absolute;
        left: 50%;
        top: 50%;
        z-index: 2;
        display: flex;
        gap: 10px;
        align-items: center;
        padding: 10px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.94);
        box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
        transform: translate(-50%, -50%);
      }}
      .crop-actions button,
      .crop-actions a {{
        min-height: 40px;
        padding: 0 14px;
        border-radius: 8px;
        white-space: nowrap;
        box-shadow: none;
      }}
      .crop-actions .accept {{
        background: #12b981;
      }}
      .crop-actions .cancel {{
        color: #fff;
        background: #ef4444;
      }}
      .editor-panel {{
        display: grid;
        gap: 12px;
      }}
      .editor-panel select,
      .editor-panel input[type="text"] {{
        min-height: 46px;
        border-radius: 999px;
        border: 1px solid var(--line);
        padding: 0 14px;
        font: inherit;
        background: white;
        width: 100%;
      }}
      .target-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
      }}
      .target-option {{
        display: flex;
        gap: 6px;
        align-items: center;
        min-width: 0;
        min-height: 38px;
        padding: 8px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: white;
        font-size: 0.94rem;
        font-weight: 700;
      }}
      .target-option span {{
        min-width: 0;
        white-space: nowrap;
      }}
      .coord-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }}
      .coord-grid label {{
        display: grid;
        gap: 4px;
        color: var(--muted);
        font-size: 0.86rem;
        font-weight: 700;
      }}
      .coord-grid input {{
        min-height: 40px;
        border-radius: 8px;
        border: 1px solid var(--line);
        padding: 0 10px;
        background: white;
      }}
      .nudge-panel {{
        display: grid;
        gap: 10px;
        padding: 12px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.72);
      }}
      .nudge-panel > summary {{
        cursor: pointer;
        color: var(--accent-2);
        font-weight: 700;
        list-style: none;
      }}
      .nudge-panel > summary::-webkit-details-marker {{
        display: none;
      }}
      .nudge-panel > summary::after {{
        content: "+";
        float: right;
        width: 28px;
        height: 28px;
        border-radius: 999px;
        background: rgba(36, 86, 122, 0.08);
        text-align: center;
        line-height: 28px;
      }}
      .nudge-panel[open] > summary::after {{
        content: "-";
      }}
      .nudge-panel-inner {{
        display: grid;
        gap: 10px;
        margin-top: 12px;
      }}
      .nudge-row {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 8px;
      }}
      .nudge-row button,
      .nudge-wide button {{
        min-height: 38px;
        padding: 0 10px;
        border-radius: 8px;
        box-shadow: none;
      }}
      .nudge-wide {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }}
      .step-field {{
        display: grid;
        gap: 4px;
        color: var(--muted);
        font-size: 0.86rem;
        font-weight: 700;
      }}
      @media (max-width: 960px) {{
        .editor-shell {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
    <section class="hero">
      <span class="eyebrow">Ortak kök</span>
      <h1>{html.escape(str(row['test_name']))} - Soru {int(row['soru_no'])}</h1>
    </section>
    <section class="editor-shell">
      <div class="editor-stage">
        <div class="editor-canvas" data-editor-canvas>
          <img src="{image_src}" alt="Orijinal sayfa" data-page-image draggable="false">
          <div class="crop-box" data-crop-box>
            <span class="crop-handle" data-handle="nw"></span>
            <span class="crop-handle" data-handle="n"></span>
            <span class="crop-handle" data-handle="ne"></span>
            <span class="crop-handle" data-handle="e"></span>
            <span class="crop-handle" data-handle="se"></span>
            <span class="crop-handle" data-handle="s"></span>
            <span class="crop-handle" data-handle="sw"></span>
            <span class="crop-handle" data-handle="w"></span>
            <div class="crop-actions">
              <button class="accept" type="submit" form="common-stem-form-{row_id}">Kabul Et</button>
              <a class="btn cancel" href="{review_focus_url(job_id, row_id, saved=False)}">İptal</a>
            </div>
          </div>
        </div>
      </div>
      <aside class="card editor-panel">
        <form id="common-stem-form-{row_id}" action="/jobs/{quote(job_id)}/feedback/common-stem" method="post" data-common-stem-form>
          <input type="hidden" name="row_id" value="{row_id}">
          <input type="hidden" name="crop_left" value="{initial_bounds['crop_left']:.2f}" data-bound-left>
          <input type="hidden" name="crop_top" value="{initial_bounds['crop_top']:.2f}" data-bound-top>
          <input type="hidden" name="crop_right" value="{initial_bounds['crop_right']:.2f}" data-bound-right>
          <input type="hidden" name="crop_bottom" value="{initial_bounds['crop_bottom']:.2f}" data-bound-bottom>
          <div class="stack">
            <div>
              <p class="label">Yerleşim</p>
              <select name="placement">
                <option value="top"{top_selected}>Üstüne ekle</option>
                <option value="left"{left_selected}>Soluna ekle</option>
              </select>
            </div>
            <div>
              <p class="label">Hedef sorular</p>
              <div class="target-grid">
                {''.join(target_options)}
              </div>
            </div>
            <details class="nudge-panel">
              <summary>İnce ayar tuşları</summary>
              <div class="nudge-panel-inner">
                <label class="step-field">
                  Adım
                  <select data-nudge-step>
                    <option value="1">1 px</option>
                    <option value="5" selected>5 px</option>
                    <option value="20">20 px</option>
                  </select>
                </label>
                <div>
                  <p class="label">Kutuyu taşı</p>
                  <div class="nudge-row">
                    <span></span>
                    <button type="button" data-nudge="move-up">Yukarı</button>
                    <span></span>
                    <button type="button" data-nudge="move-left">Sol</button>
                    <button type="button" data-nudge="move-down">Aşağı</button>
                    <button type="button" data-nudge="move-right">Sağ</button>
                  </div>
                </div>
                <div>
                  <p class="label">Sınırı oynat</p>
                  <div class="nudge-wide">
                    <button type="button" data-nudge="grow-left">Solu aç</button>
                    <button type="button" data-nudge="shrink-left">Solu daralt</button>
                    <button type="button" data-nudge="grow-top">Üstü aç</button>
                    <button type="button" data-nudge="shrink-top">Üstü daralt</button>
                    <button type="button" data-nudge="grow-right">Sağı aç</button>
                    <button type="button" data-nudge="shrink-right">Sağı daralt</button>
                    <button type="button" data-nudge="grow-bottom">Altı aç</button>
                    <button type="button" data-nudge="shrink-bottom">Altı daralt</button>
                  </div>
                </div>
              </div>
            </details>
            <div>
              <p class="label">Not</p>
              <input type="text" name="note" placeholder="İsteğe bağlı">
            </div>
            <div>
              <p class="label">Seçilen ortak kök sınırı</p>
              <div class="coord-grid">
                <label>Sol <input readonly data-read-left></label>
                <label>Üst <input readonly data-read-top></label>
                <label>Sağ <input readonly data-read-right></label>
                <label>Alt <input readonly data-read-bottom></label>
              </div>
            </div>
            <button type="submit">Ortak kökü kaydet</button>
            <a class="btn ghost" href="{review_focus_url(job_id, row_id, saved=False)}">Kontrole dön</a>
          </div>
        </form>
      </aside>
    </section>
    <script>
      (() => {{
        const pageWidth = {page_width:.6f};
        const pageHeight = {page_height:.6f};
        const initial = {json.dumps(initial_bounds)};
        const canvas = document.querySelector("[data-editor-canvas]");
        const image = document.querySelector("[data-page-image]");
        const box = document.querySelector("[data-crop-box]");
        const fields = {{
          left: document.querySelector("[data-bound-left]"),
          top: document.querySelector("[data-bound-top]"),
          right: document.querySelector("[data-bound-right]"),
          bottom: document.querySelector("[data-bound-bottom]"),
        }};
        const reads = {{
          left: document.querySelector("[data-read-left]"),
          top: document.querySelector("[data-read-top]"),
          right: document.querySelector("[data-read-right]"),
          bottom: document.querySelector("[data-read-bottom]"),
        }};
        let bounds = {{ ...initial }};
        let activeMode = "";
        let activeHandle = "";
        let start = null;
        let startBounds = null;
        const imageRect = () => image.getBoundingClientRect();
        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const eventToPage = (event) => {{
          const rect = imageRect();
          const x = clamp(event.clientX - rect.left, 0, rect.width);
          const y = clamp(event.clientY - rect.top, 0, rect.height);
          return {{ x: (x / rect.width) * pageWidth, y: (y / rect.height) * pageHeight }};
        }};
        const normalize = (a, b) => ({{
          crop_left: Math.min(a.x, b.x),
          crop_top: Math.min(a.y, b.y),
          crop_right: Math.max(a.x, b.x),
          crop_bottom: Math.max(a.y, b.y),
        }});
        const render = () => {{
          const rect = imageRect();
          const left = (bounds.crop_left / pageWidth) * rect.width;
          const top = (bounds.crop_top / pageHeight) * rect.height;
          const right = (bounds.crop_right / pageWidth) * rect.width;
          const bottom = (bounds.crop_bottom / pageHeight) * rect.height;
          box.style.left = `${{left}}px`;
          box.style.top = `${{top}}px`;
          box.style.width = `${{Math.max(2, right - left)}}px`;
          box.style.height = `${{Math.max(2, bottom - top)}}px`;
          fields.left.value = bounds.crop_left.toFixed(2);
          fields.top.value = bounds.crop_top.toFixed(2);
          fields.right.value = bounds.crop_right.toFixed(2);
          fields.bottom.value = bounds.crop_bottom.toFixed(2);
          reads.left.value = fields.left.value;
          reads.top.value = fields.top.value;
          reads.right.value = fields.right.value;
          reads.bottom.value = fields.bottom.value;
        }};
        const validBounds = (next) => (
          next.crop_right - next.crop_left >= 24 &&
          next.crop_bottom - next.crop_top >= 24
        );
        const clampBounds = (next) => ({{
          crop_left: clamp(next.crop_left, 0, pageWidth),
          crop_top: clamp(next.crop_top, 0, pageHeight),
          crop_right: clamp(next.crop_right, 0, pageWidth),
          crop_bottom: clamp(next.crop_bottom, 0, pageHeight),
        }});
        const applyNudge = (action) => {{
          const step = Number(document.querySelector("[data-nudge-step]")?.value || "5");
          let next = {{ ...bounds }};
          if (action === "move-up") {{
            const delta = Math.min(step, next.crop_top);
            next.crop_top -= delta; next.crop_bottom -= delta;
          }} else if (action === "move-down") {{
            const delta = Math.min(step, pageHeight - next.crop_bottom);
            next.crop_top += delta; next.crop_bottom += delta;
          }} else if (action === "move-left") {{
            const delta = Math.min(step, next.crop_left);
            next.crop_left -= delta; next.crop_right -= delta;
          }} else if (action === "move-right") {{
            const delta = Math.min(step, pageWidth - next.crop_right);
            next.crop_left += delta; next.crop_right += delta;
          }} else if (action === "grow-left") {{
            next.crop_left -= step;
          }} else if (action === "shrink-left") {{
            next.crop_left += step;
          }} else if (action === "grow-top") {{
            next.crop_top -= step;
          }} else if (action === "shrink-top") {{
            next.crop_top += step;
          }} else if (action === "grow-right") {{
            next.crop_right += step;
          }} else if (action === "shrink-right") {{
            next.crop_right -= step;
          }} else if (action === "grow-bottom") {{
            next.crop_bottom += step;
          }} else if (action === "shrink-bottom") {{
            next.crop_bottom -= step;
          }}
          next = clampBounds(next);
          if (!validBounds(next)) return;
          bounds = next;
          render();
        }};
        const moveBounds = (origin, startPoint, currentPoint) => {{
          const width = origin.crop_right - origin.crop_left;
          const height = origin.crop_bottom - origin.crop_top;
          const left = clamp(origin.crop_left + (currentPoint.x - startPoint.x), 0, pageWidth - width);
          const top = clamp(origin.crop_top + (currentPoint.y - startPoint.y), 0, pageHeight - height);
          return {{
            crop_left: left,
            crop_top: top,
            crop_right: left + width,
            crop_bottom: top + height,
          }};
        }};
        const resizeBounds = (handle, origin, currentPoint) => {{
          let next = {{ ...origin }};
          if (handle.includes("w")) next.crop_left = currentPoint.x;
          if (handle.includes("e")) next.crop_right = currentPoint.x;
          if (handle.includes("n")) next.crop_top = currentPoint.y;
          if (handle.includes("s")) next.crop_bottom = currentPoint.y;
          next = clampBounds(next);
          return {{
            crop_left: Math.min(next.crop_left, next.crop_right),
            crop_top: Math.min(next.crop_top, next.crop_bottom),
            crop_right: Math.max(next.crop_left, next.crop_right),
            crop_bottom: Math.max(next.crop_top, next.crop_bottom),
          }};
        }};
        const startInteraction = (event, mode, handle = "") => {{
          event.preventDefault();
          activeMode = mode;
          activeHandle = handle;
          start = eventToPage(event);
          startBounds = {{ ...bounds }};
          canvas.setPointerCapture(event.pointerId);
        }};
        document.querySelectorAll("[data-nudge]").forEach((button) => {{
          button.addEventListener("click", () => applyNudge(button.dataset.nudge || ""));
        }});
        canvas.addEventListener("pointerdown", (event) => {{
          if (event.target.closest(".crop-actions")) return;
          const handle = event.target.closest("[data-handle]");
          if (handle) {{
            startInteraction(event, "resize", handle.dataset.handle || "");
            return;
          }}
          if (event.target.closest("[data-crop-box]")) {{
            startInteraction(event, "move");
            return;
          }}
          activeMode = "draw";
          start = eventToPage(event);
          startBounds = null;
          bounds = normalize(start, start);
          canvas.setPointerCapture(event.pointerId);
          render();
        }});
        canvas.addEventListener("pointermove", (event) => {{
          if (!activeMode || !start) return;
          event.preventDefault();
          const currentPoint = eventToPage(event);
          if (activeMode === "draw") {{
            bounds = normalize(start, currentPoint);
          }} else if (activeMode === "move" && startBounds) {{
            bounds = moveBounds(startBounds, start, currentPoint);
          }} else if (activeMode === "resize" && startBounds) {{
            const next = resizeBounds(activeHandle, startBounds, currentPoint);
            if (validBounds(next)) bounds = next;
          }}
          render();
        }});
        canvas.addEventListener("pointerup", (event) => {{
          if (!activeMode || !start) return;
          if (activeMode === "draw") {{
            bounds = normalize(start, eventToPage(event));
          }}
          activeMode = "";
          activeHandle = "";
          start = null;
          startBounds = null;
          canvas.releasePointerCapture(event.pointerId);
          render();
        }});
        document.querySelector("[data-common-stem-form]").addEventListener("submit", (event) => {{
          const selected = document.querySelectorAll('input[name="target_soru_no"]:checked').length;
          if ((bounds.crop_right - bounds.crop_left) < 24 || (bounds.crop_bottom - bounds.crop_top) < 24) {{
            event.preventDefault();
            alert("Seçilen ortak kök kutusu çok küçük.");
            return;
          }}
          if (selected < 1) {{
            event.preventDefault();
            alert("En az bir hedef soru seç.");
          }}
        }});
        if (image.complete) render();
        image.addEventListener("load", render);
        window.addEventListener("resize", render);
      }})();
    </script>
    """
    return page_shell("Ortak Kök", body)


def page_editor_url(job_id: str, row_id: int, *, saved: bool = False) -> str:
    query = f"?row_id={int(row_id)}"
    if saved:
        query += "&saved=1"
    return f"/jobs/{quote(job_id)}/page-editor{query}"


def _page_editor_bounds(row: dict[str, str | int]) -> dict[str, float]:
    return {
        "crop_left": float(row["crop_left"]),
        "crop_top": float(row["crop_top"]),
        "crop_right": float(row["crop_right"]),
        "crop_bottom": float(row["crop_bottom"]),
    }


def page_editor_page(job_id: str, row_id: int, saved: bool = False) -> HTMLResponse:
    job_dir, _meta = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    source_row = rows[row_id]
    if not source_row.get("profile_key"):
        raise HTTPException(status_code=400, detail="Bu sayfa için öğrenme profili yok")

    source_pdf = str(source_row.get("source_pdf") or "")
    page_idx = int(source_row.get("page_idx") or 0)
    layout = source_page_layout(job_id, row_id)
    page_width = float(layout["width_px"])
    page_height = float(layout["height_px"])
    image_src = f"/jobs/{quote(job_id)}/preview/source-page?row_id={row_id}"

    page_rows = [
        (index, row)
        for index, row in enumerate(rows)
        if str(row.get("source_pdf") or "") == source_pdf
        and int(row.get("page_idx") or 0) == page_idx
        and row.get("profile_key")
    ]
    if not page_rows:
        raise HTTPException(status_code=404, detail="Sayfada düzenlenebilir soru bulunamadı")

    questions = []
    for index, row in page_rows:
        questions.append(
            {
                "id": f"q-{index}",
                "kind": "question",
                "rowId": index,
                "testNo": int(row.get("test_no") or 0),
                "testName": str(row.get("test_name") or ""),
                "soruNo": int(row.get("soru_no") or 0),
                "label": f"Soru {int(row.get('soru_no') or 0):02d}",
                "bounds": _page_editor_bounds(row),
                "issueCode": "bottom_cut",
                "dirty": False,
            }
        )

    existing_question_numbers = sorted(
        {
            int(row.get("soru_no") or 0)
            for row in rows
            if str(row.get("source_pdf") or "") == source_pdf
            and int(row.get("test_no") or 0) == int(source_row.get("test_no") or 0)
            and int(row.get("soru_no") or 0) > 0
        }
    )

    source_row_by_test: dict[int, int] = {}
    for index, row in page_rows:
        source_row_by_test.setdefault(int(row.get("test_no") or 0), index)

    stem_groups: dict[str, dict] = {}
    for index, row in page_rows:
        stem = row_common_stem_payload(row)
        if stem is None or int(stem.get("page_idx") or -1) != page_idx:
            continue
        bounds = {
            "crop_left": float(stem["crop_left"]),
            "crop_top": float(stem["crop_top"]),
            "crop_right": float(stem["crop_right"]),
            "crop_bottom": float(stem["crop_bottom"]),
        }
        test_no = int(row.get("test_no") or 0)
        key = json.dumps(
            {
                "test_no": test_no,
                "placement": str(stem.get("placement") or "top"),
                "bounds": {name: round(value, 2) for name, value in bounds.items()},
            },
            sort_keys=True,
        )
        group = stem_groups.setdefault(
            key,
            {
                "id": f"s-{len(stem_groups)}",
                "kind": "stem",
                "sourceRowId": source_row_by_test.get(test_no, index),
                "testNo": test_no,
                "testName": str(row.get("test_name") or ""),
                "label": "Ortak kök",
                "bounds": bounds,
                "placement": str(stem.get("placement") or "top"),
                "targets": [],
                "dirty": False,
                "isNew": False,
            },
        )
        soru_no = int(row.get("soru_no") or 0)
        if soru_no not in group["targets"]:
            group["targets"].append(soru_no)

    stems = []
    for group in stem_groups.values():
        group["targets"] = sorted(group["targets"])
        group["label"] = "Ortak kök " + ", ".join(f"{number:02d}" for number in group["targets"])
        stems.append(group)

    editor_data = {
        "pageWidth": page_width,
        "pageHeight": page_height,
        "pageNo": page_idx + 1,
        "sourcePdf": source_pdf,
        "returnUrl": review_focus_url(job_id, row_id, saved=False),
        "questions": questions,
        "stems": stems,
        "activeTestNo": int(source_row.get("test_no") or 0),
        "existingQuestionNumbers": existing_question_numbers,
    }
    saved_note = '<div class="note">Sayfa düzenlemeleri kaydedildi.</div>' if saved else ""
    body = f"""
    <style>
      .page-editor-shell {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) 360px;
        gap: 16px;
        align-items: start;
      }}
      @media (max-width: 900px) {{
        .page-editor-shell {{
          grid-template-columns: 1fr;
        }}
      }}
      .page-editor-stage {{
        overflow: auto;
        max-height: 84vh;
        padding: 12px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
      }}
      .page-editor-canvas {{
        position: relative;
        display: inline-block;
        min-width: 320px;
        max-width: 100%;
        user-select: none;
      }}
      .page-editor-canvas img {{
        display: block;
        width: min(100%, 1040px);
        height: auto;
        background: white;
      }}
      .page-box {{
        position: absolute;
        border: 2px solid #2f6df6;
        border-radius: 2px;
        background: rgba(47, 109, 246, 0.07);
        cursor: move;
        touch-action: none;
      }}
      .page-box.stem {{
        border-color: #12b981;
        background: rgba(18, 185, 129, 0.10);
      }}
      .page-box.active {{
        z-index: 4;
        outline: 3px solid rgba(201, 101, 50, 0.45);
      }}
      .page-box-label {{
        position: absolute;
        left: 0;
        top: -28px;
        max-width: 220px;
        padding: 4px 7px;
        border-radius: 6px;
        background: #172033;
        color: #fff;
        font-size: 0.78rem;
        font-weight: 800;
        line-height: 1.1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }}
      .box-handle {{
        position: absolute;
        width: 12px;
        height: 12px;
        border: 2px solid #fff;
        border-radius: 3px;
        background: #c96532;
        box-shadow: 0 1px 6px rgba(0, 0, 0, 0.25);
      }}
      .box-handle[data-handle="nw"] {{ left: -8px; top: -8px; cursor: nwse-resize; }}
      .box-handle[data-handle="n"] {{ left: calc(50% - 6px); top: -8px; cursor: ns-resize; }}
      .box-handle[data-handle="ne"] {{ right: -8px; top: -8px; cursor: nesw-resize; }}
      .box-handle[data-handle="e"] {{ right: -8px; top: calc(50% - 6px); cursor: ew-resize; }}
      .box-handle[data-handle="se"] {{ right: -8px; bottom: -8px; cursor: nwse-resize; }}
      .box-handle[data-handle="s"] {{ left: calc(50% - 6px); bottom: -8px; cursor: ns-resize; }}
      .box-handle[data-handle="sw"] {{ left: -8px; bottom: -8px; cursor: nesw-resize; }}
      .box-handle[data-handle="w"] {{ left: -8px; top: calc(50% - 6px); cursor: ew-resize; }}
      .page-editor-panel {{
        display: grid;
        gap: 12px;
      }}
      .page-tool-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }}
      .page-tool-row button,
      .page-tool-row a {{
        min-height: 40px;
        padding: 0 12px;
        border-radius: 8px;
      }}
      .page-object-list {{
        display: grid;
        gap: 8px;
        max-height: 260px;
        overflow: auto;
      }}
      .page-object {{
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 8px;
        align-items: center;
        min-height: 42px;
        padding: 8px 10px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
        color: var(--ink);
        text-align: left;
        box-shadow: none;
      }}
      .page-object.active {{
        border-color: #c96532;
        background: rgba(201, 101, 50, 0.08);
      }}
      .page-object.new {{
        border-color: rgba(18, 185, 129, 0.55);
      }}
      .page-object span {{
        min-width: 0;
        overflow-wrap: anywhere;
      }}
      .page-object small {{
        color: var(--muted);
        white-space: nowrap;
      }}
      .page-field-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }}
      .page-editor-panel input,
      .page-editor-panel select {{
        min-height: 40px;
        border-radius: 8px;
        border: 1px solid var(--line);
        padding: 0 10px;
        font: inherit;
        background: white;
        width: 100%;
      }}
      .target-list {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }}
      .target-list label {{
        display: flex;
        gap: 6px;
        align-items: center;
        min-height: 36px;
        padding: 7px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
        font-weight: 700;
      }}
      .target-list input {{
        width: auto;
        min-height: auto;
      }}
      .page-hidden {{
        display: none;
      }}
      @media (max-width: 1020px) {{
        .page-editor-shell {{
          grid-template-columns: 1fr;
        }}
        .page-editor-stage {{
          max-height: 70vh;
        }}
      }}
    </style>
    <section class="hero">
      <span class="eyebrow">Sayfa düzenle</span>
      <h1>{html.escape(str(source_row.get('test_name') or 'Test'))} | Sayfa {page_idx + 1}</h1>
    </section>
    <section class="page-editor-shell">
      <div class="page-editor-stage">
        <div class="page-editor-canvas" data-page-canvas>
          <img src="{image_src}" alt="Orijinal sayfa" data-page-image draggable="false">
        </div>
      </div>
      <aside class="card page-editor-panel">
        {saved_note}
        <div class="page-tool-row">
          <button type="button" data-add-question>Yeni soru</button>
          <button type="button" data-add-stem>Yeni ortak kök</button>
          <a class="btn ghost" href="{review_focus_url(job_id, row_id, saved=False)}">Kontrole dön</a>
        </div>
        <div>
          <p class="label">Sayfadaki kutular</p>
          <div class="page-object-list" data-object-list></div>
        </div>
        <div data-active-panel>
          <p class="label">Seçilen</p>
          <strong data-active-title>-</strong>
          <div class="stack" style="margin-top:10px;">
            <div data-question-fields>
              <p class="label">Hata nedeni</p>
              <select data-issue-code>
                {issue_options()}
              </select>
              <div data-new-question-fields class="page-hidden" style="margin-top:10px;">
                <p class="label">Soru no</p>
                <input type="number" min="1" step="1" data-new-question-no>
              </div>
            </div>
            <div data-stem-fields class="page-hidden">
              <p class="label">Yerleşim</p>
              <select data-placement>
                <option value="top">Üstüne ekle</option>
                <option value="left">Soluna ekle</option>
              </select>
              <p class="label" style="margin-top:10px;">Hedef sorular</p>
              <div class="target-list" data-target-list></div>
            </div>
            <div>
              <p class="label">Sınır</p>
              <div class="page-field-grid">
                <label>Sol <input readonly data-read-left></label>
                <label>Üst <input readonly data-read-top></label>
                <label>Sağ <input readonly data-read-right></label>
                <label>Alt <input readonly data-read-bottom></label>
              </div>
            </div>
          </div>
        </div>
        <form action="/jobs/{quote(job_id)}/feedback/page-edits" method="post" data-page-edit-form>
          <input type="hidden" name="row_id" value="{row_id}">
          <input type="hidden" name="edits_json" data-edits-json>
          <div class="stack">
            <div>
              <p class="label">Not</p>
              <input name="note" placeholder="İsteğe bağlı">
            </div>
            <button type="submit">Sayfadaki düzenlemeleri kaydet</button>
          </div>
        </form>
      </aside>
    </section>
    <script>
      (() => {{
        const data = {json.dumps(editor_data, ensure_ascii=False)};
        const canvas = document.querySelector("[data-page-canvas]");
        const image = document.querySelector("[data-page-image]");
        const list = document.querySelector("[data-object-list]");
        const activeTitle = document.querySelector("[data-active-title]");
        const questionFields = document.querySelector("[data-question-fields]");
        const stemFields = document.querySelector("[data-stem-fields]");
        const issueCode = document.querySelector("[data-issue-code]");
        const newQuestionFields = document.querySelector("[data-new-question-fields]");
        const newQuestionNo = document.querySelector("[data-new-question-no]");
        const placement = document.querySelector("[data-placement]");
        const targetList = document.querySelector("[data-target-list]");
        const editsJson = document.querySelector("[data-edits-json]");
        const reads = {{
          left: document.querySelector("[data-read-left]"),
          top: document.querySelector("[data-read-top]"),
          right: document.querySelector("[data-read-right]"),
          bottom: document.querySelector("[data-read-bottom]"),
        }};
        const objects = [...data.questions, ...data.stems];
        let activeId = objects[0]?.id || "";
        let activeMode = "";
        let activeHandle = "";
        let startPoint = null;
        let startBounds = null;
        let stemCounter = data.stems.length;
        let questionCounter = 0;

        const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
        const allQuestions = () => objects.filter((item) => item.kind === "question");
        const existingQuestionNumbers = new Set(data.existingQuestionNumbers || []);
        const active = () => objects.find((item) => item.id === activeId) || objects[0];
        const rect = () => image.getBoundingClientRect();
        const pointFromEvent = (event) => {{
          const box = rect();
          return {{
            x: (clamp(event.clientX - box.left, 0, box.width) / box.width) * data.pageWidth,
            y: (clamp(event.clientY - box.top, 0, box.height) / box.height) * data.pageHeight,
          }};
        }};
        const normalize = (a, b) => ({{
          crop_left: Math.min(a.x, b.x),
          crop_top: Math.min(a.y, b.y),
          crop_right: Math.max(a.x, b.x),
          crop_bottom: Math.max(a.y, b.y),
        }});
        const cleanBounds = (bounds) => ({{
          crop_left: clamp(bounds.crop_left, 0, data.pageWidth),
          crop_top: clamp(bounds.crop_top, 0, data.pageHeight),
          crop_right: clamp(bounds.crop_right, 0, data.pageWidth),
          crop_bottom: clamp(bounds.crop_bottom, 0, data.pageHeight),
        }});
        const validBounds = (bounds) => (
          bounds.crop_right - bounds.crop_left >= 24 &&
          bounds.crop_bottom - bounds.crop_top >= 24
        );
        const markDirty = (item) => {{
          item.dirty = true;
        }};
        const updateQuestionLabel = (item) => {{
          item.label = `Soru ${{String(item.soruNo || 0).padStart(2, "0")}}`;
        }};
        const usedQuestionNumbers = (ignoreId = "") => new Set(
          allQuestions()
            .filter((question) => question.id !== ignoreId)
            .map((question) => Number(question.soruNo || 0))
            .filter((number) => number > 0)
        );
        const nextQuestionNo = (afterNumber = 0) => {{
          const used = usedQuestionNumbers();
          (data.existingQuestionNumbers || []).forEach((number) => used.add(Number(number || 0)));
          let candidate = Math.max(1, Number(afterNumber || 0) + 1);
          while (used.has(candidate)) candidate += 1;
          return candidate;
        }};
        const setActive = (id) => {{
          activeId = id;
          render();
        }};
        const renderBoxes = () => {{
          canvas.querySelectorAll(".page-box").forEach((node) => node.remove());
          const box = rect();
          objects.forEach((item) => {{
            const b = item.bounds;
            const left = (b.crop_left / data.pageWidth) * box.width;
            const top = (b.crop_top / data.pageHeight) * box.height;
            const right = (b.crop_right / data.pageWidth) * box.width;
            const bottom = (b.crop_bottom / data.pageHeight) * box.height;
            const node = document.createElement("div");
            node.className = `page-box ${{item.kind}}${{item.id === activeId ? " active" : ""}}`;
            node.dataset.id = item.id;
            node.style.left = `${{left}}px`;
            node.style.top = `${{top}}px`;
            node.style.width = `${{Math.max(2, right - left)}}px`;
            node.style.height = `${{Math.max(2, bottom - top)}}px`;
            const label = document.createElement("span");
            label.className = "page-box-label";
            label.textContent = item.label;
            node.appendChild(label);
            if (item.id === activeId) {{
              ["nw", "n", "ne", "e", "se", "s", "sw", "w"].forEach((handle) => {{
                const handleNode = document.createElement("span");
                handleNode.className = "box-handle";
                handleNode.dataset.handle = handle;
                node.appendChild(handleNode);
              }});
            }}
            canvas.appendChild(node);
          }});
        }};
        const renderList = () => {{
          list.innerHTML = "";
          objects.forEach((item) => {{
            const button = document.createElement("button");
            button.type = "button";
            button.className = `page-object${{item.id === activeId ? " active" : ""}}${{item.isNew ? " new" : ""}}`;
            button.dataset.id = item.id;
            const title = document.createElement("span");
            title.textContent = item.label;
            const badge = document.createElement("small");
            badge.textContent = item.kind === "stem" ? "kök" : "soru";
            button.append(title, badge);
            button.addEventListener("click", () => setActive(item.id));
            list.appendChild(button);
          }});
        }};
        const renderPanel = () => {{
          const item = active();
          if (!item) return;
          activeTitle.textContent = item.label;
          reads.left.value = item.bounds.crop_left.toFixed(2);
          reads.top.value = item.bounds.crop_top.toFixed(2);
          reads.right.value = item.bounds.crop_right.toFixed(2);
          reads.bottom.value = item.bounds.crop_bottom.toFixed(2);
          questionFields.classList.toggle("page-hidden", item.kind !== "question");
          stemFields.classList.toggle("page-hidden", item.kind !== "stem");
          if (item.kind === "question") {{
            issueCode.value = item.issueCode || "bottom_cut";
            newQuestionFields.classList.toggle("page-hidden", !item.isNew);
            if (item.isNew) newQuestionNo.value = String(item.soruNo || "");
          }} else {{
            newQuestionFields.classList.add("page-hidden");
            placement.value = item.placement || "top";
            targetList.innerHTML = "";
            allQuestions()
              .filter((question) => question.testNo === item.testNo)
              .forEach((question) => {{
                const label = document.createElement("label");
                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                checkbox.value = question.soruNo;
                checkbox.checked = (item.targets || []).includes(question.soruNo);
                checkbox.addEventListener("change", () => {{
                  const selected = new Set(item.targets || []);
                  if (checkbox.checked) selected.add(question.soruNo);
                  else selected.delete(question.soruNo);
                  item.targets = [...selected].sort((a, b) => a - b);
                  item.label = "Ortak kök " + item.targets.map((number) => String(number).padStart(2, "0")).join(", ");
                  markDirty(item);
                  render();
                }});
                const span = document.createElement("span");
                span.textContent = question.label;
                label.append(checkbox, span);
                targetList.appendChild(label);
              }});
          }}
        }};
        const render = () => {{
          renderBoxes();
          renderList();
          renderPanel();
        }};
        const moveBounds = (origin, start, current) => {{
          const width = origin.crop_right - origin.crop_left;
          const height = origin.crop_bottom - origin.crop_top;
          const left = clamp(origin.crop_left + current.x - start.x, 0, data.pageWidth - width);
          const top = clamp(origin.crop_top + current.y - start.y, 0, data.pageHeight - height);
          return {{ crop_left: left, crop_top: top, crop_right: left + width, crop_bottom: top + height }};
        }};
        const resizeBounds = (handle, origin, current) => {{
          let next = {{ ...origin }};
          if (handle.includes("w")) next.crop_left = current.x;
          if (handle.includes("e")) next.crop_right = current.x;
          if (handle.includes("n")) next.crop_top = current.y;
          if (handle.includes("s")) next.crop_bottom = current.y;
          next = cleanBounds(next);
          return {{
            crop_left: Math.min(next.crop_left, next.crop_right),
            crop_top: Math.min(next.crop_top, next.crop_bottom),
            crop_right: Math.max(next.crop_left, next.crop_right),
            crop_bottom: Math.max(next.crop_top, next.crop_bottom),
          }};
        }};
        canvas.addEventListener("pointerdown", (event) => {{
          const boxNode = event.target.closest(".page-box");
          if (!boxNode) return;
          const item = objects.find((candidate) => candidate.id === boxNode.dataset.id);
          if (!item) return;
          setActive(item.id);
          event.preventDefault();
          activeMode = event.target.closest("[data-handle]") ? "resize" : "move";
          activeHandle = event.target.closest("[data-handle]")?.dataset.handle || "";
          startPoint = pointFromEvent(event);
          startBounds = {{ ...item.bounds }};
          canvas.setPointerCapture(event.pointerId);
        }});
        canvas.addEventListener("pointermove", (event) => {{
          if (!activeMode || !startPoint || !startBounds) return;
          event.preventDefault();
          const item = active();
          const current = pointFromEvent(event);
          const next = activeMode === "resize"
            ? resizeBounds(activeHandle, startBounds, current)
            : moveBounds(startBounds, startPoint, current);
          if (validBounds(next)) {{
            item.bounds = next;
            markDirty(item);
            renderBoxes();
            renderPanel();
          }}
        }});
        canvas.addEventListener("pointerup", (event) => {{
          if (!activeMode) return;
          activeMode = "";
          activeHandle = "";
          startPoint = null;
          startBounds = null;
          canvas.releasePointerCapture(event.pointerId);
          render();
        }});
        issueCode.addEventListener("change", () => {{
          const item = active();
          if (item?.kind !== "question") return;
          item.issueCode = issueCode.value || "bottom_cut";
          markDirty(item);
        }});
        newQuestionNo.addEventListener("input", () => {{
          const item = active();
          if (item?.kind !== "question" || !item.isNew) return;
          const previous = Number(item.soruNo || 0);
          item.soruNo = Math.max(1, Math.floor(Number(newQuestionNo.value || "1")));
          objects
            .filter((candidate) => candidate.kind === "stem" && candidate.testNo === item.testNo && (candidate.targets || []).includes(previous))
            .forEach((stem) => {{
              stem.targets = [...new Set((stem.targets || []).map((number) => Number(number) === previous ? item.soruNo : number))].sort((a, b) => a - b);
              markDirty(stem);
            }});
          updateQuestionLabel(item);
          markDirty(item);
          renderBoxes();
          renderList();
          renderPanel();
        }});
        placement.addEventListener("change", () => {{
          const item = active();
          if (item?.kind !== "stem") return;
          item.placement = placement.value === "left" ? "left" : "top";
          markDirty(item);
        }});
        document.querySelector("[data-add-stem]").addEventListener("click", () => {{
          const current = active();
          const question = current?.kind === "question" ? current : allQuestions()[0];
          if (!question) return;
          const top = Math.max(0, question.bounds.crop_top - 140);
          const bottom = Math.max(24, question.bounds.crop_top - 8);
          const stem = {{
            id: `s-new-${{++stemCounter}}`,
            kind: "stem",
            sourceRowId: question.rowId ?? question.sourceRowId,
            testNo: question.testNo,
            testName: question.testName,
            label: `Ortak kök ${{String(question.soruNo).padStart(2, "0")}}`,
            bounds: {{
              crop_left: question.bounds.crop_left,
              crop_top: top,
              crop_right: question.bounds.crop_right,
              crop_bottom: bottom > top + 24 ? bottom : Math.min(data.pageHeight, top + 120),
            }},
            placement: "top",
            targets: [question.soruNo],
            dirty: true,
            isNew: true,
          }};
          objects.push(stem);
          setActive(stem.id);
        }});
        document.querySelector("[data-add-question]").addEventListener("click", () => {{
          const current = active();
          const baseQuestion = current?.kind === "question" ? current : allQuestions()[0];
          if (!baseQuestion) return;
          const height = Math.max(120, Math.min(260, baseQuestion.bounds.crop_bottom - baseQuestion.bounds.crop_top));
          const top = clamp(baseQuestion.bounds.crop_bottom + 24, 0, Math.max(0, data.pageHeight - height));
          const question = {{
            id: `q-new-${{++questionCounter}}`,
            kind: "question",
            rowId: null,
            sourceRowId: baseQuestion.rowId ?? baseQuestion.sourceRowId,
            testNo: baseQuestion.testNo,
            testName: baseQuestion.testName,
            soruNo: nextQuestionNo(baseQuestion.soruNo),
            label: "",
            bounds: {{
              crop_left: baseQuestion.bounds.crop_left,
              crop_top: top,
              crop_right: baseQuestion.bounds.crop_right,
              crop_bottom: Math.min(data.pageHeight, top + height),
            }},
            issueCode: "bottom_cut",
            dirty: true,
            isNew: true,
          }};
          updateQuestionLabel(question);
          objects.push(question);
          setActive(question.id);
        }});
        document.querySelector("[data-page-edit-form]").addEventListener("submit", (event) => {{
          const dirtyQuestions = objects.filter((item) => item.kind === "question" && item.dirty && !item.isNew);
          const newQuestions = objects.filter((item) => item.kind === "question" && item.isNew);
          const dirtyStems = objects.filter((item) => item.kind === "stem" && item.dirty);
          for (const item of [...dirtyQuestions, ...newQuestions, ...dirtyStems]) {{
            if (!validBounds(item.bounds)) {{
              event.preventDefault();
              alert(`${{item.label}} kutusu çok küçük.`);
              return;
            }}
          }}
          const seenNew = new Set();
          for (const question of newQuestions) {{
            const soruNo = Math.floor(Number(question.soruNo || 0));
            if (soruNo <= 0) {{
              event.preventDefault();
              alert("Yeni soru için geçerli bir soru numarası gir.");
              return;
            }}
            if (existingQuestionNumbers.has(soruNo) || usedQuestionNumbers(question.id).has(soruNo) || seenNew.has(soruNo)) {{
              event.preventDefault();
              alert(`Soru ${{String(soruNo).padStart(2, "0")}} zaten kayıtlı.`);
              return;
            }}
            seenNew.add(soruNo);
          }}
          for (const stem of dirtyStems) {{
            if (!stem.targets?.length) {{
              event.preventDefault();
              alert(`${{stem.label}} için en az bir hedef soru seç.`);
              return;
            }}
          }}
          editsJson.value = JSON.stringify({{
            questions: dirtyQuestions.map((item) => ({{
              row_id: item.rowId,
              bounds: item.bounds,
              issue_code: item.issueCode || "bottom_cut",
            }})),
            new_questions: newQuestions.map((item) => ({{
              source_row_id: item.sourceRowId,
              soru_no: item.soruNo,
              bounds: item.bounds,
              issue_code: item.issueCode || "bottom_cut",
            }})),
            common_stems: dirtyStems.map((item) => ({{
              source_row_id: item.sourceRowId,
              target_soru_no: item.targets || [],
              bounds: item.bounds,
              placement: item.placement || "top",
            }})),
          }});
        }});
        image.addEventListener("load", render);
        window.addEventListener("resize", render);
        if (image.complete) render();
      }})();
    </script>
    """
    return page_shell("Sayfa Düzenle", body)


def number_hide_progress_page(job_id: str, op_id: str) -> HTMLResponse:
    body = f"""
    <section class="hero">
      <span class="eyebrow">Soru numarası</span>
      <h1>Numaralar gizleniyor.</h1>
      <p class="sub">Seçilen soru PDF'leri ve önizlemeleri güncelleniyor.</p>
    </section>
    <section class="card progress-card" data-number-hide-progress="{html.escape(op_id)}" data-job-id="{html.escape(job_id)}">
      <div class="status">
        <div>
          <p class="label">Durum</p>
          <strong data-hide-status>Hazırlanıyor</strong>
        </div>
        <span class="pill" data-hide-count>0/0</span>
      </div>
      <div class="meter"><span data-hide-bar></span></div>
      <div class="processing-grid">
        <div class="metric">
          <span class="label">İşlenen</span>
          <strong data-hide-current>Bekleniyor</strong>
        </div>
        <div class="metric">
          <span class="label">İlerleme</span>
          <strong data-hide-percent>%0</strong>
        </div>
        <div class="metric">
          <span class="label">Sonuç</span>
          <strong data-hide-result>Çalışıyor</strong>
        </div>
      </div>
      <div class="error hidden" data-hide-error></div>
    </section>
    <script>
      (() => {{
        const root = document.querySelector("[data-number-hide-progress]");
        if (!root) return;
        const opId = root.dataset.numberHideProgress || "";
        const jobId = root.dataset.jobId || "";
        const statusNode = document.querySelector("[data-hide-status]");
        const countNode = document.querySelector("[data-hide-count]");
        const barNode = document.querySelector("[data-hide-bar]");
        const currentNode = document.querySelector("[data-hide-current]");
        const percentNode = document.querySelector("[data-hide-percent]");
        const resultNode = document.querySelector("[data-hide-result]");
        const errorNode = document.querySelector("[data-hide-error]");

        const setText = (node, text) => {{
          if (node) node.textContent = text || "";
        }};
        const poll = async () => {{
          try {{
            const response = await fetch(`/jobs/${{jobId}}/number-hide/${{opId}}/status`, {{ cache: "no-store" }});
            const payload = await response.json();
            const percent = Math.max(4, Math.min(100, Number(payload.progress_percent || 0)));
            setText(statusNode, payload.message || payload.status);
            setText(countNode, `${{payload.done || 0}}/${{payload.total || 0}}`);
            setText(currentNode, payload.current || "Bekleniyor");
            setText(percentNode, `%${{percent}}`);
            setText(resultNode, payload.status === "completed" ? "Tamamlandı" : "Çalışıyor");
            if (barNode) barNode.style.width = `${{percent}}%`;
            if (payload.status === "completed") {{
              window.location.href = payload.redirect_url || `/jobs/${{jobId}}/review?saved=1`;
              return;
            }}
            if (payload.status === "error") {{
              setText(resultNode, "Hata");
              if (errorNode) {{
                errorNode.textContent = payload.error || "İşlem başarısız oldu.";
                errorNode.classList.remove("hidden");
              }}
              return;
            }}
          }} catch (error) {{
            setText(statusNode, "Durum alınamadı, yeniden deneniyor...");
          }}
          window.setTimeout(poll, 700);
        }};
        poll();
      }})();
    </script>
    """
    return page_shell("Soru Numarası Gizleme", body)


@app.on_event("startup")
def startup() -> None:
    ensure_cleanup_worker_started()


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_get(request: Request):
    if admin_logged_in(request):
        return RedirectResponse(url="/admin", status_code=303)
    return admin_login_page()


@app.post("/admin/login")
def admin_login_post(username: str = Form(...), password: str = Form(...)):
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return admin_login_page("Kullanıcı adı veya şifre hatalı.")
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        admin_token(),
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
    )
    return response


@app.post("/admin/logout")
def admin_logout() -> RedirectResponse:
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin_home(
    request: Request,
    deleted: str | None = None,
    deleted_count: str | None = None,
    learned_events: str | None = None,
    learned_bounds: str | None = None,
    learned_common_stems: str | None = None,
    learned_deleted_count: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    filter_error: str | None = None,
) -> HTMLResponse:
    parsed_deleted_count: int | None = None
    if deleted_count and deleted_count.isdigit():
        parsed_deleted_count = int(deleted_count)
    parsed_learned_events = int(learned_events) if learned_events and learned_events.isdigit() else None
    parsed_learned_bounds = int(learned_bounds) if learned_bounds and learned_bounds.isdigit() else None
    parsed_learned_common_stems = (
        int(learned_common_stems) if learned_common_stems and learned_common_stems.isdigit() else None
    )
    parsed_learned_deleted_count = (
        int(learned_deleted_count) if learned_deleted_count and learned_deleted_count.isdigit() else None
    )
    return admin_page(
        request,
        deleted=deleted == "1",
        deleted_count=parsed_deleted_count,
        learned_events=parsed_learned_events,
        learned_bounds=parsed_learned_bounds,
        learned_common_stems=parsed_learned_common_stems,
        learned_deleted_count=parsed_learned_deleted_count,
        start_date=start_date or "",
        end_date=end_date or "",
        filter_error=filter_error or "",
    )


@app.get("/admin/archive", response_class=HTMLResponse)
def admin_archive(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    message: str | None = None,
    filter_error: str | None = None,
) -> HTMLResponse:
    return admin_archive_page(
        request,
        start_date=start_date or "",
        end_date=end_date or "",
        message=message or "",
        filter_error=filter_error or "",
    )


@app.post("/admin/archive/update")
def admin_archive_update(
    request: Request,
    rel_path: str = Form(...),
    status: str = Form("new"),
    dev_note: str = Form(""),
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
) -> RedirectResponse:
    require_admin(request)
    manager = archive_manager()
    manager.update_record(rel_path.strip(), status=status, dev_note=dev_note)
    return RedirectResponse(
        url=admin_archive_redirect_url(start_date or "", end_date or "", "Kayıt durumu güncellendi."),
        status_code=303,
    )


@app.post("/admin/archive/import")
def admin_archive_import(
    request: Request,
    rel_path: str = Form(...),
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
) -> RedirectResponse:
    require_admin(request)
    clean_rel_path = rel_path.strip()
    root = LOCAL_JOBS_ROOT.resolve()
    target_dir = (LOCAL_JOBS_ROOT / clean_rel_path).resolve()
    if not target_dir.is_relative_to(root) or not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="Geçersiz arşiv yolu")
    report_path = target_dir / "report.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="report.json bulunamadı")

    events, bounds, common_stems, _learned_paths = import_feedback_report_paths([report_path], CropFeedbackStore())
    manager = archive_manager()
    records = manager.state.get("records", {})
    prev_note = ""
    if isinstance(records, dict):
        existing = records.get(clean_rel_path, {})
        if isinstance(existing, dict):
            prev_note = str(existing.get("dev_note") or "")
    manager.update_record(clean_rel_path, status="learned", dev_note=prev_note)
    return RedirectResponse(
        url=admin_archive_redirect_url(
            start_date or "",
            end_date or "",
            f"Öğrenme işlendi: hata {events}, sınır {bounds}, ortak kök {common_stems}.",
        ),
        status_code=303,
    )


@app.post("/admin/archive/delete-learned")
def admin_archive_delete_learned(
    request: Request,
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
) -> RedirectResponse:
    require_admin(request)
    filters = parse_admin_date_filters(start_date, end_date)
    if filters["error"]:
        return RedirectResponse(
            url=admin_archive_redirect_url(start_date or "", end_date or "", str(filters["error"])),
            status_code=303,
        )
    all_feedback_dirs = sorted(local_feedback_dirs(), key=feedback_dir_created_at)
    feedback_dirs = filter_feedback_dirs(
        all_feedback_dirs,
        int(filters["start_ts"]) if filters["start_ts"] is not None else None,
        int(filters["end_ts"]) if filters["end_ts"] is not None else None,
    )
    manager = archive_manager()
    records = manager.list_records(feedback_dirs)
    learned_rel_paths = {record.rel_path for record in records if record.status == "learned"}
    deleted_count = 0
    for rel_path in learned_rel_paths:
        target_dir = LOCAL_JOBS_ROOT / rel_path
        if remove_feedback_dir_and_empty_parents(target_dir):
            deleted_count += 1
    manager.remove_records(learned_rel_paths)
    return RedirectResponse(
        url=admin_archive_redirect_url(start_date or "", end_date or "", f"Öğrenildi kaydı silindi: {deleted_count}"),
        status_code=303,
    )


@app.get("/admin/archive/image")
def admin_archive_image(request: Request, rel_path: str, filename: str) -> FileResponse:
    require_admin(request)
    clean_rel_path = rel_path.strip()
    root = LOCAL_JOBS_ROOT.resolve()
    target_dir = (LOCAL_JOBS_ROOT / clean_rel_path).resolve()
    if not target_dir.is_relative_to(root) or not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="Geçersiz arşiv yolu")
    file_path = (target_dir / filename).resolve()
    if not file_path.is_relative_to(target_dir) or not file_path.exists():
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    if file_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=400, detail="Geçersiz dosya türü")
    return FileResponse(file_path, media_type="image/png")


@app.get("/admin/download")
def admin_download(request: Request, start_date: str | None = None, end_date: str | None = None) -> FileResponse:
    require_admin(request)
    filters = parse_admin_date_filters(start_date, end_date)
    if filters["error"]:
        raise HTTPException(status_code=400, detail=str(filters["error"]))
    has_filter = bool(filters["start_date"] or filters["end_date"])
    feedback_dirs = (
        filter_feedback_dirs(
            sorted(local_feedback_dirs(), key=feedback_dir_created_at),
            int(filters["start_ts"]) if filters["start_ts"] is not None else None,
            int(filters["end_ts"]) if filters["end_ts"] is not None else None,
        )
        if has_filter
        else None
    )
    handle = tempfile.NamedTemporaryFile(prefix="pdf-cut-feedback-", suffix=".zip", delete=False)
    handle.close()
    zip_path = Path(handle.name)
    build_feedback_zip(zip_path, feedback_dirs, include_error_memory=not has_filter)
    download_name = f"pdf-cut-feedback-{now_ts()}.zip"
    if has_filter:
        download_name = f"pdf-cut-feedback-filtered-{now_ts()}.zip"
    return FileResponse(
        zip_path,
        filename=download_name,
        media_type="application/zip",
        background=BackgroundTask(remove_temp_file, str(zip_path)),
    )


@app.post("/admin/learn")
def admin_learn(
    request: Request,
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
) -> RedirectResponse:
    require_admin(request)
    filters = parse_admin_date_filters(start_date, end_date)
    start_value = str(filters["start_date"] or "")
    end_value = str(filters["end_date"] or "")
    filter_query = admin_filter_query(start_value, end_value)
    if filters["error"]:
        error_part = f"filter_error={quote(str(filters['error']))}"
        query_parts = [error_part]
        if filter_query:
            query_parts.append(filter_query)
        return RedirectResponse(url=f"/admin?{'&'.join(query_parts)}", status_code=303)

    all_feedback_dirs = sorted(local_feedback_dirs(), key=feedback_dir_created_at)
    has_filter = bool(start_value or end_value)
    feedback_dirs = (
        filter_feedback_dirs(
            all_feedback_dirs,
            int(filters["start_ts"]) if filters["start_ts"] is not None else None,
            int(filters["end_ts"]) if filters["end_ts"] is not None else None,
        )
        if has_filter
        else all_feedback_dirs
    )

    report_paths = [path / "report.json" for path in feedback_dirs if (path / "report.json").exists()]
    events, bounds, common_stems, learned_paths = import_feedback_report_paths(report_paths, CropFeedbackStore())

    learned_dirs = {report_path.parent for report_path in learned_paths}
    deleted_count = 0
    for path in learned_dirs:
        if remove_feedback_dir_and_empty_parents(path):
            deleted_count += 1

    query_parts = [
        f"learned_events={events}",
        f"learned_bounds={bounds}",
        f"learned_common_stems={common_stems}",
        f"learned_deleted_count={deleted_count}",
    ]
    if filter_query:
        query_parts.append(filter_query)
    return RedirectResponse(url=f"/admin?{'&'.join(query_parts)}", status_code=303)


@app.post("/admin/delete")
def admin_delete(
    request: Request,
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
) -> RedirectResponse:
    require_admin(request)
    filters = parse_admin_date_filters(start_date, end_date)
    start_value = str(filters["start_date"] or "")
    end_value = str(filters["end_date"] or "")
    filter_query = admin_filter_query(start_value, end_value)
    if filters["error"]:
        error_part = f"filter_error={quote(str(filters['error']))}"
        query_parts = [error_part]
        if filter_query:
            query_parts.append(filter_query)
        return RedirectResponse(url=f"/admin?{'&'.join(query_parts)}", status_code=303)
    has_filter = bool(start_value or end_value)
    if has_filter:
        feedback_dirs = filter_feedback_dirs(
            sorted(local_feedback_dirs(), key=feedback_dir_created_at),
            int(filters["start_ts"]) if filters["start_ts"] is not None else None,
            int(filters["end_ts"]) if filters["end_ts"] is not None else None,
        )
        deleted_count = 0
        for path in feedback_dirs:
            if remove_feedback_dir_and_empty_parents(path):
                deleted_count += 1
        query_parts = [f"deleted_count={deleted_count}"]
        if filter_query:
            query_parts.append(filter_query)
        return RedirectResponse(url=f"/admin?{'&'.join(query_parts)}", status_code=303)
    ensure_dir(LOCAL_JOBS_ROOT)
    for child in LOCAL_JOBS_ROOT.iterdir():
        if child.name in {"README.md", ".DS_Store"}:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    return RedirectResponse(url="/admin?deleted=1", status_code=303)


OFFLINE_PACKAGE_SPECS = {
    "macos-arm64": {
        "glob": "PDF-Kesim-Offline-macOS-arm64-v*.zip",
        "title": "macOS",
        "icon": "🍎",
        "label": "macOS uygulamasını indir",
        "detail": "Apple Silicon (M1, M2, M3, M4 ve sonrası)",
        "media_type": "application/zip",
    },
    "windows-x64": {
        "glob": "PDF-Kesim-Offline-Windows-x64-v*.zip",
        "title": "Windows",
        "icon": "🪟",
        "label": "Windows uygulamasını indir",
        "detail": "Windows 10/11 · 64 bit",
        "media_type": "application/zip",
    },
    "linux-x64": {
        "glob": "PDF-Kesim-Offline-Linux-x64-v*.tar.gz",
        "title": "Linux",
        "icon": "🐧",
        "label": "Linux uygulamasını indir",
        "detail": "x86_64 · glibc 2.17 ve sonrası",
        "media_type": "application/gzip",
    },
}


def latest_offline_package(platform_key: str) -> Path | None:
    spec = OFFLINE_PACKAGE_SPECS.get(platform_key)
    if not spec or not OFFLINE_RELEASES_ROOT.exists():
        return None
    candidates = sorted(OFFLINE_RELEASES_ROOT.glob(str(spec["glob"])), key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def offline_package_version(package: Path) -> str:
    match = re.search(r"-v(.+?)(?:\.tar\.gz|\.zip)$", package.name)
    return match.group(1) if match else APP_VERSION


def offline_platform_block(platform_key: str) -> str:
    spec = OFFLINE_PACKAGE_SPECS[platform_key]
    package = latest_offline_package(platform_key)
    title = html.escape(str(spec["title"]))
    icon = html.escape(str(spec["icon"]))
    detail = html.escape(str(spec["detail"]))
    label = html.escape(str(spec["label"]))
    if package is None:
        action = '<span class="btn" aria-disabled="true">Paket hazırlanıyor</span>'
        metadata = detail
    else:
        size_mb = package.stat().st_size / (1024 * 1024)
        version = html.escape(offline_package_version(package))
        action = f'<a class="btn secondary" href="/offline/download/{platform_key}">⬇️ {label}</a>'
        metadata = f"{detail} · {size_mb:.0f} MB · Sürüm {version}"
    help_action = (
        '<a class="btn ghost" href="/offline/macos-help">macOS açılmıyorsa çözüm</a>'
        if platform_key == "macos-arm64"
        else ""
    )
    return f"""
      <div class="step">
        <span class="step-no">{icon}</span>
        <div>
          <strong>{title}</strong>
          <span>{metadata}</span>
          <div class="actions" style="margin-top:10px">{action}{help_action}</div>
        </div>
      </div>
    """


@app.get("/offline", response_class=HTMLResponse)
def offline_use_page() -> HTMLResponse:
    platform_blocks = "".join(
        offline_platform_block(platform_key)
        for platform_key in ("macos-arm64", "windows-x64", "linux-x64")
    )

    body = f"""
    <section class="hero">
      <span class="eyebrow">Tamamen yerel</span>
      <h1>Sunucu olmadan kullan.</h1>
      <p class="sub">PDF dosyaları bilgisayarınızdan çıkmaz. Python, Poppler, Tesseract ve gerekli tüm kütüphaneler uygulamanın içinde gelir.</p>
    </section>
    <section class="layout">
      <div class="card stack">
        <div>
          <p class="label">İşletim sisteminizi seçin</p>
          <h2>PDF Kesim Offline</h2>
          <p>Uygulama kendi yerel web sunucusunu açar ve aynı arayüzü uygulama penceresinde veya tarayıcınızda çalıştırır. Kurulumdan sonra internet gerekmez.</p>
        </div>
        <div class="steps">{platform_blocks}</div>
        <div class="actions"><a class="btn ghost" href="/">Siteye dön</a></div>
      </div>
      <div class="card">
        <p class="label">İlk kurulum</p>
        <div class="steps">
          <div class="step"><span class="step-no">1</span><div><strong>Doğru paketi indirin</strong><span>İşletim sisteminize uygun ZIP veya TAR.GZ dosyasını seçin.</span></div></div>
          <div class="step"><span class="step-no">2</span><div><strong>Paketi tamamen çıkarın</strong><span>Windows'ta .bat, Linux'ta .sh dosyasını; macOS'ta uygulamayı açın.</span></div></div>
          <div class="step"><span class="step-no">3</span><div><strong>İlk çalıştırma onayı</strong><span>İmza sertifikası olmadığı için işletim sistemi bir kez güvenlik onayı isteyebilir.</span></div></div>
        </div>
      </div>
    </section>
    """
    return page_shell("Çevrimdışı Kullan", body)


@app.get("/offline/macos-help", response_class=HTMLResponse)
def macos_offline_help_page() -> HTMLResponse:
    command = (
        'xattr -dr com.apple.quarantine "/Applications/PDF Kesim Offline.app" '
        '&& open "/Applications/PDF Kesim Offline.app"'
    )
    body = f"""
    <section class="hero">
      <span class="eyebrow">macOS Gatekeeper</span>
      <h1>“Açılamadı” uyarısını çözün.</h1>
      <p class="sub">Uygulamanın kod imzası bütünlük kontrolünden geçer; ancak Apple Developer ID ile noterlenmediği için macOS ilk çalıştırmada ayrıca izin ister.</p>
    </section>
    <section class="layout">
      <div class="card">
        <p class="label">Önerilen yöntem</p>
        <div class="steps">
          <div class="step"><span class="step-no">1</span><div><strong>Uygulamayı bir kez açmayı deneyin</strong><span>Uyarı gelince Bitti düğmesine basın.</span></div></div>
          <div class="step"><span class="step-no">2</span><div><strong>Gizlilik ve Güvenlik'i açın</strong><span>Apple menüsü → Sistem Ayarları → Gizlilik ve Güvenlik bölümüne gidin.</span></div></div>
          <div class="step"><span class="step-no">3</span><div><strong>Yine de Aç'a basın</strong><span>Güvenlik bölümünde PDF Kesim Offline için “Yine de Aç”ı seçip Mac parolanızı girin. Bu seçenek ilk denemeden sonra yaklaşık bir saat görünür.</span></div></div>
        </div>
        <div class="actions" style="margin-top:18px">
          <a class="btn secondary" href="x-apple.systempreferences:com.apple.preference.security?Privacy">Gizlilik ve Güvenlik'i aç</a>
          <a class="btn ghost" href="/offline">İndirme sayfasına dön</a>
        </div>
      </div>
      <div class="card stack">
        <div>
          <p class="label">Yine de Aç görünmüyorsa</p>
          <h2>Terminal alternatifi</h2>
          <p>Uygulamayı önce Uygulamalar klasörüne taşıyın. Ardından Terminal'i açıp aşağıdaki komutu bir kez çalıştırın.</p>
        </div>
        <pre id="macos-unblock-command">{html.escape(command)}</pre>
        <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('macos-unblock-command').textContent); this.textContent='Kopyalandı'">Komutu kopyala</button>
        <div class="note">Bu komutu yalnızca bu siteden indirdiğiniz PDF Kesim Offline uygulaması için kullanın.</div>
      </div>
    </section>
    """
    return page_shell("macOS Açılış Yardımı", body)


@app.get("/offline/download/{platform_key}")
def download_offline_package(platform_key: str) -> FileResponse:
    package = latest_offline_package(platform_key)
    spec = OFFLINE_PACKAGE_SPECS.get(platform_key)
    if package is None or spec is None:
        raise HTTPException(status_code=404, detail="Çevrimdışı paket henüz hazır değil")
    return FileResponse(
        package,
        filename=package.name,
        media_type=str(spec["media_type"]),
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    history_link = (
        '<a class="btn-history" href="/jobs/history">📋 İş Geçmişi</a>'
        if LOCAL_MODE
        else ""
    )
    offline_link = (
        ""
        if LOCAL_MODE
        else '<a class="btn-offline" href="/offline">⬇️ Çevrimdışı Kullan</a>'
    )
    font_links = "" if LOCAL_MODE else """
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Inter:wght@300;400;500;700&display=swap" rel="stylesheet">"""
    html_content = """<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>Kolay Soru Kesim & Düzenle</title>
  {font_links}
  <style>
    :root {
      --bg-dark: #0f172a;
      --bg-sidebar: #1e293b;
      --bg-canvas: #1e293b;
      --bg-card: #0f172a;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #10b981;
      --accent-hover: #059669;
      --border: #475569;
      --btn-bg: #475569;
      --btn-hover: #64748b;
    }
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    body {
      font-family: 'Inter', sans-serif;
      background: var(--bg-dark);
      color: var(--text);
      display: flex;
      height: 100vh;
      overflow: hidden;
    }
    /* Layout */
    .sidebar {
      width: 320px;
      background: var(--bg-sidebar);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      height: 100vh;
      flex-shrink: 0;
    }
    .sidebar-header {
      padding: 24px;
      border-bottom: 1px solid var(--border);
    }
    .sidebar-header h1 {
      font-family: 'Outfit', sans-serif;
      font-size: 1.5rem;
      font-weight: 700;
      margin-bottom: 6px;
      background: linear-gradient(135deg, #38bdf8, #10b981);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .sidebar-header p {
      font-size: 0.85rem;
      color: var(--text-muted);
    }
    .sidebar-content {
      flex: 1;
      padding: 24px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .info-box {
      font-size: 0.85rem;
      line-height: 1.6;
      color: var(--text-muted);
      background: rgba(15, 23, 42, 0.4);
      padding: 16px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }
    .info-box h3 {
      font-family: 'Outfit', sans-serif;
      color: var(--text);
      margin-bottom: 8px;
      font-size: 0.95rem;
    }
    .btn-history {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      width: 100%;
      padding: 12px;
      background: var(--btn-bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      font-weight: 600;
      text-decoration: none;
      font-size: 0.9rem;
      transition: all 0.2s ease;
    }
    .btn-history:hover {
      background: var(--btn-hover);
      border-color: #64748b;
    }
    .btn-offline {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      padding: 12px;
      margin-top: 10px;
      background: rgba(56, 189, 248, 0.12);
      color: #7dd3fc;
      border: 1px solid #0ea5e9;
      border-radius: 8px;
      font-weight: 700;
      text-decoration: none;
      font-size: 0.9rem;
      transition: all 0.2s ease;
    }
    .btn-offline:hover {
      background: rgba(56, 189, 248, 0.2);
      color: #e0f2fe;
    }
    
    .workspace {
      flex: 1;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-y: auto;
      background: var(--bg-dark);
      padding: 40px;
    }
    .content-container {
      max-width: 1100px;
      width: 100%;
      margin: 0 auto;
    }
    
    /* Upload Card */
    .upload-card {
      background: var(--bg-sidebar);
      border: 2px dashed var(--border);
      border-radius: 12px;
      padding: 48px 32px;
      text-align: center;
      cursor: pointer;
      transition: all 0.3s ease;
      position: relative;
    }
    .upload-card:hover, .upload-card.dragover {
      border-color: var(--accent);
      background: rgba(16, 185, 129, 0.05);
    }
    .upload-icon {
      font-size: 3rem;
      margin-bottom: 16px;
    }
    .upload-card h2 {
      font-family: 'Outfit', sans-serif;
      font-size: 1.4rem;
      font-weight: 600;
      margin-bottom: 8px;
    }
    .upload-card p {
      color: var(--text-muted);
      font-size: 0.9rem;
    }
    .file-input {
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      opacity: 0;
      cursor: pointer;
    }
    
    /* Config Panel & Split View */
    .config-panel {
      display: none;
      background: var(--bg-sidebar);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      margin-top: 24px;
      animation: fadeIn 0.4s ease forwards;
    }
    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .config-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 16px;
      margin-bottom: 20px;
    }
    .pdf-info {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .pdf-icon {
      font-size: 1.8rem;
    }
    .pdf-name {
      font-weight: 700;
      font-family: 'Outfit', sans-serif;
      font-size: 1.1rem;
      word-break: break-all;
    }
    .pdf-pages-badge {
      font-size: 0.8rem;
      background: var(--bg-dark);
      padding: 4px 8px;
      border-radius: 6px;
      border: 1px solid var(--border);
      color: var(--text-muted);
      white-space: nowrap;
    }
    
    .config-split {
      display: flex;
      gap: 24px;
      align-items: flex-start;
    }
    .config-left {
      flex: 1.2;
      min-width: 0;
    }
    .config-right {
      flex: 0.8;
      min-width: 340px;
      background: var(--bg-dark);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      position: sticky;
      top: 20px;
    }
    @media (max-width: 900px) {
      .config-split {
        flex-direction: column;
      }
      .config-right {
        position: static;
        width: 100%;
      }
    }
    
    /* Page Exclusions Checklist */
    .pages-section-title {
      font-size: 0.95rem;
      font-weight: 700;
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .bulk-actions {
      display: flex;
      gap: 8px;
    }
    .btn-small {
      background: none;
      border: 1px solid var(--border);
      color: var(--text-muted);
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 0.75rem;
      cursor: pointer;
      font-weight: 600;
      transition: all 0.2s ease;
    }
    .btn-small:hover {
      border-color: var(--text-muted);
      color: var(--text);
    }
    .pages-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(88px, 1fr));
      gap: 8px;
      max-height: 480px;
      overflow-y: auto;
      padding: 8px;
      background: var(--bg-dark);
      border-radius: 8px;
      border: 1px solid var(--border);
      margin-bottom: 20px;
    }
    .page-badge-wrapper {
      position: relative;
    }
    
    /* Sleek badge checkbox grid style */
    .page-badge {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px;
      background: var(--bg-sidebar);
      border: 1px solid var(--border);
      border-radius: 6px;
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
      user-select: none;
      transition: all 0.2s ease;
      color: var(--text-muted);
    }
    .page-badge.included {
      background: rgba(16, 185, 129, 0.06);
      border-color: rgba(16, 185, 129, 0.3);
      color: var(--text);
    }
    .page-badge.active {
      border-color: #38bdf8;
      box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.35);
    }
    .page-badge:hover {
      border-color: #64748b;
    }
    .badge-checkbox {
      accent-color: var(--accent);
      cursor: pointer;
      width: 14px;
      height: 14px;
    }
    
    /* Config Options */
    .config-options {
      display: flex;
      flex-direction: column;
      gap: 16px;
      border-top: 1px solid var(--border);
      padding-top: 20px;
    }
    .mode-selector {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .mode-option {
      position: relative;
      display: flex;
      flex-direction: column;
      gap: 5px;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--bg-dark);
      cursor: pointer;
      transition: border-color 0.2s, background 0.2s, box-shadow 0.2s;
    }
    .mode-option:hover {
      border-color: #64748b;
    }
    .mode-option:has(input:checked) {
      border-color: var(--accent);
      background: rgba(16, 185, 129, 0.08);
      box-shadow: 0 0 0 1px rgba(16, 185, 129, 0.25);
    }
    .mode-option input {
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }
    .mode-option-title {
      font-size: 0.9rem;
      font-weight: 700;
    }
    .mode-option-desc {
      color: var(--text-muted);
      font-size: 0.75rem;
      line-height: 1.4;
    }
    .option-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .option-label-wrapper {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .option-title {
      font-size: 0.95rem;
      font-weight: 700;
    }
    .option-desc {
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    
    /* Switch Style */
    .switch {
      position: relative;
      display: inline-block;
      width: 44px;
      height: 24px;
    }
    .switch input {
      opacity: 0;
      width: 0;
      height: 0;
    }
    .slider {
      position: absolute;
      cursor: pointer;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background-color: var(--btn-bg);
      transition: .3s;
      border-radius: 24px;
    }
    .slider:before {
      position: absolute;
      content: "";
      height: 16px;
      width: 16px;
      left: 4px;
      bottom: 4px;
      background-color: white;
      transition: .3s;
      border-radius: 50%;
    }
    input:checked + .slider {
      background-color: var(--accent);
    }
    input:checked + .slider:before {
      transform: translateX(20px);
    }
    
    /* Actions */
    .action-row {
      margin-top: 24px;
    }
    .btn-start {
      width: 100%;
      padding: 14px;
      background: var(--accent);
      color: white;
      border: none;
      border-radius: 8px;
      font-size: 1.05rem;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }
    .btn-start:hover {
      background: var(--accent-hover);
    }
    
    /* Preview pane */
    .preview-header-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
    }
    .preview-title {
      font-family: 'Outfit', sans-serif;
      font-size: 1.1rem;
      font-weight: 700;
    }
    .preview-image-container {
      position: relative;
      background: #090d16;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 420px;
      cursor: crosshair;
    }
    .preview-img {
      max-width: 100%;
      max-height: 480px;
      display: block;
      object-fit: contain;
      transition: opacity 0.2s ease;
    }
    .magnifier-lens {
      position: absolute;
      border: 3px solid var(--accent);
      border-radius: 50%;
      cursor: crosshair;
      width: 150px;
      height: 150px;
      display: none;
      pointer-events: none;
      box-shadow: 0 5px 15px rgba(0,0,0,0.5);
      background-repeat: no-repeat;
      z-index: 10;
    }
    .preview-controls {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-top: 16px;
    }
    .preview-nav-buttons {
      display: flex;
      gap: 8px;
    }
    .btn-nav {
      flex: 1;
      padding: 10px;
      background: var(--btn-bg);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 6px;
      font-weight: 600;
      cursor: pointer;
      font-size: 0.85rem;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }
    .btn-nav:hover {
      background: var(--btn-hover);
      border-color: #64748b;
    }
    .btn-nav:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .preview-toggle-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: var(--bg-sidebar);
      padding: 12px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }
    .preview-toggle-label {
      font-size: 0.85rem;
      font-weight: 700;
    }
    .keyboard-hint {
      font-size: 0.75rem;
      color: var(--text-muted);
      text-align: center;
      margin-top: 8px;
      font-style: italic;
    }
    
    /* Loader Overlay */
    .loader-overlay {
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(15, 23, 42, 0.8);
      z-index: 1000;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 16px;
      backdrop-filter: blur(4px);
    }
    .spinner {
      width: 48px;
      height: 48px;
      border: 4px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 1s linear infinite;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    .loader-text {
      font-family: 'Outfit', sans-serif;
      font-size: 1.2rem;
      font-weight: 600;
    }
    .loader-sub {
      color: var(--text-muted);
      font-size: 0.9rem;
    }
  </style>
</head>
<body>

  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sidebar-header">
      <h1>Kolay Soru Kesim</h1>
      <p>Otomatik Soru Tespit ve Düzenleme</p>
    </div>
    <div class="sidebar-content">
      <div class="info-box">
        <h3>Nasıl Çalışır?</h3>
        <p style="margin-bottom: 8px;">1. Sol panelden veya sürükleyerek PDF dosyanızı yükleyin.</p>
        <p style="margin-bottom: 8px;">2. İşleme dahil edilmesini istemediğiniz sayfaları uncheck edin.</p>
        <p style="margin-bottom: 8px;">3. Soru numaralarını gizleme tercihini seçip kesimi başlatın.</p>
        <p>4. Canlı logları ve ilerlemeyi izleyin, ardından Easy Editor ile düzenleyin.</p>
      </div>
      <div>
        {history_link}
        {offline_link}
      </div>
    </div>
  </div>

  <!-- Workspace -->
  <div class="workspace">
    <div class="content-container">
      
      <!-- Upload Dropzone -->
      <div class="upload-card" id="dropzone">
        <span class="upload-icon">📄</span>
        <h2>PDF Dosyası Yükle</h2>
        <p>Dosyayı buraya sürükleyin veya tıklayarak seçin</p>
        <input type="file" id="pdf-file-input" class="file-input" accept="application/pdf">
      </div>
      
      <!-- Configuration Panel -->
      <div class="config-panel" id="config-panel">
        <div class="config-header">
          <div class="pdf-info">
            <span class="pdf-icon">📕</span>
            <div>
              <div class="pdf-name" id="label-pdf-name">matematik_test.pdf</div>
              <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 2px;">Analiz edilmeye hazır</div>
            </div>
          </div>
          <span class="pdf-pages-badge" id="label-pdf-pages">48 Sayfa</span>
        </div>
        
        <div class="config-split">
          <!-- Left Column -->
          <div class="config-left">
            <!-- Checklist Section -->
            <div class="pages-section-title">
              <span>Kesilecek Sayfalar</span>
              <div class="bulk-actions">
                <button class="btn-small" onclick="toggleAllPages(true)">Tümünü Seç</button>
                <button class="btn-small" onclick="toggleAllPages(false)">Temizle</button>
              </div>
            </div>
            
            <div class="pages-grid" id="pages-grid">
              <!-- Dynamically populated page checkboxes -->
            </div>
            
            <!-- Config Options Switch -->
            <div class="config-options">
              <div>
                <div class="option-title" style="margin-bottom:10px;">Kesim Modu</div>
                <div class="mode-selector" role="radiogroup" aria-label="Kesim modu">
                  <label class="mode-option">
                    <input type="radio" name="workflow-mode" value="automatic" checked>
                    <span class="mode-option-title">✨ Otomatik Kesim</span>
                    <span class="mode-option-desc">Soruları başlangıçta otomatik algılar ve keser.</span>
                  </label>
                  <label class="mode-option">
                    <input type="radio" name="workflow-mode" value="manual">
                    <span class="mode-option-title">✍️ Manuel Kesim</span>
                    <span class="mode-option-desc">Boş editörle açılır; çizim ve algılama araçları hazır kalır.</span>
                  </label>
                </div>
              </div>
              <div class="option-item">
                <div class="option-label-wrapper">
                  <span class="option-title">Soru Numaralarını Gizle</span>
                  <span class="option-desc">Soru numaralarını otomatik olarak gizler</span>
                </div>
                <label class="switch">
                  <input type="checkbox" id="opt-hide-numbers" checked>
                  <span class="slider"></span>
                </label>
              </div>
            </div>
            
            <!-- Start Button -->
            <div class="action-row">
              <button class="btn-start" id="btn-start-job" onclick="startCuttingJob()">
                <span>🚀</span> <span id="start-button-label">Otomatik Kesimi Başlat</span>
              </button>
            </div>
          </div>
          
          <!-- Right Column (Page Preview Panel) -->
          <div class="config-right">
            <div class="preview-header-row">
              <span class="preview-title" id="focused-page-title">Sayfa 1 Önizleme</span>
              <span class="pdf-pages-badge" style="background:var(--bg-sidebar)">Önizleme</span>
            </div>
            
            <div class="preview-image-container" id="preview-image-container">
              <img id="preview-image" src="" alt="Sayfa önizlemesi yükleniyor..." class="preview-img">
              <div class="magnifier-lens" id="magnifier-lens"></div>
            </div>
            
            <div class="preview-controls">
              <div class="preview-toggle-row">
                <span class="preview-toggle-label">Kesime Dahil Et</span>
                <label class="switch">
                  <input type="checkbox" id="preview-page-checkbox" checked>
                  <span class="slider"></span>
                </label>
              </div>
              
              <div class="preview-nav-buttons">
                <button class="btn-nav" id="btn-prev-page" onclick="navigatePage(-1)">
                  ◀ Önceki
                </button>
                <button class="btn-nav" id="btn-next-page" onclick="navigatePage(1)">
                  Sonraki ▶
                </button>
              </div>
              
              <div class="keyboard-hint">
                İpucu: Sağ/Sol ok tuşlarıyla geçiş yapabilir, Boşluk (Space) tuşuyla seçimi değiştirebilirsiniz.
              </div>
            </div>
          </div>
        </div>
      </div>
      
    </div>
  </div>

  <!-- Loader Overlay -->
  <div class="loader-overlay" id="loader-overlay">
    <div class="spinner"></div>
    <div class="loader-text" id="loader-title">PDF Analiz Ediliyor</div>
    <div class="loader-sub" id="loader-sub">Lütfen sayfalar okunurken bekleyin...</div>
  </div>

  <script>
    let activeJobId = null;
    let totalPagesCount = 0;
    let currentPageIdx = 1; // 1-based index
    
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('pdf-file-input');
    const loaderOverlay = document.getElementById('loader-overlay');
    const loaderTitle = document.getElementById('loader-title');
    const loaderSub = document.getElementById('loader-sub');
    const configPanel = document.getElementById('config-panel');
    const labelPdfName = document.getElementById('label-pdf-name');
    const labelPdfPages = document.getElementById('label-pdf-pages');
    const pagesGrid = document.getElementById('pages-grid');

    const previewImgContainer = document.getElementById('preview-image-container');
    const previewImg = document.getElementById('preview-image');
    const lens = document.getElementById('magnifier-lens');
    const previewPageCheckbox = document.getElementById('preview-page-checkbox');
    const btnPrevPage = document.getElementById('btn-prev-page');
    const btnNextPage = document.getElementById('btn-next-page');
    const zoomFactor = 2.5;

    document.querySelectorAll('input[name="workflow-mode"]').forEach(input => {
      input.addEventListener('change', updateWorkflowModeUI);
    });

    function selectedWorkflowMode() {
      const selected = document.querySelector('input[name="workflow-mode"]:checked');
      return selected ? selected.value : 'automatic';
    }

    function updateWorkflowModeUI() {
      const label = document.getElementById('start-button-label');
      if (!label) return;
      label.textContent = selectedWorkflowMode() === 'manual'
        ? 'Manuel Düzenleyiciyi Aç'
        : 'Otomatik Kesimi Başlat';
    }

    // Drag and drop event listeners
    dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropzone.classList.add('dragover');
    });
    dropzone.addEventListener('dragleave', () => {
      dropzone.classList.remove('dragover');
    });
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
      const files = e.dataTransfer.files;
      if (files.length > 0 && files[0].type === 'application/pdf') {
        uploadPDF(files[0]);
      } else {
        alert("Lütfen geçerli bir PDF dosyası yükleyin.");
      }
    });

    fileInput.addEventListener('change', (e) => {
      if (fileInput.files.length > 0) {
        uploadPDF(fileInput.files[0]);
      }
    });

    function showLoader(title, sub) {
      loaderTitle.textContent = title;
      loaderSub.textContent = sub;
      loaderOverlay.style.display = 'flex';
    }

    function hideLoader() {
      loaderOverlay.style.display = 'none';
    }

    function uploadPDF(file) {
      showLoader("PDF Analiz Ediliyor", "Lütfen sayfalar okunurken bekleyin...");
      
      const formData = new FormData();
      formData.append('pdf_file', file);
      
      fetch('/jobs/pre-upload', {
        method: 'POST',
        body: formData
      })
      .then(res => {
        if (!res.ok) {
          return res.json().then(data => { throw new Error(data.detail || "Yükleme başarısız") });
        }
        return res.json();
      })
      .then(data => {
        activeJobId = data.job_id;
        totalPagesCount = data.page_count;
        currentPageIdx = 1;
        
        labelPdfName.textContent = data.pdf_name;
        labelPdfPages.textContent = data.page_count + " Sayfa";
        
        // Build pages grid
        pagesGrid.innerHTML = '';
        for (let i = 1; i <= data.page_count; i++) {
          const wrapper = document.createElement('div');
          wrapper.className = 'page-badge-wrapper';
          
          const badge = document.createElement('div');
          badge.className = 'page-badge included';
          badge.id = 'badge-' + i;
          
          const span = document.createElement('span');
          span.textContent = i;
          
          const checkbox = document.createElement('input');
          checkbox.type = 'checkbox';
          checkbox.id = 'chk-page-' + i;
          checkbox.className = 'badge-checkbox';
          checkbox.checked = true;
          
          // Toggle checkbox click logic
          checkbox.addEventListener('click', (e) => {
            e.stopPropagation();
            handleBadgeCheckChange(i, checkbox.checked);
          });
          
          // Badge select focus logic
          badge.addEventListener('click', () => {
            updateFocusedPage(i);
          });
          
          badge.appendChild(span);
          badge.appendChild(checkbox);
          wrapper.appendChild(badge);
          pagesGrid.appendChild(wrapper);
        }
        
        hideLoader();
        configPanel.style.display = 'block';
        
        // Load first page preview
        updateFocusedPage(1);
        
        // Scroll config panel into view
        configPanel.scrollIntoView({ behavior: 'smooth' });
      })
      .catch(err => {
        hideLoader();
        alert("Hata: " + err.message);
      });
    }

    function handleBadgeCheckChange(pageNo, isChecked) {
      const badge = document.getElementById('badge-' + pageNo);
      const chk = document.getElementById('chk-page-' + pageNo);
      chk.checked = isChecked;
      
      if (isChecked) {
        badge.classList.add('included');
      } else {
        badge.classList.remove('included');
      }
      
      // Update preview switch if we are looking at the page
      if (currentPageIdx === pageNo) {
        previewPageCheckbox.checked = isChecked;
      }
    }

    function updateFocusedPage(pageNo) {
      if (pageNo < 1 || pageNo > totalPagesCount) return;
      currentPageIdx = pageNo;
      
      // Update preview title
      document.getElementById('focused-page-title').textContent = "Sayfa " + pageNo + " Önizleme";
      
      // Update active highlight in grid
      document.querySelectorAll('.page-badge').forEach(b => b.classList.remove('active'));
      const activeBadge = document.getElementById('badge-' + pageNo);
      if (activeBadge) {
        activeBadge.classList.add('active');
        activeBadge.scrollIntoView({ behavior: 'auto', block: 'nearest' });
      }
      
      // Set preview checkbox state to match active grid badge check state
      const activeChk = document.getElementById('chk-page-' + pageNo);
      if (activeChk) {
        previewPageCheckbox.checked = activeChk.checked;
      }
      
      // Disable nav buttons if boundaries reached
      btnPrevPage.disabled = (pageNo === 1);
      btnNextPage.disabled = (pageNo === totalPagesCount);
      
      // Load page preview image with opacity loader effect
      previewImg.style.opacity = '0.35';
      previewImg.onload = () => {
        previewImg.style.opacity = '1';
        // Reset magnifier size based on newly loaded image bounds
        if (lens.style.display === 'block') {
          lens.style.backgroundImage = `url('${previewImg.src}')`;
          lens.style.backgroundSize = `${previewImg.width * zoomFactor}px ${previewImg.height * zoomFactor}px`;
        }
      };
      previewImg.src = '/jobs/' + activeJobId + '/preview/page/' + pageNo;
    }

    function navigatePage(dir) {
      updateFocusedPage(currentPageIdx + dir);
    }

    // Toggle inclusion state from preview switch
    previewPageCheckbox.addEventListener('change', (e) => {
      handleBadgeCheckChange(currentPageIdx, e.target.checked);
    });

    function toggleAllPages(status) {
      for (let i = 1; i <= totalPagesCount; i++) {
        handleBadgeCheckChange(i, status);
      }
    }

    // Keyboard controls
    document.addEventListener('keydown', (e) => {
      if (!configPanel || configPanel.style.display !== 'block') return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        if (currentPageIdx > 1) updateFocusedPage(currentPageIdx - 1);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        if (currentPageIdx < totalPagesCount) updateFocusedPage(currentPageIdx + 1);
      } else if (e.key === ' ' || e.code === 'Space') {
        e.preventDefault();
        previewPageCheckbox.checked = !previewPageCheckbox.checked;
        handleBadgeCheckChange(currentPageIdx, previewPageCheckbox.checked);
      }
    });

    // Hover magnifier glass logic
    previewImgContainer.addEventListener('mousemove', moveLens);
    previewImgContainer.addEventListener('mouseenter', showLens);
    previewImgContainer.addEventListener('mouseleave', hideLens);

    function showLens() {
      lens.style.display = 'block';
      lens.style.backgroundImage = `url('${previewImg.src}')`;
      lens.style.backgroundSize = `${previewImg.width * zoomFactor}px ${previewImg.height * zoomFactor}px`;
    }

    function hideLens() {
      lens.style.display = 'none';
    }

    function moveLens(e) {
      const rect = previewImg.getBoundingClientRect();
      const containerRect = previewImgContainer.getBoundingClientRect();
      
      let x = e.clientX - rect.left;
      let y = e.clientY - rect.top;
      
      let lensX = e.clientX - containerRect.left - (lens.offsetWidth / 2);
      let lensY = e.clientY - containerRect.top - (lens.offsetHeight / 2);
      
      lensX = Math.max(0, Math.min(containerRect.width - lens.offsetWidth, lensX));
      lensY = Math.max(0, Math.min(containerRect.height - lens.offsetHeight, lensY));
      
      lens.style.left = lensX + 'px';
      lens.style.top = lensY + 'px';
      
      const bgX = -(x * zoomFactor - (lens.offsetWidth / 2));
      const bgY = -(y * zoomFactor - (lens.offsetHeight / 2));
      
      lens.style.backgroundPosition = `${bgX}px ${bgY}px`;
    }

    function startCuttingJob() {
      if (!activeJobId) return;
      
      const selectedPages = [];
      for (let i = 1; i <= totalPagesCount; i++) {
        const chk = document.getElementById('chk-page-' + i);
        if (chk && chk.checked) {
          selectedPages.push(i);
        }
      }
      
      if (selectedPages.length === 0) {
        alert("Lütfen en az bir sayfa seçin.");
        return;
      }
      
      const startBtn = document.querySelector('.btn-start');
      if (startBtn) {
        startBtn.disabled = true;
        startBtn.style.opacity = '0.6';
        startBtn.style.cursor = 'not-allowed';
        startBtn.textContent = 'Başlatılıyor...';
      }
      
      const pagesStr = selectedPages.join(',');
      const hideNumbers = document.getElementById('opt-hide-numbers').checked;
      const workflowMode = selectedWorkflowMode();
      
      const formData = new FormData();
      formData.append('pages', pagesStr);
      formData.append('hide_question_number', hideNumbers ? '1' : '0');
      formData.append('workflow_mode', workflowMode);
      
      fetch('/jobs/' + activeJobId + '/start', {
        method: 'POST',
        body: formData
      })
      .then(res => res.json())
      .then(data => {
        if (data.redirect_url) {
          window.location.href = data.redirect_url;
        } else {
          if (startBtn) {
            startBtn.disabled = false;
            startBtn.style.opacity = '1';
            startBtn.style.cursor = 'pointer';
            startBtn.innerHTML = '<span>🚀</span> <span id="start-button-label">' +
              (workflowMode === 'manual' ? 'Manuel Düzenleyiciyi Aç' : 'Otomatik Kesimi Başlat') + '</span>';
          }
          alert("Hata: Kesim başlatılamadı.");
        }
      })
      .catch(err => {
        const startBtn = document.querySelector('.btn-start');
        if (startBtn) {
          startBtn.disabled = false;
          startBtn.style.opacity = '1';
          startBtn.style.cursor = 'pointer';
          startBtn.innerHTML = '<span>🚀</span> <span id="start-button-label">' +
            (workflowMode === 'manual' ? 'Manuel Düzenleyiciyi Aç' : 'Otomatik Kesimi Başlat') + '</span>';
        }
        alert("Hata: " + err.message);
      });
    }
  </script>
</body>
</html>"""
    html_content = html_content.replace("{font_links}", font_links)
    html_content = html_content.replace("{history_link}", history_link)
    html_content = html_content.replace("{offline_link}", offline_link)
    return HTMLResponse(html_content)


@app.get("/jobs/history", response_class=HTMLResponse)
def jobs_history() -> HTMLResponse:
    if not LOCAL_MODE:
        raise HTTPException(status_code=404, detail="İş geçmişi sadece lokal modda açık")
    rows = load_job_history()
    if not rows:
        body = """
        <section class="hero">
          <span class="eyebrow">İş geçmişi</span>
          <h1>Kayıt bulunamadı.</h1>
        </section>
        <section class="card">
          <div class="actions">
            <a class="btn ghost" href="/">Ana sayfa</a>
          </div>
        </section>
        """
        return page_shell("İş geçmişi", body)

    items = []
    for row in rows:
        status = str(row["status"])
        if status == "completed":
            status_label = "Tamamlandı"
        elif status == "processing":
            status_label = "İşleniyor"
        elif status == "queued":
            status_label = "Sırada"
        elif status == "error":
            status_label = "Hata"
        elif status == "empty":
            status_label = "Boş sonuç"
        else:
            status_label = status
        profile_text = str(row["profile_key"])[:16] if row["profile_key"] else "-"
        items.append(
            f"""
            <tr>
              <td><a href="/jobs/{quote(str(row['job_id']))}">{html.escape(str(row['pdf_name']))}</a></td>
              <td>{html.escape(format_ts(int(row["created_at"])))}</td>
              <td>{html.escape(status_label)}</td>
              <td>%{float(row["success_rate"]):.1f}</td>
              <td>{html.escape(profile_text)}</td>
              <td>{int(row["feedback_count"])}</td>
            </tr>
            """
        )

    body = f"""
    <section class="hero">
      <span class="eyebrow">İş geçmişi</span>
      <h1>Son işlemler.</h1>
    </section>
    <section class="card">
      <div class="actions" style="margin-bottom:12px;">
        <a class="btn ghost" href="/">Ana sayfa</a>
      </div>
      <div style="overflow-x:auto;">
        <table style="width:100%; border-collapse:collapse;">
          <thead>
            <tr>
              <th style="text-align:left; padding:8px; border-bottom:1px solid var(--line);">PDF</th>
              <th style="text-align:left; padding:8px; border-bottom:1px solid var(--line);">Tarih</th>
              <th style="text-align:left; padding:8px; border-bottom:1px solid var(--line);">Durum</th>
              <th style="text-align:left; padding:8px; border-bottom:1px solid var(--line);">Başarı</th>
              <th style="text-align:left; padding:8px; border-bottom:1px solid var(--line);">Profil</th>
              <th style="text-align:left; padding:8px; border-bottom:1px solid var(--line);">Feedback</th>
            </tr>
          </thead>
          <tbody>
            {''.join(items)}
          </tbody>
        </table>
      </div>
    </section>
    """
    return page_shell("İş geçmişi", body)


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    pdf_file: UploadFile = File(...),
    hide_question_number: str | None = Form(default=None),
) -> HTMLResponse:
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Yalnızca PDF yükleyebilirsin")

    ensure_cleanup_worker_started()

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_ROOT / job_id
    input_dir = job_dir / "input"
    out_dir = job_dir / "output"
    ensure_dir(input_dir)
    ensure_dir(out_dir)

    pdf_name = sanitize_filename(Path(pdf_file.filename).name)
    pdf_path = input_dir / pdf_name
    with pdf_path.open("wb") as handle:
        shutil.copyfileobj(pdf_file.file, handle)

    create_job_meta(job_dir, pdf_name)
    update_job_meta(job_dir, hide_question_number=bool(hide_question_number is not None))
    thread = threading.Thread(
        target=process_job,
        kwargs={
            "job_dir": job_dir,
            "pdf_path": pdf_path,
            "out_dir": out_dir,
            "hide_question_number": hide_question_number is not None,
            "single_question_pdfs": False,
        },
        name=f"pdf-cut-job-{job_id}",
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url=f"/jobs/{job_id}/easy-editor", status_code=303)


@app.post("/jobs/pre-upload")
async def pre_upload(
    pdf_file: UploadFile = File(...),
) -> JSONResponse:
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Yalnızca PDF yükleyebilirsin")

    ensure_cleanup_worker_started()

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_ROOT / job_id
    input_dir = job_dir / "input"
    out_dir = job_dir / "output"
    ensure_dir(input_dir)
    ensure_dir(out_dir)

    pdf_name = sanitize_filename(Path(pdf_file.filename).name)
    pdf_path = input_dir / pdf_name
    with pdf_path.open("wb") as handle:
        shutil.copyfileobj(pdf_file.file, handle)

    try:
        reader = PdfReader(pdf_path)
        page_count = len(reader.pages)
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"PDF dosyası okunamadı: {exc}")

    created_at = now_ts()
    meta: dict = {
        "pdf_name": pdf_name,
        "created_at": created_at,
        "completed_at": 0,
        "expires_at": 0,
        "status": "pre_upload",
        "message": "PDF yüklendi, kesim yapılandırması bekleniyor",
        "current_test": "",
        "current_question": "",
        "current_page": "",
        "progress_percent": 0,
        "processor_pid": 0,
        "last_touch_at": 0,
    }
    job_meta_path(job_dir).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    return JSONResponse({
        "job_id": job_id,
        "page_count": page_count,
        "pdf_name": pdf_name
    })


@app.post("/jobs/{job_id}/start")
async def start_job(
    job_id: str,
    pages: str | None = Form(default=None),
    hide_question_number: str | None = Form(default=None),
    workflow_mode: str = Form(default="automatic"),
) -> JSONResponse:
    try:
        job_dir = safe_job_dir(job_id)
        meta = read_job_meta(job_dir)
    except HTTPException:
        raise HTTPException(status_code=404, detail="İş bulunamadı")

    pdf_name = meta.get("pdf_name") or ""
    pdf_path = job_dir / "input" / pdf_name
    out_dir = job_dir / "output"

    workflow_mode = workflow_mode.strip().lower()
    if workflow_mode not in {"automatic", "manual"}:
        raise HTTPException(status_code=400, detail="Geçersiz kesim modu")

    hide_numbers = hide_question_number == "1" or hide_question_number == "true"

    if workflow_mode == "manual":
        ensure_dir(out_dir)
        manifest_path = out_dir / f"{Path(pdf_name).stem}_crop_manifest.json"
        manifest_path.write_text("[]\n", encoding="utf-8")
        completed_at = now_ts()
        update_job_meta(
            job_dir,
            status="completed",
            message="Manuel kesim için düzenleyici hazır",
            completed_at=completed_at,
            expires_at=completed_at + active_job_ttl_seconds(),
            progress_percent=100,
            processor_pid=0,
            selected_pages=pages or "",
            hide_question_number=hide_numbers,
            workflow_mode="manual",
        )
        append_log(job_dir, "[MANUEL] Otomatik kesim atlandı; düzenleyici boş olarak hazırlandı.\n")
        return JSONResponse({
            "status": "started",
            "mode": "manual",
            "redirect_url": f"/jobs/{job_id}/easy-editor",
        })

    update_job_meta(
        job_dir,
        status="queued",
        message="İşlem sıraya alındı, analiz başlatılıyor",
        progress_percent=5,
        selected_pages=pages or "",
        hide_question_number=hide_numbers,
        workflow_mode="automatic",
    )

    thread = threading.Thread(
        target=process_job,
        kwargs={
            "job_dir": job_dir,
            "pdf_path": pdf_path,
            "out_dir": out_dir,
            "hide_question_number": hide_numbers,
            "single_question_pdfs": False,
            "pages": pages,
        },
        name=f"pdf-cut-job-{job_id}",
        daemon=True,
    )
    thread.start()

    return JSONResponse({
        "status": "started",
        "redirect_url": f"/jobs/{job_id}/easy-editor"
    })


def find_input_pdf(job_dir: Path) -> Path:
    input_dir = job_dir / "input"
    pdf_files = list(input_dir.glob("*.pdf"))
    if not pdf_files:
        raise HTTPException(status_code=404, detail="PDF dosyası bulunamadı")
    return pdf_files[0]


@app.get("/jobs/{job_id}/preview/page/{page_no}")
def get_pdf_page_preview(job_id: str, page_no: int, dpi: int = 150) -> FileResponse:
    try:
        job_dir = safe_job_dir(job_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="İş bulunamadı")

    pdf_path = find_input_pdf(job_dir)
    cache_dir = job_dir / "preview_cache"
    ensure_dir(cache_dir)
    out_prefix = cache_dir / f"page_preview_{page_no}_{dpi}"
    image_path = out_prefix.with_suffix(".png")

    if not image_path.exists():
        result = subprocess.run(
            [
                get_binary_path("pdftocairo"),
                "-png",
                "-singlefile",
                "-r",
                str(dpi),
                "-f",
                str(page_no),
                "-l",
                str(page_no),
                str(pdf_path),
                str(out_prefix),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=processor_env(),
        )
        if result.returncode != 0 or not image_path.exists():
            raise HTTPException(status_code=500, detail="Sayfa önizlemesi üretilemedi")

    return FileResponse(image_path, media_type="image/png")


@app.get("/jobs/{job_id}", response_class=RedirectResponse)
def job_results(job_id: str) -> RedirectResponse:
    return RedirectResponse(url=f"/jobs/{job_id}/easy-editor", status_code=303)


@app.post("/jobs/{job_id}/delete")
def delete_job(job_id: str) -> RedirectResponse:
    try:
        job_dir = safe_job_dir(job_id)
        meta = read_job_meta(job_dir)
    except HTTPException:
        return RedirectResponse(url="/", status_code=303)

    if str(meta.get("status") or "") not in {"queued", "processing"}:
        cleanup_job_dir(job_dir)
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> RedirectResponse:
    try:
        job_dir = safe_job_dir(job_id)
        meta = read_job_meta(job_dir)
    except HTTPException:
        return RedirectResponse(url="/", status_code=303)

    status = str(meta.get("status") or "")
    if status not in {"queued", "processing"}:
        return RedirectResponse(url=f"/jobs/{quote(job_id)}", status_code=303)

    pid = int(meta.get("processor_pid") or 0)
    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass

    append_log(job_dir, "\n[WEB] İşlem kullanıcı tarafından iptal edildi.\n")
    update_job_meta(
        job_dir,
        status="cancelled",
        message="İşlem iptal edildi",
        error="İşlem kullanıcı tarafından iptal edildi.",
        expires_at=now_ts() + ERROR_JOB_TTL_SECONDS,
        progress_percent=100,
        processor_pid=0,
    )
    return RedirectResponse(url=f"/jobs/{quote(job_id)}", status_code=303)


@app.get("/jobs/{job_id}/status")
def job_status(job_id: str) -> JSONResponse:
    try:
        job_dir, meta = get_active_job(job_id)
    except HTTPException as exc:
        if exc.status_code == 410:
            return JSONResponse({"status": "expired", "message": "Bu işlem için bekleme süresi doldu"}, status_code=410)
        raise

    payload = {
        "status": str(meta.get("status") or ""),
        "message": str(meta.get("message") or ""),
        "current_test": str(meta.get("current_test") or ""),
        "current_question": str(meta.get("current_question") or ""),
        "current_page": str(meta.get("current_page") or ""),
        "progress_percent": int(meta.get("progress_percent") or 0),
        "error": str(meta.get("error") or ""),
        "log_tail": log_tail(job_dir),
        "log_download_url": f"/jobs/{quote(job_id)}/download/log",
    }
    return JSONResponse(payload)


@app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
def job_review(job_id: str, saved: str | None = None, focus: int | None = None) -> HTMLResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Kontrol ekranı kapalı")
    try:
        job_dir, meta = get_active_job(job_id)
    except HTTPException as exc:
        if exc.status_code in {404, 410}:
            return expired_page()
        raise
    status = str(meta.get("status") or "")
    if status in {"queued", "processing"}:
        return progress_page(job_id, str(meta["pdf_name"]))
    if status in {"error", "empty"}:
        return job_error_page(job_id, meta)
    return review_page(job_id, meta, saved == "1", focus)


@app.post("/jobs/{job_id}/touch")
def touch_job(job_id: str) -> JSONResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Kontrol ekranı kapalı")
    try:
        job_dir, meta = get_active_job(job_id)
    except HTTPException as exc:
        if exc.status_code in {404, 410}:
            return JSONResponse({"status": "expired", "message": "İş bulunamadı veya süresi doldu"}, status_code=410)
        raise

    status = str(meta.get("status") or "")
    if status in {"queued", "processing", "error", "empty", "cancelled"}:
        return JSONResponse({"status": status, "expires_at": int(meta.get("expires_at") or 0)})

    now = now_ts()
    last_touch_at = int(meta.get("last_touch_at") or 0)
    if now - last_touch_at < TOUCH_MIN_INTERVAL_SECONDS:
        retry_after = TOUCH_MIN_INTERVAL_SECONDS - max(0, now - last_touch_at)
        return JSONResponse(
            {
                "status": "throttled",
                "message": "Touch isteği çok sık gönderildi",
                "expires_at": int(meta.get("expires_at") or 0),
                "retry_after": retry_after,
            },
            status_code=429,
        )

    updated = update_job_meta(
        job_dir,
        expires_at=now + active_job_ttl_seconds(),
        last_touch_at=now,
    )
    return JSONResponse({"status": "ok", "expires_at": int(updated.get("expires_at") or 0)})


@app.get("/jobs/{job_id}/crop-editor", response_class=HTMLResponse)
def job_crop_editor(job_id: str, row_id: int) -> HTMLResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Sınır düzenleme kapalı")
    return crop_editor_page(job_id, row_id)


@app.get("/jobs/{job_id}/common-stem-editor", response_class=HTMLResponse)
def job_common_stem_editor(job_id: str, row_id: int) -> HTMLResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Ortak kök düzenleme kapalı")
    return common_stem_editor_page(job_id, row_id)


@app.get("/jobs/{job_id}/page-editor", response_class=HTMLResponse)
def job_page_editor(job_id: str, row_id: int, saved: str | None = None) -> HTMLResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Sayfa düzenleme kapalı")
    return page_editor_page(job_id, row_id, saved == "1")


def record_feedback_from_row(
    row: dict[str, str | int],
    issue_code: str,
    note: str = "",
    *,
    job_id: str | None = None,
    row_id: int | None = None,
) -> None:
    profile_key = str(row.get("profile_key") or "")
    if not profile_key:
        return
    CropFeedbackStore().record(
        ManualFeedback(
            profile_key=profile_key,
            source_pdf=str(row.get("source_pdf") or ""),
            test_no=int(row.get("test_no") or 0),
            test_name=str(row.get("test_name") or ""),
            soru_no=int(row.get("soru_no") or 0),
            page_idx=int(row.get("page_idx") or 0),
            issue_code=issue_code,
            note=note.strip()[:500],
            output_path=str(row.get("question_pdf") or ""),
        )
    )
    if job_id is not None:
        archive_feedback_event(job_id, row, issue_code, note, kind="issue", row_id=row_id)


@app.post("/jobs/{job_id}/feedback")
def save_feedback(
    job_id: str,
    profile_key: str = Form(...),
    source_pdf: str = Form(...),
    test_no: int = Form(...),
    test_name: str = Form(...),
    soru_no: int = Form(...),
    page_idx: int = Form(...),
    row_id: int | None = Form(default=None),
    issue_code: str = Form(...),
    note: str = Form(default=""),
    output_path: str = Form(default=""),
) -> RedirectResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Geri bildirim kapalı")
    get_active_job(job_id)
    if not profile_key:
        raise HTTPException(status_code=400, detail="Öğrenme profili bulunamadı")
    if issue_code not in ALLOWED_FEEDBACK_ISSUES:
        raise HTTPException(status_code=400, detail="Geçersiz hata türü")
    CropFeedbackStore().record(
        ManualFeedback(
            profile_key=profile_key,
            source_pdf=source_pdf,
            test_no=test_no,
            test_name=test_name,
            soru_no=soru_no,
            page_idx=page_idx,
            issue_code=issue_code,
            note=note.strip()[:500],
            output_path=output_path,
        )
    )
    archive_feedback_event(
        job_id,
        {
            "profile_key": profile_key,
            "source_pdf": source_pdf,
            "test_no": test_no,
            "test_name": test_name,
            "soru_no": soru_no,
            "page_idx": page_idx,
            "question_pdf": output_path,
        },
        issue_code,
        note,
        kind="issue",
        row_id=row_id,
    )
    return RedirectResponse(url=review_focus_url(job_id, row_id, saved=True), status_code=303)


@app.post("/jobs/{job_id}/feedback/bounds")
def save_bounds_feedback(
    job_id: str,
    row_id: int = Form(...),
    crop_left: float = Form(...),
    crop_top: float = Form(...),
    crop_right: float = Form(...),
    crop_bottom: float = Form(...),
    issue_code: str = Form(...),
    note: str = Form(default=""),
) -> RedirectResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Sınır düzenleme kapalı")
    job_dir, _ = get_active_job(job_id)
    if issue_code not in ALLOWED_FEEDBACK_ISSUES:
        raise HTTPException(status_code=400, detail="Geçersiz hata türü")
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    row = rows[row_id]
    profile_key = str(row.get("profile_key") or "")
    if not profile_key:
        raise HTTPException(status_code=400, detail="Öğrenme profili bulunamadı")
    layout = source_page_layout(job_id, row_id)
    page_width = float(layout["width_px"])
    page_height = float(layout["height_px"])
    bounds = {
        "crop_left": max(0.0, min(float(crop_left), page_width)),
        "crop_top": max(0.0, min(float(crop_top), page_height)),
        "crop_right": max(0.0, min(float(crop_right), page_width)),
        "crop_bottom": max(0.0, min(float(crop_bottom), page_height)),
    }
    if bounds["crop_right"] <= bounds["crop_left"] + 24.0 or bounds["crop_bottom"] <= bounds["crop_top"] + 24.0:
        raise HTTPException(status_code=400, detail="Seçilen sınır çok küçük")
    store = CropFeedbackStore()
    store.record_bounds(
        profile_key=profile_key,
        source_pdf=str(row.get("source_pdf") or ""),
        test_no=int(row.get("test_no") or 0),
        test_name=str(row.get("test_name") or ""),
        soru_no=int(row.get("soru_no") or 0),
        page_idx=int(row.get("page_idx") or 0),
        bounds=bounds,
        note=note.strip()[:500],
    )
    store.record(
        ManualFeedback(
            profile_key=profile_key,
            source_pdf=str(row.get("source_pdf") or ""),
            test_no=int(row.get("test_no") or 0),
            test_name=str(row.get("test_name") or ""),
            soru_no=int(row.get("soru_no") or 0),
            page_idx=int(row.get("page_idx") or 0),
            issue_code=issue_code,
            note=note.strip()[:500],
            output_path=str(row.get("question_pdf") or ""),
        )
    )
    archive_feedback_event(job_id, row, issue_code, note, kind="manual_bounds", row_id=row_id, bounds=bounds)
    update_manifest_bounds(job_dir, row, bounds)
    refresh_current_question_pdf(job_id, row_id)
    return RedirectResponse(url=review_focus_url(job_id, row_id, saved=True), status_code=303)


@app.post("/jobs/{job_id}/feedback/common-stem")
def save_common_stem_feedback(
    job_id: str,
    row_id: int = Form(...),
    target_soru_no: list[int] | None = Form(default=None),
    crop_left: float = Form(...),
    crop_top: float = Form(...),
    crop_right: float = Form(...),
    crop_bottom: float = Form(...),
    placement: str = Form(default="top"),
    note: str = Form(default=""),
) -> RedirectResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Ortak kök düzenleme kapalı")
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    source_row = rows[row_id]
    profile_key = str(source_row.get("profile_key") or "")
    if not profile_key:
        raise HTTPException(status_code=400, detail="Öğrenme profili bulunamadı")

    targets = sorted({int(number) for number in (target_soru_no or []) if int(number) > 0})
    valid_targets = {
        int(row.get("soru_no") or 0)
        for row in rows
        if str(row.get("source_pdf") or "") == str(source_row.get("source_pdf") or "")
        and int(row.get("test_no") or 0) == int(source_row.get("test_no") or 0)
    }
    targets = [number for number in targets if number in valid_targets]
    if not targets:
        raise HTTPException(status_code=400, detail="En az bir geçerli hedef soru seçilmelidir")

    layout = source_page_layout(job_id, row_id)
    page_width = float(layout["width_px"])
    page_height = float(layout["height_px"])
    bounds = {
        "crop_left": max(0.0, min(float(crop_left), page_width)),
        "crop_top": max(0.0, min(float(crop_top), page_height)),
        "crop_right": max(0.0, min(float(crop_right), page_width)),
        "crop_bottom": max(0.0, min(float(crop_bottom), page_height)),
    }
    if bounds["crop_right"] <= bounds["crop_left"] + 24.0 or bounds["crop_bottom"] <= bounds["crop_top"] + 24.0:
        raise HTTPException(status_code=400, detail="Seçilen ortak kök sınırı çok küçük")
    clean_placement = "left" if placement == "left" else "top"

    store = CropFeedbackStore()
    store.record_common_stem(
        profile_key=profile_key,
        source_pdf=str(source_row.get("source_pdf") or ""),
        test_no=int(source_row.get("test_no") or 0),
        test_name=str(source_row.get("test_name") or ""),
        source_soru_no=int(source_row.get("soru_no") or 0),
        source_page_idx=int(source_row.get("page_idx") or 0),
        target_soru_nos=targets,
        bounds=bounds,
        placement=clean_placement,
        note=note.strip()[:500],
    )
    update_manifest_common_stem(job_dir, source_row, targets, bounds, clean_placement)

    refreshed_rows = load_review_rows(job_dir / "output")
    for target_index, target_row in enumerate(refreshed_rows):
        if (
            str(target_row.get("source_pdf") or "") == str(source_row.get("source_pdf") or "")
            and int(target_row.get("test_no") or 0) == int(source_row.get("test_no") or 0)
            and int(target_row.get("soru_no") or 0) in targets
        ):
            refresh_current_question_pdf(job_id, target_index)

    archive_feedback_event(
        job_id,
        source_row,
        "missing_common_stem",
        note,
        kind="manual_common_stem",
        row_id=row_id,
        bounds=bounds,
        common_stem={
            "target_soru_nos": targets,
            "placement": clean_placement,
            "source_page_idx": int(source_row.get("page_idx") or 0),
            "bounds": bounds,
        },
    )
    return RedirectResponse(url=f"/jobs/{quote(job_id)}/review?saved=1", status_code=303)


@app.post("/jobs/{job_id}/feedback/page-edits")
def save_page_edits_feedback(
    job_id: str,
    row_id: int = Form(...),
    edits_json: str = Form(...),
    note: str = Form(default=""),
) -> RedirectResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Sayfa düzenleme kapalı")
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")

    try:
        payload = json.loads(edits_json or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Düzenleme verisi okunamadı")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Düzenleme verisi geçersiz")

    source_row = rows[row_id]
    source_pdf = str(source_row.get("source_pdf") or "")
    page_idx = int(source_row.get("page_idx") or 0)
    layout = source_page_layout(job_id, row_id)
    page_width = float(layout["width_px"])
    page_height = float(layout["height_px"])

    def clean_bounds(raw_bounds: dict) -> dict[str, float]:
        try:
            bounds = {
                "crop_left": max(0.0, min(float(raw_bounds["crop_left"]), page_width)),
                "crop_top": max(0.0, min(float(raw_bounds["crop_top"]), page_height)),
                "crop_right": max(0.0, min(float(raw_bounds["crop_right"]), page_width)),
                "crop_bottom": max(0.0, min(float(raw_bounds["crop_bottom"]), page_height)),
            }
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Sınır bilgisi geçersiz")
        if bounds["crop_right"] <= bounds["crop_left"] + 24.0 or bounds["crop_bottom"] <= bounds["crop_top"] + 24.0:
            raise HTTPException(status_code=400, detail="Seçilen sınır çok küçük")
        return bounds

    store = CropFeedbackStore()
    refresh_row_ids: set[int] = set()
    archived_count = 0

    for question_edit in payload.get("questions", []) or []:
        if not isinstance(question_edit, dict):
            continue
        try:
            edit_row_id = int(question_edit.get("row_id"))
        except (TypeError, ValueError):
            edit_row_id = -1
        if edit_row_id < 0 or edit_row_id >= len(rows):
            continue
        row = rows[edit_row_id]
        if (
            str(row.get("source_pdf") or "") != source_pdf
            or int(row.get("page_idx") or 0) != page_idx
            or not row.get("profile_key")
        ):
            continue
        issue_code = str(question_edit.get("issue_code") or "bottom_cut")
        if issue_code not in ALLOWED_FEEDBACK_ISSUES:
            issue_code = "bottom_cut"
        bounds = clean_bounds(question_edit.get("bounds") or {})
        store.record_bounds(
            profile_key=str(row.get("profile_key") or ""),
            source_pdf=str(row.get("source_pdf") or ""),
            test_no=int(row.get("test_no") or 0),
            test_name=str(row.get("test_name") or ""),
            soru_no=int(row.get("soru_no") or 0),
            page_idx=int(row.get("page_idx") or 0),
            bounds=bounds,
            note=note.strip()[:500],
        )
        store.record(
            ManualFeedback(
                profile_key=str(row.get("profile_key") or ""),
                source_pdf=str(row.get("source_pdf") or ""),
                test_no=int(row.get("test_no") or 0),
                test_name=str(row.get("test_name") or ""),
                soru_no=int(row.get("soru_no") or 0),
                page_idx=int(row.get("page_idx") or 0),
                issue_code=issue_code,
                note=note.strip()[:500],
                output_path=str(row.get("question_pdf") or ""),
            )
        )
        update_manifest_bounds(job_dir, row, bounds)
        archive_feedback_event(job_id, row, issue_code, note, kind="manual_bounds", row_id=edit_row_id, bounds=bounds)
        refresh_row_ids.add(edit_row_id)
        archived_count += 1

    for new_question in payload.get("new_questions", []) or []:
        if not isinstance(new_question, dict):
            continue
        try:
            source_row_id = int(new_question.get("source_row_id"))
            soru_no = int(new_question.get("soru_no"))
        except (TypeError, ValueError):
            continue
        if source_row_id < 0 or source_row_id >= len(rows) or soru_no <= 0:
            continue
        template_row = rows[source_row_id]
        if (
            str(template_row.get("source_pdf") or "") != source_pdf
            or int(template_row.get("page_idx") or 0) != page_idx
            or not template_row.get("profile_key")
        ):
            continue
        issue_code = str(new_question.get("issue_code") or "bottom_cut")
        if issue_code not in ALLOWED_FEEDBACK_ISSUES:
            issue_code = "bottom_cut"
        bounds = clean_bounds(new_question.get("bounds") or {})
        add_manifest_question(job_dir, template_row, soru_no, bounds)
        rows = load_review_rows(job_dir / "output")
        new_row_id = next(
            (
                index
                for index, candidate in enumerate(rows)
                if str(candidate.get("source_pdf") or "") == source_pdf
                and int(candidate.get("test_no") or 0) == int(template_row.get("test_no") or 0)
                and int(candidate.get("soru_no") or 0) == soru_no
                and int(candidate.get("page_idx") or 0) == page_idx
            ),
            -1,
        )
        if new_row_id < 0:
            raise HTTPException(status_code=500, detail="Eklenen soru kaydı bulunamadı")
        new_row = rows[new_row_id]
        store.record_bounds(
            profile_key=str(new_row.get("profile_key") or ""),
            source_pdf=str(new_row.get("source_pdf") or ""),
            test_no=int(new_row.get("test_no") or 0),
            test_name=str(new_row.get("test_name") or ""),
            soru_no=int(new_row.get("soru_no") or 0),
            page_idx=int(new_row.get("page_idx") or 0),
            bounds=bounds,
            note=note.strip()[:500],
        )
        store.record(
            ManualFeedback(
                profile_key=str(new_row.get("profile_key") or ""),
                source_pdf=str(new_row.get("source_pdf") or ""),
                test_no=int(new_row.get("test_no") or 0),
                test_name=str(new_row.get("test_name") or ""),
                soru_no=int(new_row.get("soru_no") or 0),
                page_idx=int(new_row.get("page_idx") or 0),
                issue_code=issue_code,
                note=note.strip()[:500],
                output_path=str(new_row.get("question_pdf") or ""),
            )
        )
        refresh_current_question_pdf(job_id, new_row_id)
        archive_feedback_event(job_id, new_row, issue_code, note, kind="manual_added_question", row_id=new_row_id, bounds=bounds)
        archived_count += 1

    for stem_edit in payload.get("common_stems", []) or []:
        if not isinstance(stem_edit, dict):
            continue
        try:
            source_row_id = int(stem_edit.get("source_row_id"))
        except (TypeError, ValueError):
            source_row_id = -1
        if source_row_id < 0 or source_row_id >= len(rows):
            continue
        stem_source = rows[source_row_id]
        if (
            str(stem_source.get("source_pdf") or "") != source_pdf
            or int(stem_source.get("page_idx") or 0) != page_idx
            or not stem_source.get("profile_key")
        ):
            continue
        raw_targets = stem_edit.get("target_soru_no") or []
        if not isinstance(raw_targets, list):
            continue
        targets = sorted({int(number) for number in raw_targets if int(number) > 0})
        valid_targets = {
            int(row.get("soru_no") or 0)
            for row in rows
            if str(row.get("source_pdf") or "") == source_pdf
            and int(row.get("test_no") or 0) == int(stem_source.get("test_no") or 0)
            and int(row.get("page_idx") or 0) == page_idx
        }
        targets = [number for number in targets if number in valid_targets]
        if not targets:
            continue
        bounds = clean_bounds(stem_edit.get("bounds") or {})
        clean_placement = "left" if str(stem_edit.get("placement") or "") == "left" else "top"
        store.record_common_stem(
            profile_key=str(stem_source.get("profile_key") or ""),
            source_pdf=str(stem_source.get("source_pdf") or ""),
            test_no=int(stem_source.get("test_no") or 0),
            test_name=str(stem_source.get("test_name") or ""),
            source_soru_no=int(stem_source.get("soru_no") or 0),
            source_page_idx=int(stem_source.get("page_idx") or 0),
            target_soru_nos=targets,
            bounds=bounds,
            placement=clean_placement,
            note=note.strip()[:500],
        )
        update_manifest_common_stem(job_dir, stem_source, targets, bounds, clean_placement)
        for target_index, target_row in enumerate(load_review_rows(job_dir / "output")):
            if (
                str(target_row.get("source_pdf") or "") == source_pdf
                and int(target_row.get("test_no") or 0) == int(stem_source.get("test_no") or 0)
                and int(target_row.get("soru_no") or 0) in targets
            ):
                refresh_row_ids.add(target_index)
        archive_feedback_event(
            job_id,
            stem_source,
            "missing_common_stem",
            note,
            kind="manual_common_stem",
            row_id=source_row_id,
            bounds=bounds,
            common_stem={
                "target_soru_nos": targets,
                "placement": clean_placement,
                "source_page_idx": int(stem_source.get("page_idx") or 0),
                "bounds": bounds,
            },
        )
        archived_count += 1

    for refresh_row_id in sorted(refresh_row_ids):
        refresh_current_question_pdf(job_id, refresh_row_id)

    if archived_count <= 0:
        return RedirectResponse(url=page_editor_url(job_id, row_id, saved=False), status_code=303)
    return RedirectResponse(url=page_editor_url(job_id, row_id, saved=True), status_code=303)


@app.post("/jobs/{job_id}/feedback/bulk")
def save_bulk_feedback(
    job_id: str,
    row_id: list[int] | None = Form(default=None),
    issue_code: str = Form(...),
    note: str = Form(default=""),
) -> RedirectResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Geri bildirim kapalı")
    job_dir, _ = get_active_job(job_id)
    if issue_code not in ALLOWED_FEEDBACK_ISSUES:
        raise HTTPException(status_code=400, detail="Geçersiz hata türü")
    rows = load_review_rows(job_dir / "output")
    selected = row_id or []
    if not selected:
        raise HTTPException(status_code=400, detail="En az bir soru seçilmelidir")
    saved_count = 0
    for index in selected:
        if index < 0 or index >= len(rows):
            continue
        row = rows[index]
        if not row.get("profile_key"):
            continue
        record_feedback_from_row(row, issue_code, note, job_id=job_id, row_id=index)
        saved_count += 1
    if saved_count <= 0:
        raise HTTPException(status_code=400, detail="Kaydedilecek geçerli soru bulunamadı")
    return RedirectResponse(url=f"/jobs/{quote(job_id)}/review?saved=1", status_code=303)


@app.post("/jobs/{job_id}/feedback/bulk-bounds")
def save_bulk_bounds_feedback(
    job_id: str,
    row_id: list[int] | None = Form(default=None),
    operation: str = Form(...),
    amount: float = Form(default=12.0),
    note: str = Form(default=""),
) -> RedirectResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Toplu sınır düzenleme kapalı")
    job_dir, _ = get_active_job(job_id)
    if operation not in BULK_BOUNDS_OPERATIONS:
        raise HTTPException(status_code=400, detail="Geçersiz toplu sınır işlemi")
    rows = load_review_rows(job_dir / "output")
    selected = sorted({int(index) for index in (row_id or []) if int(index) >= 0})
    if not selected:
        raise HTTPException(status_code=400, detail="En az bir soru seçilmelidir")

    op_meta = BULK_BOUNDS_OPERATIONS[operation]
    issue_code = str(op_meta["issue"])
    op_label = str(op_meta["label"])
    clean_note = note.strip()[:500]
    if clean_note:
        clean_note = f"{op_label}. {clean_note}"
    else:
        clean_note = op_label

    store = CropFeedbackStore()
    saved_indices: list[int] = []
    for index in selected:
        if index < 0 or index >= len(rows):
            continue
        row = rows[index]
        profile_key = str(row.get("profile_key") or "")
        if not profile_key:
            continue
        layout = source_page_layout(job_id, index)
        bounds = bulk_adjust_bounds(
            row,
            operation,
            amount,
            page_width=float(layout["width_px"]),
            page_height=float(layout["height_px"]),
        )
        if bounds is None:
            continue
        store.record_bounds(
            profile_key=profile_key,
            source_pdf=str(row.get("source_pdf") or ""),
            test_no=int(row.get("test_no") or 0),
            test_name=str(row.get("test_name") or ""),
            soru_no=int(row.get("soru_no") or 0),
            page_idx=int(row.get("page_idx") or 0),
            bounds=bounds,
            note=clean_note,
        )
        store.record(
            ManualFeedback(
                profile_key=profile_key,
                source_pdf=str(row.get("source_pdf") or ""),
                test_no=int(row.get("test_no") or 0),
                test_name=str(row.get("test_name") or ""),
                soru_no=int(row.get("soru_no") or 0),
                page_idx=int(row.get("page_idx") or 0),
                issue_code=issue_code,
                note=clean_note,
                output_path=str(row.get("question_pdf") or ""),
            )
        )
        archive_feedback_event(job_id, row, issue_code, clean_note, kind="manual_bounds", row_id=index, bounds=bounds)
        update_manifest_bounds(job_dir, row, bounds)
        refresh_current_question_pdf(job_id, index)
        saved_indices.append(index)

    if not saved_indices:
        raise HTTPException(status_code=400, detail="Düzenlenecek geçerli soru bulunamadı")
    return RedirectResponse(url=review_focus_url(job_id, saved_indices[0], saved=True), status_code=303)


@app.post("/jobs/{job_id}/feedback/toggle-number-visibility")
def toggle_number_visibility(
    job_id: str,
    row_id: int = Form(...),
) -> RedirectResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Soru numarası gizleme kapalı")
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")
    if row_id < 0 or row_id >= len(rows):
        raise HTTPException(status_code=404, detail="Soru kaydı bulunamadı")
    current_row = rows[row_id]

    current_state = bool(int(current_row.get("question_number_hidden") or 0))
    new_state = not current_state

    update_manifest_question_number_hidden(job_dir, current_row, new_state)
    refresh_current_question_pdf(job_id, row_id)

    return RedirectResponse(url=review_focus_url(job_id, row_id, saved=True), status_code=303)


@app.post("/jobs/{job_id}/feedback/hide-question-numbers")
def hide_question_numbers_after_cut(
    job_id: str,
    row_id: list[int] | None = Form(default=None),
    scope: str = Form(default="selected"),
) -> RedirectResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Soru numarası gizleme kapalı")
    job_dir, _ = get_active_job(job_id)
    rows = load_review_rows(job_dir / "output")

    hidden = True
    if scope.endswith("_show"):
        hidden = False
        base_scope = scope[:-5]
    else:
        base_scope = scope

    if base_scope == "all":
        selected = list(range(len(rows)))
    else:
        selected = sorted({int(index) for index in (row_id or []) if int(index) >= 0})
    if not selected:
        raise HTTPException(status_code=400, detail="En az bir soru seçilmelidir")

    selected = [index for index in selected if 0 <= index < len(rows) and rows[index].get("question_pdf")]
    if not selected:
        raise HTTPException(status_code=400, detail=f"{'Gizlenecek' if hidden else 'Gösterilecektir'} geçerli soru bulunamadı")

    action_text = "gizleme" if hidden else "gösterme"
    op_message = f"Soru numarası {action_text} sıraya alındı"

    op_id = uuid.uuid4().hex[:12]
    write_number_hide_op(job_dir, op_id, {
        "status": "queued",
        "message": op_message,
        "current": "Hazırlanıyor",
        "done": 0,
        "total": len(selected),
        "progress_percent": 4,
        "redirect_url": "",
        "error": "",
    })
    thread = threading.Thread(target=run_number_hide_operation, args=(job_id, op_id, selected, hidden), daemon=True)
    thread.start()
    return RedirectResponse(url=f"/jobs/{quote(job_id)}/number-hide/{op_id}", status_code=303)


@app.get("/jobs/{job_id}/number-hide/{op_id}", response_class=HTMLResponse)
def job_number_hide_progress(job_id: str, op_id: str) -> HTMLResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Soru numarası gizleme kapalı")
    job_dir, _ = get_active_job(job_id)
    read_number_hide_op(job_dir, op_id)
    return number_hide_progress_page(job_id, op_id)


@app.get("/jobs/{job_id}/number-hide/{op_id}/status")
def job_number_hide_status(job_id: str, op_id: str) -> JSONResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Soru numarası gizleme kapalı")
    job_dir, _ = get_active_job(job_id)
    return JSONResponse(read_number_hide_op(job_dir, op_id))


@app.get("/jobs/{job_id}/preview/image")
def preview_image(job_id: str, path: str) -> FileResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Önizleme kapalı")
    image_path = preview_image_for_pdf(job_id, path)
    return FileResponse(image_path, media_type="image/png")


@app.get("/jobs/{job_id}/preview/source-page")
def preview_source_page(job_id: str, row_id: int) -> FileResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Önizleme kapalı")
    image_path = source_page_image(job_id, row_id)
    return FileResponse(image_path, media_type="image/png")


@app.get("/jobs/{job_id}/preview/crop")
def preview_source_crop(job_id: str, row_id: int, v: str | None = None) -> FileResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Önizleme kapalı")
    image_path = source_crop_image(job_id, row_id)
    return FileResponse(image_path, media_type="image/png")


@app.get("/jobs/{job_id}/preview/compare")
def preview_compare(job_id: str, row_id: int, kind: str) -> FileResponse:
    if not FEEDBACK_MODE:
        raise HTTPException(status_code=404, detail="Karşılaştırma kapalı")
    image_path = compare_image_path(job_id, row_id, kind)
    return FileResponse(image_path, media_type="image/png")


def remove_temp_file(path: str) -> None:
    Path(path).unlink(missing_ok=True)


@app.get("/jobs/{job_id}/download/all")
def download_all(job_id: str) -> FileResponse:
    job_dir, _ = get_active_job(job_id)
    out_dir = job_dir / "output"
    result_pdfs = collect_result_pdfs(out_dir)
    if not result_pdfs:
        raise HTTPException(status_code=404, detail="ZIP için dosya bulunamadı")

    handle = tempfile.NamedTemporaryFile(prefix=f"{job_id}_", suffix=".zip", delete=False)
    handle.close()
    zip_path = Path(handle.name)
    build_zip(result_pdfs, zip_path, out_dir)
    return FileResponse(
        zip_path,
        filename=f"{job_id}_test_pdfs.zip",
        media_type="application/zip",
        background=BackgroundTask(remove_temp_file, str(zip_path)),
    )


@app.get("/jobs/{job_id}/download/file")
def download_file(job_id: str, path: str) -> FileResponse:
    _job_dir, file_path = safe_output_pdf(job_id, path)
    return FileResponse(file_path, filename=file_path.name, media_type="application/pdf")


@app.get("/jobs/{job_id}/download/log")
def download_log(job_id: str) -> FileResponse:
    job_dir, meta = get_active_job(job_id)
    log_path = job_log_path(job_dir)
    if not log_path.exists():
        log_path.write_text(str(meta.get("error") or "Log bulunamadı"), encoding="utf-8")
    filename = f"{job_id}_hata_logu.txt"
    return FileResponse(log_path, filename=filename, media_type="text/plain; charset=utf-8")


@app.get("/health")
def health() -> dict[str, object]:
    dependencies = health_dependencies()
    disk = health_disk()
    all_deps_ok = all(dependencies.values())
    return {
        "status": "ok" if all_deps_ok else "degraded",
        "version": DEPLOY_INFO["version"],
        "deployed_at": DEPLOY_INFO["deployed_at"],
        "deployed_at_label": DEPLOY_INFO["deployed_at_label"],
        "dependencies": dependencies,
        "dependencies_ok": all_deps_ok,
        "disk": disk,
    }


@app.get("/favicon.ico")
def favicon() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=307)


import easy_editor
easy_editor.register_easy_editor(app)
