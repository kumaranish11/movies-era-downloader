"""
app.py — single-file Flask download proxy for "Movies Era Downloader"

Flow:
    1. GET /download?token=<url b64'd THREE times>&file=<name b64'd once, no ext>[&logo=<icon-filename>]
       -> shows a branded download page (filename, type, size, button).
       The real origin URL is never shown anywhere on this page.
       Optional `logo` param: an image filename (WITH extension, e.g.
       logo=mybrand.png) that must sit next to this app.py. If found, it's
       used instead of the default file-type icon (highest priority).
       Otherwise, the file's category is auto-detected and, if a matching
       icon exists next to app.py (video.png for videos, audio.png for
       audio, archive.png, image.png, document.png, other.png), that is
       used automatically. Falls back to a plain emoji icon if nothing
       matches.

    3. GET /health -> lightweight JSON health check for uptime monitors /
       Render's health checks. Does not touch any upstream server.

    2. The button on that page points at:
       GET /download/file?token=...&file=...
       -> streams the actual bytes as a fast, range-enabled attachment
          named "<decoded file><ext>". The extension is resolved from (in
          order): the origin URL's path, the origin's Content-Disposition
          header, then a specific/known origin Content-Type — never a
          generic ".bin" guess.

    3. Every failure mode (bad link, source down, timeout, 404 upstream,
       access denied, etc.) renders its own branded, human-readable error
       page instead of a raw stack trace or bare JSON.

Deploy on Render:
    - Build command: pip install -r requirements.txt
    - Start command: gunicorn app:app --workers 2 --threads 8 --timeout 120
    - Drop logo/icon images in the same folder as app.py, then pass
      ?logo=that-filename.png on a /download link.

Generating a link (Python):
    import base64
    def make_token(url: str) -> str:
        b = url.encode()
        for _ in range(3):                      # triple-encoded
            b = base64.urlsafe_b64encode(b)
        return b.decode()
    def make_file(name_without_ext: str) -> str:
        return base64.urlsafe_b64encode(name_without_ext.encode()).decode()

    # https://your-site.onrender.com/download?token=<make_token(url)>&file=<make_file(name)>&logo=mybrand.png
"""

import base64
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import requests
from flask import Flask, Response, redirect, request, send_from_directory, stream_with_context
from markupsafe import escape

app = Flask(__name__)

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

BRAND_NAME = "Movies Era"
BRAND_TAGLINE = "Downloader"
SITE_FULL = f"{BRAND_NAME} {BRAND_TAGLINE}"

ALLOWED_SCHEMES = {"http", "https"}
TOKEN_LAYERS = 3  # how many times `token` is base64-encoded
CHUNK_SIZE = 256 * 1024  # 256 KB — good balance of throughput vs memory
UPSTREAM_TIMEOUT = (10, 30)  # (connect timeout, per-chunk read timeout)
USER_AGENT = "Mozilla/5.0 (compatible; MoviesEraDownloader/1.0)"
LOGO_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}

SAFE_PASSTHROUGH_HEADERS = {"content-length", "content-range", "accept-ranges", "last-modified", "etag"}

# Explicit extension <-> Content-Type map — the single source of truth for
# extension guessing. We deliberately do NOT use Python's stdlib
# `mimetypes` module: on many Linux hosts it reads /etc/mime.types and
# maps generic types like application/octet-stream to ".bin".
EXTENSION_MIME = {
    ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".mp4": "video/mp4",
    ".m4v": "video/x-m4v", ".avi": "video/x-msvideo", ".webm": "video/webm",
    ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv", ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg", ".3gp": "video/3gpp", ".ts": "video/mp2t",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
    ".zip": "application/zip", ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed", ".tar": "application/x-tar",
    ".gz": "application/gzip", ".iso": "application/x-iso9660-image",
    ".pdf": "application/pdf", ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json",
    ".apk": "application/vnd.android.package-archive", ".exe": "application/x-msdownload",
}
CONTENT_TYPE_TO_EXT = {}
for _ext, _ct in EXTENSION_MIME.items():
    CONTENT_TYPE_TO_EXT.setdefault(_ct, _ext)

VIDEO_EXT = {".mkv", ".mov", ".mp4", ".m4v", ".avi", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".ts"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
DOC_EXT = {".pdf", ".doc", ".docx", ".txt", ".csv"}
PLAYABLE_CATEGORIES = {"video", "audio"}
ICONS = {"video": "🎬", "audio": "🎵", "archive": "🗜️", "image": "🖼️", "document": "📄", "other": "📦"}
CATEGORY_STYLE = {
    "video": ("#ede9fe", "#6d28d9"),
    "audio": ("#fce7f3", "#be185d"),
    "archive": ("#fef3c7", "#b45309"),
    "image": ("#cffafe", "#0e7490"),
    "document": ("#dbeafe", "#1d4ed8"),
    "other": ("#e5e7eb", "#374151"),
}

FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
    "%3Ctext y='.9em' font-size='90'%3E%F0%9F%8E%AC%3C/text%3E%3C/svg%3E"
)


class DownloadError(Exception):
    """Raised for any user-facing failure. Carries everything needed to
    render a friendly, branded error page instead of a stack trace."""

    def __init__(self, title, message, status=400):
        self.title = title
        self.message = message
        self.status = status
        super().__init__(message)


# ---------------------------------------------------------------- helpers

def _category_for(ext: str) -> str:
    ext = ext.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in ARCHIVE_EXT:
        return "archive"
    if ext in IMAGE_EXT:
        return "image"
    if ext in DOC_EXT:
        return "document"
    return "other"


def _b64_decode_once(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8")
    except Exception:
        return base64.b64decode(padded).decode("utf-8")


def _b64_decode_n(value: str, times: int) -> str:
    result = value
    for _ in range(times):
        result = _b64_decode_once(result)
    return result


def _extension_from_url(url: str) -> str:
    path = urlparse(url).path
    _, ext = os.path.splitext(path)
    return ext.lower()


def _extension_from_content_disposition(cd_header):
    if not cd_header:
        return None
    match = re.search(r"filename\*=(?:UTF-8'')?\"?([^\";]+)\"?", cd_header, re.IGNORECASE)
    if not match:
        match = re.search(r'filename="?([^";]+)"?', cd_header, re.IGNORECASE)
    if not match:
        return None
    filename = unquote(match.group(1))
    _, ext = os.path.splitext(filename)
    return ext.lower() or None


def _extension_from_content_type(content_type):
    if not content_type:
        return None
    base_ct = content_type.split(";")[0].strip().lower()
    return CONTENT_TYPE_TO_EXT.get(base_ct)


def _resolve_extension_from_headers(ext_from_url, content_type, content_disposition):
    """Never falls back to a generic '.bin' guess."""
    if ext_from_url:
        return ext_from_url
    cd_ext = _extension_from_content_disposition(content_disposition)
    if cd_ext:
        return cd_ext
    ct_ext = _extension_from_content_type(content_type)
    if ct_ext:
        return ct_ext
    return ""


def _safe_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace('"', "").strip()
    return name or "download"


def _human_size(num_bytes):
    if num_bytes is None:
        return None
    num = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def _resolve_logo(logo_param):
    if not logo_param:
        return None
    name = os.path.basename(logo_param)
    if not re.match(r"^[A-Za-z0-9_\-.]+$", name):
        return None
    _, ext = os.path.splitext(name)
    if ext.lower() not in LOGO_ALLOWED_EXT:
        return None
    if not os.path.isfile(os.path.join(ROOT_DIR, name)):
        return None
    return name


def _auto_category_icon(category: str):
    """Looks for <category>.png (or .jpg/.svg/etc.) next to app.py, e.g.
    video.png for video files, audio.png for audio files. Returns the
    filename if found, else None."""
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico"):
        candidate = f"{category}{ext}"
        if os.path.isfile(os.path.join(ROOT_DIR, candidate)):
            return candidate
    return None


def _decode_params():
    """Shared decode + validation for both routes."""
    token = request.args.get("token")
    file_param = request.args.get("file")
    if not token or not file_param:
        raise DownloadError(
            "Invalid Download Link",
            "This link is missing required information. Please request a new download link.",
            400,
        )

    try:
        download_url = _b64_decode_n(token, times=TOKEN_LAYERS)
    except Exception:
        raise DownloadError(
            "Corrupted Link",
            "This download link appears to be corrupted or malformed.",
            400,
        )

    try:
        raw_name = _b64_decode_once(file_param)
    except Exception:
        raise DownloadError(
            "Corrupted Link",
            "This download link appears to be corrupted or malformed.",
            400,
        )

    parsed = urlparse(download_url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
        raise DownloadError(
            "Invalid Source",
            "This download link points to an unsupported source.",
            400,
        )

    filename = _safe_filename(raw_name)
    ext = _extension_from_url(download_url)
    return download_url, filename, ext, parsed.scheme


def _classify_connection_exception(exc):
    """Turn a requests exception into a specific, user-facing DownloadError.
    Never includes the exception text (which could contain the origin URL)."""
    if isinstance(exc, requests.exceptions.Timeout):
        return DownloadError(
            "Request Timed Out",
            "The source server took too long to respond. Please try again in a moment.",
            504,
        )
    if isinstance(exc, requests.exceptions.SSLError):
        return DownloadError(
            "Secure Connection Failed",
            "Could not establish a secure connection to the source server.",
            502,
        )
    if isinstance(exc, requests.exceptions.ConnectionError):
        return DownloadError(
            "Server Unreachable",
            "We couldn't connect to the source server. It may be temporarily down.",
            502,
        )
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return DownloadError(
            "Too Many Redirects",
            "This link redirects in a loop and can't be resolved.",
            502,
        )
    return DownloadError(
        "Download Failed",
        "Something went wrong while fetching this file. Please try again.",
        502,
    )


def _classify_status(status_code):
    """Maps an origin HTTP status to a specific DownloadError, or None if
    the status is fine (< 400)."""
    if status_code < 400:
        return None
    if status_code == 404:
        return DownloadError("File Not Found", "The requested file no longer exists on the source server.", 404)
    if status_code == 403:
        return DownloadError("Access Denied", "The source server denied access to this file.", 403)
    if status_code == 410:
        return DownloadError("Link Expired", "This download link is no longer valid.", 410)
    if status_code == 429:
        return DownloadError("Source Server Busy", "The source server is rate-limiting requests. Please try again shortly.", 429)
    if status_code >= 500:
        return DownloadError("Source Server Error", "The source server encountered an error. Please try again later.", 502)
    return DownloadError("Download Failed", "This file is currently unavailable.", status_code)


def _get_probe(url, headers):
    """A GET probe that reads only headers, never the body. Raises a
    classified DownloadError on any definitive failure."""
    try:
        r = requests.get(url, headers=headers, timeout=UPSTREAM_TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise _classify_connection_exception(exc)
    try:
        err = _classify_status(r.status_code)
        if err:
            raise err
        return r.headers.get("Content-Length"), r.headers.get("Content-Type"), r.headers.get("Content-Disposition")
    finally:
        r.close()


def _probe_upstream(url: str):
    """Validates the source is genuinely reachable and servable, and
    returns its metadata. Raises DownloadError on definitive failure —
    this is what catches a dead link on the download PAGE itself, before
    the visitor ever clicks Download, instead of failing later."""
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.head(url, headers=headers, timeout=UPSTREAM_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        # HEAD failed at the connection level — some hosts just don't
        # support it well. Give GET a fair chance before reporting an error.
        return _get_probe(url, headers)

    if r.status_code in (405, 501):
        return _get_probe(url, headers)

    err = _classify_status(r.status_code)
    if err:
        raise err
    return r.headers.get("Content-Length"), r.headers.get("Content-Type"), r.headers.get("Content-Disposition")


def _open_upstream(url, fwd_headers):
    """GET the origin with classified, user-facing errors on failure."""
    try:
        upstream = requests.get(url, headers=fwd_headers, stream=True, timeout=UPSTREAM_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        raise _classify_connection_exception(exc)

    err = _classify_status(upstream.status_code)
    if err:
        upstream.close()
        raise err

    return upstream


# ----------------------------------------------------------------- theme

SITE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 50%, #c026d3 100%);
  padding: 24px;
}
.wrap { width: 100%; max-width: 440px; animation: fadeUp .45s ease both; }
@keyframes fadeUp { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
.brand { display: flex; align-items: center; justify-content: center; gap: 8px; margin-bottom: 18px; }
.brand-emoji { font-size: 24px; }
.brand-logo { width: 26px; height: 26px; border-radius: 6px; object-fit: cover; }
.brand-name { color: #fff; font-weight: 800; font-size: 18px; letter-spacing: .2px; }
.brand-tag { color: rgba(255,255,255,0.75); font-size: 13px; font-weight: 500; }
.card {
  background: #ffffff; border-radius: 20px; padding: 36px 30px; box-shadow: 0 20px 60px rgba(0,0,0,0.28);
  text-align: center;
}
.icon, .icon-img {
  width: 88px; height: 88px; border-radius: 20px; margin: 0 auto 18px; display: flex;
  align-items: center; justify-content: center; background: #f3f4f6; font-size: 44px;
}
.icon-img { object-fit: cover; }
.icon-error { background: #fee2e2; color: #dc2626; }
.filename { font-size: 17px; font-weight: 700; color: #111827; word-break: break-word; margin: 0 0 6px; }
.err-title { font-size: 19px; font-weight: 800; color: #111827; margin: 0 0 8px; }
.meta { color: #6b7280; font-size: 14px; margin: 0 0 20px; line-height: 1.5; }
.badges { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-bottom: 24px; }
.badge {
  background: #f3f4f6; color: #374151; font-size: 12px; font-weight: 600; padding: 6px 12px;
  border-radius: 999px; white-space: nowrap;
}
.badge-type { font-weight: 700; }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px; width: 100%;
  background: linear-gradient(135deg, #4f46e5, #7c3aed); color: #fff; text-decoration: none;
  font-weight: 700; font-size: 16px; padding: 15px 24px; border-radius: 12px; border: none; cursor: pointer;
  box-shadow: 0 8px 20px rgba(79,70,229,0.35); transition: transform .15s ease, box-shadow .15s ease;
}
.btn:hover { transform: translateY(-2px); box-shadow: 0 12px 26px rgba(79,70,229,0.45); }
.actions { display: flex; gap: 10px; }
.actions .btn { width: auto; flex: 1; }
.btn-secondary { background: #f3f4f6; color: #111827; box-shadow: none; }
.btn-secondary:hover { transform: translateY(-2px); box-shadow: 0 8px 18px rgba(0,0,0,0.12); }
.btn-copy {
  display: inline-flex; align-items: center; gap: 6px; background: none; border: none; cursor: pointer;
  color: #6b7280; font-size: 12px; font-weight: 600; margin-top: 12px; padding: 4px 8px;
}
.btn-copy:hover { color: #4f46e5; }
.hint { margin-top: 14px; font-size: 12px; color: #9ca3af; }
.footer { text-align: center; margin-top: 18px; font-size: 12px; color: rgba(255,255,255,0.75); }
.toast {
  position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%) translateY(20px);
  background: #111827; color: #fff; font-size: 13px; font-weight: 600; padding: 10px 18px;
  border-radius: 999px; opacity: 0; pointer-events: none; transition: opacity .2s ease, transform .2s ease;
  box-shadow: 0 10px 24px rgba(0,0,0,0.3);
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.wrap-wide { width: 100%; max-width: 760px; animation: fadeUp .45s ease both; }
.video-frame {
  position: relative; width: 100%; aspect-ratio: 16/9; background: #000; border-radius: 14px;
  overflow: hidden; margin-bottom: 22px;
}
.video-frame video { width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
.audio-frame { margin: 4px 0 22px; }
.audio-frame audio { width: 100%; }
.cover { width: 140px; height: 140px; border-radius: 18px; margin: 0 auto 18px; object-fit: cover; background: #f3f4f6; }
.cover-emoji {
  width: 140px; height: 140px; border-radius: 18px; margin: 0 auto 18px; background: #f3f4f6;
  display: flex; align-items: center; justify-content: center; font-size: 64px;
}
.stack { display: flex; flex-direction: column; gap: 10px; }
"""


def _brand_header_html(logo_name):
    if logo_name:
        return f'<img class="brand-logo" src="/assets/{escape(logo_name)}" alt="{escape(BRAND_NAME)}"><span class="brand-name">{escape(BRAND_NAME)}</span><span class="brand-tag">{escape(BRAND_TAGLINE)}</span>'
    return f'<span class="brand-emoji">🎬</span><span class="brand-name">{escape(BRAND_NAME)}</span><span class="brand-tag">{escape(BRAND_TAGLINE)}</span>'


def render_download_page(filename, ext, type_badge_html, meta_badges, icon_html, download_href, logo_name, page_url, og_image_url, watch_href=None):
    filename_e = escape(filename)
    ext_e = escape(ext)
    badges_html = type_badge_html + "".join(f'<span class="badge">{escape(b)}</span>' for b in meta_badges)
    og_image_tag = f'<meta property="og:image" content="{escape(og_image_url)}">' if og_image_url else ""
    watch_btn_html = f'<a class="btn btn-secondary" href="{watch_href}">▶ Watch Online</a>' if watch_href else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{filename_e}{ext_e} — {escape(SITE_FULL)}</title>
<meta property="og:title" content="{filename_e}{ext_e}">
<meta property="og:description" content="Download via {escape(SITE_FULL)}">
<meta property="og:type" content="website">
{og_image_tag}
<link rel="icon" href="{FAVICON}">
<style>{SITE_CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="brand">{_brand_header_html(logo_name)}</div>
    <div class="card">
      {icon_html}
      <p class="filename">{filename_e}{ext_e}</p>
      <div class="badges">{badges_html}</div>
      <div class="stack">
        <a class="btn" href="{download_href}">⬇ Download</a>
        {watch_btn_html}
      </div>
      <button class="btn-copy" onclick="copyLink(this)" data-url="{escape(page_url)}">🔗 Copy link</button>
      <p class="hint">Direct download — no redirects, no shorteners.</p>
    </div>
    <p class="footer">© {escape(SITE_FULL)}</p>
  </div>
  <div class="toast" id="toast">Link copied</div>
  <script>
    function copyLink(btn) {{
      var url = btn.getAttribute('data-url');
      var toast = document.getElementById('toast');
      var done = function() {{ toast.classList.add('show'); setTimeout(function() {{ toast.classList.remove('show'); }}, 1800); }};
      if (navigator.clipboard) {{ navigator.clipboard.writeText(url).then(done, done); }} else {{ done(); }}
    }}
  </script>
</body>
</html>"""


def render_watch_page(filename, ext, category, type_badge_html, meta_badges, media_html, download_href, logo_name, page_url, og_image_url):
    filename_e = escape(filename)
    ext_e = escape(ext)
    badges_html = type_badge_html + "".join(f'<span class="badge">{escape(b)}</span>' for b in meta_badges)
    og_image_tag = f'<meta property="og:image" content="{escape(og_image_url)}">' if og_image_url else ""
    wrap_class = "wrap-wide" if category == "video" else "wrap"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{filename_e}{ext_e} — {escape(SITE_FULL)}</title>
<meta property="og:title" content="{filename_e}{ext_e}">
<meta property="og:description" content="Watch online via {escape(SITE_FULL)}">
<meta property="og:type" content="video.other">
{og_image_tag}
<link rel="icon" href="{FAVICON}">
<style>{SITE_CSS}</style>
</head>
<body>
  <div class="{wrap_class}">
    <div class="brand">{_brand_header_html(logo_name)}</div>
    <div class="card">
      {media_html}
      <p class="filename">{filename_e}{ext_e}</p>
      <div class="badges">{badges_html}</div>
      <div class="stack">
        <a class="btn btn-secondary" href="{download_href}">⬇ Download instead</a>
      </div>
      <p class="hint">Streamed directly — nothing is saved unless you download it.</p>
    </div>
    <p class="footer">© {escape(SITE_FULL)}</p>
  </div>
</body>
</html>"""


def render_error_page(title, message, status, logo_name=None):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — {escape(SITE_FULL)}</title>
<link rel="icon" href="{FAVICON}">
<style>{SITE_CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="brand">{_brand_header_html(logo_name)}</div>
    <div class="card">
      <div class="icon icon-error">⚠️</div>
      <p class="err-title">{escape(title)}</p>
      <p class="meta">{escape(message)}</p>
      <div class="actions">
        <a class="btn" href="javascript:location.reload()">↻ Try Again</a>
        <a class="btn btn-secondary" href="/">🏠 Home</a>
      </div>
      <p class="hint">Error code {status}</p>
    </div>
    <p class="footer">© {escape(SITE_FULL)}</p>
  </div>
</body>
</html>"""


# ----------------------------------------------------------------- routes

@app.route("/download", methods=["GET", "HEAD"])
def download_page():
    download_url, filename, ext, scheme = _decode_params()
    size_bytes, content_type, content_disposition = _probe_upstream(download_url)
    ext = _resolve_extension_from_headers(ext, content_type, content_disposition)

    logo_name = _resolve_logo(request.args.get("logo"))
    category = _category_for(ext)
    icon_source = logo_name or _auto_category_icon(category)
    if icon_source:
        icon_html = f'<img class="icon-img" src="/assets/{escape(icon_source)}" alt="{escape(category)}">'
    else:
        icon_html = f'<div class="icon">{ICONS[category]}</div>'

    bg, fg = CATEGORY_STYLE[category]
    type_label = f"{ICONS[category]} {ext.lstrip('.').upper()}" if ext else f"{ICONS[category]} {category.upper()}"
    type_badge_html = f'<span class="badge badge-type" style="background:{bg};color:{fg}">{escape(type_label)}</span>'

    badges = []
    size_h = _human_size(int(size_bytes)) if size_bytes and size_bytes.isdigit() else None
    if size_h:
        badges.append(size_h)
    if scheme == "https":
        badges.append("🔒 Secure")
    badges.append("⚡ Direct")

    qs = request.query_string.decode()
    download_href = f"/download/file?{qs}"
    watch_href = f"/watch?{qs}" if category in PLAYABLE_CATEGORIES else None
    og_image_url = request.url_root.rstrip("/") + f"/assets/{icon_source}" if icon_source else None

    html = render_download_page(
        filename=filename,
        ext=ext,
        type_badge_html=type_badge_html,
        meta_badges=badges,
        icon_html=icon_html,
        download_href=download_href,
        logo_name=logo_name,
        page_url=request.url,
        og_image_url=og_image_url,
        watch_href=watch_href,
    )
    return Response(html, mimetype="text/html")


@app.route("/watch", methods=["GET", "HEAD"])
def watch_page():
    download_url, filename, ext, scheme = _decode_params()
    size_bytes, content_type, content_disposition = _probe_upstream(download_url)
    ext = _resolve_extension_from_headers(ext, content_type, content_disposition)
    category = _category_for(ext)

    if category not in PLAYABLE_CATEGORIES:
        # Nothing to preview for this file type — send them straight to
        # the normal download page instead of a dead end.
        return redirect(f"/download?{request.query_string.decode()}")

    logo_name = _resolve_logo(request.args.get("logo"))
    icon_source = logo_name or _auto_category_icon(category)
    stream_src = f"/watch/stream?{request.query_string.decode()}"
    resolved_ct = EXTENSION_MIME.get(ext, content_type or ("video/mp4" if category == "video" else "audio/mpeg"))

    if category == "video":
        media_html = (
            f'<div class="video-frame"><video controls preload="metadata" playsinline>'
            f'<source src="{escape(stream_src)}" type="{escape(resolved_ct)}">'
            f"Your browser can't play this format in-page — try Download instead."
            f"</video></div>"
        )
    else:
        if icon_source:
            cover_html = f'<img class="cover" src="/assets/{escape(icon_source)}" alt="cover">'
        else:
            cover_html = f'<div class="cover-emoji">{ICONS[category]}</div>'
        media_html = (
            f'{cover_html}<div class="audio-frame"><audio controls preload="metadata">'
            f'<source src="{escape(stream_src)}" type="{escape(resolved_ct)}">'
            f"Your browser can't play this format — try Download instead."
            f"</audio></div>"
        )

    bg, fg = CATEGORY_STYLE[category]
    type_label = f"{ICONS[category]} {ext.lstrip('.').upper()}" if ext else f"{ICONS[category]} {category.upper()}"
    type_badge_html = f'<span class="badge badge-type" style="background:{bg};color:{fg}">{escape(type_label)}</span>'

    badges = []
    size_h = _human_size(int(size_bytes)) if size_bytes and size_bytes.isdigit() else None
    if size_h:
        badges.append(size_h)
    if scheme == "https":
        badges.append("🔒 Secure")
    badges.append("📡 Streaming")

    og_image_url = request.url_root.rstrip("/") + f"/assets/{icon_source}" if icon_source else None

    html = render_watch_page(
        filename=filename,
        ext=ext,
        category=category,
        type_badge_html=type_badge_html,
        meta_badges=badges,
        media_html=media_html,
        download_href=f"/download?{request.query_string.decode()}",
        logo_name=logo_name,
        page_url=request.url,
        og_image_url=og_image_url,
    )
    return Response(html, mimetype="text/html")


@app.route("/assets/<path:filename>")
def serve_asset(filename):
    name = os.path.basename(filename)
    _, ext = os.path.splitext(name)
    if ext.lower() not in LOGO_ALLOWED_EXT or not os.path.isfile(os.path.join(ROOT_DIR, name)):
        raise DownloadError("Not Found", "That asset doesn't exist.", 404)
    return send_from_directory(ROOT_DIR, name)


def _build_stream_response(download_url, filename, ext, request, disposition):
    """Shared range-enabled streaming logic used by both the forced
    download route and the in-browser watch/stream route. `disposition`
    is 'attachment' (triggers Save As) or 'inline' (plays/renders in the
    browser instead of downloading)."""
    fwd_headers = {"User-Agent": USER_AGENT}
    range_header = request.headers.get("Range")
    if range_header:
        fwd_headers["Range"] = range_header

    if request.method == "HEAD":
        size_bytes, content_type, content_disposition = _probe_upstream(download_url)
        ext = _resolve_extension_from_headers(ext, content_type, content_disposition)
        resolved_ct = EXTENSION_MIME.get(ext, content_type or "application/octet-stream")
        headers = {
            "Content-Disposition": f'{disposition}; filename="{filename}{ext}"',
            "Accept-Ranges": "bytes",
        }
        if size_bytes:
            headers["Content-Length"] = size_bytes
        return Response(b"", headers=headers, content_type=resolved_ct)

    upstream = _open_upstream(download_url, fwd_headers)

    ext = _resolve_extension_from_headers(ext, upstream.headers.get("Content-Type"), upstream.headers.get("Content-Disposition"))
    full_filename = f"{filename}{ext}"

    out_headers = {k.title(): v for k, v in upstream.headers.items() if k.lower() in SAFE_PASSTHROUGH_HEADERS}
    out_headers["Content-Disposition"] = f'{disposition}; filename="{full_filename}"'
    out_headers.setdefault("Accept-Ranges", "bytes")
    out_headers["Content-Type"] = EXTENSION_MIME.get(ext, upstream.headers.get("Content-Type", "application/octet-stream"))

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    status = 206 if (range_header and upstream.status_code == 206) else 200
    resp = Response(stream_with_context(generate()), status=status, headers=out_headers)
    resp.direct_passthrough = True
    return resp


@app.route("/download/file", methods=["GET", "HEAD"])
def download_file():
    download_url, filename, ext, _scheme = _decode_params()
    return _build_stream_response(download_url, filename, ext, request, disposition="attachment")


@app.route("/watch/stream", methods=["GET", "HEAD"])
def watch_stream():
    download_url, filename, ext, _scheme = _decode_params()
    return _build_stream_response(download_url, filename, ext, request, disposition="inline")


@app.route("/")
def index():
    return (
        f"{SITE_FULL} proxy is running.\n"
        "Usage: /download?token=<url base64'd 3x>&file=<filename base64'd once, no extension>[&logo=<icon-filename>]\n"
        "Watch in-browser (video/audio only): /watch?token=...&file=...\n"
        "Health check: /health\n"
    )


@app.route("/health")
def health():
    return {
        "status": "ok",
        "service": SITE_FULL,
        "time": datetime.now(timezone.utc).isoformat(),
    }, 200


@app.errorhandler(DownloadError)
def handle_download_error(e):
    logo_name = _resolve_logo(request.args.get("logo"))
    return Response(render_error_page(e.title, e.message, e.status, logo_name), mimetype="text/html", status=e.status)


@app.errorhandler(404)
def handle_404(e):
    return Response(
        render_error_page("Page Not Found", "The page you're looking for doesn't exist.", 404),
        mimetype="text/html",
        status=404,
    )


@app.errorhandler(500)
def handle_500(e):
    return Response(
        render_error_page("Something Went Wrong", "An unexpected error occurred. Please try again.", 500),
        mimetype="text/html",
        status=500,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
