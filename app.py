"""WWEC Website Backend:
- Serves the existing WWEC static website cleanly without altering its design.
- Connects Customer Contact Form to Google Sheets via Google Apps Script Web App.
- Connects Public Gallery, Project Detail Pages & Private Admin Dashboard to Google Sheets + Google Drive.
- Secures Admin Dashboard with Username + PBKDF2 Password Hashing, Sessions, CSRF & Rate Limiting.
"""
import base64
import csv
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from urllib.parse import parse_qs, urlparse

import requests
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- App Configuration ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    MAX_CONTENT_LENGTH=80 * 1024 * 1024,  # 80 MB max total upload request (supports photos + video)
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("wwec")

SERVICES = {
    "building-solutions": "Building Solutions",
    "manufacturing": "Manufacturing",
    "professional-services": "Professional Services",
    "development-support": "Development & Support",
    "engineering-solutions": "Engineering Solutions",
}

GALLERY_CATEGORIES = {
    "architectural": "Architectural Design",
    "ongoing": "Ongoing Projects",
    "completed": "Completed Projects",
    "special": "Special Development",
}

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_SINGLE_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB per image

ALLOWED_VIDEO_EXTENSIONS = {"mp4", "webm", "mov"}
ALLOWED_VIDEO_MIME_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
MAX_SINGLE_VIDEO_BYTES = 45 * 1024 * 1024  # 45 MB per video

# ---------- Simple In-Memory Rate Limiter & Duplicate Guard ----------
_hits = {}
_recent_submissions = {}
_lock = threading.Lock()


def too_many(key: str, limit: int, window: int) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _hits.get(key, []) if now - t < window]
        if len(recent) >= limit:
            _hits[key] = recent
            return True
        recent.append(now)
        _hits[key] = recent
        return False


def is_duplicate_submission(fingerprint: str, ttl: int = 300) -> bool:
    now = time.time()
    with _lock:
        expired = [k for k, ts in _recent_submissions.items() if now - ts > ttl]
        for k in expired:
            del _recent_submissions[k]
        if fingerprint in _recent_submissions:
            return True
        _recent_submissions[fingerprint] = now
        return False


def client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


# ---------- Simple In-Memory Cache for Public Gallery ----------
_gallery_cache = {"ts": 0.0, "items": None}
GALLERY_CACHE_TTL = 45


def invalidate_gallery_cache():
    _gallery_cache["ts"] = 0.0
    _gallery_cache["items"] = None


# ---------- Google Apps Script Bridge Helpers ----------
def gas_configured() -> bool:
    return bool(os.environ.get("GAS_WEBAPP_URL") and os.environ.get("GAS_API_SECRET"))


def gas_get(action: str, extra_params: dict | None = None) -> dict:
    url = os.environ.get("GAS_WEBAPP_URL", "").strip()
    secret = os.environ.get("GAS_API_SECRET", "").strip()
    if not url or not secret:
        return {"ok": False, "error": "Google Sheets/Drive integration is not configured yet."}
    params = {"action": action, "secret": secret}
    if extra_params:
        params.update(extra_params)
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("GAS GET (%s) failed: %s", action, exc)
        return {"ok": False, "error": "Unable to connect to Google Sheets/Drive right now."}


def gas_post(action: str, payload: dict) -> dict:
    url = os.environ.get("GAS_WEBAPP_URL", "").strip()
    secret = os.environ.get("GAS_API_SECRET", "").strip()
    if not url or not secret:
        return {"ok": False, "error": "Google Sheets/Drive integration is not configured yet."}
    body = dict(payload)
    body["action"] = action
    body["secret"] = secret
    try:
        resp = requests.post(url, json=body, timeout=90)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("GAS POST (%s) failed: %s", action, exc)
        return {"ok": False, "error": "Google Sheets/Drive request failed. Please try again."}


# ---------- Optional Email Notification ----------
def send_mail(row: dict):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not host or not to:
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = f"New WWEC Enquiry: {row['subject']}"[:120]
        msg["From"] = os.environ.get("SMTP_USER") or to
        msg["To"] = to
        msg["Reply-To"] = row["email"]
        msg.set_content(
            f"Customer Name: {row['name']}\n"
            f"Email Address: {row['email']}\n"
            f"Phone Number: {row['phone']}\n"
            f"Subject / Service: {row['subject']}\n"
            f"Submitted At: {row['submitted_at']}\n\n"
            f"Message:\n{row['message']}\n"
        )
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=15) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            s.send_message(msg)
    except Exception as exc:
        logger.warning("Email notification failed: %s", exc)


# ---------- Media Validation Helpers ----------
def validate_image_file(file_storage) -> tuple[bool, str, bytes, str]:
    filename = secure_filename(file_storage.filename or "")
    if "." not in filename:
        return False, "Invalid image file name.", b"", ""
    ext = filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return False, f"Unsupported image type '.{ext}'. Only JPG, JPEG, PNG, and WEBP are allowed.", b"", ""

    mimetype = (file_storage.mimetype or "").lower()
    if mimetype not in ALLOWED_IMAGE_MIME_TYPES:
        return False, "Invalid image MIME type.", b"", ""

    data = file_storage.read()
    file_storage.seek(0)

    if not data:
        return False, f"Image '{filename}' is empty.", b"", ""
    if len(data) > MAX_SINGLE_IMAGE_BYTES:
        return False, f"Image '{filename}' exceeds the 8 MB limit.", b"", ""

    is_jpeg = data[:3] == b"\xff\xd8\xff"
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if not (is_jpeg or is_png or is_webp):
        return False, f"File '{filename}' is not a valid JPG, PNG, or WEBP image.", b"", ""

    return True, "", data, filename


def validate_video_file(file_storage) -> tuple[bool, str, bytes, str]:
    filename = secure_filename(file_storage.filename or "")
    if "." not in filename:
        return False, "Invalid video file name.", b"", ""
    ext = filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return False, f"Unsupported video type '.{ext}'. Only MP4, WEBM, and MOV are allowed.", b"", ""

    mimetype = (file_storage.mimetype or "").lower()
    if mimetype not in ALLOWED_VIDEO_MIME_TYPES:
        return False, "Invalid video MIME type.", b"", ""

    data = file_storage.read()
    file_storage.seek(0)

    if not data:
        return False, f"Video '{filename}' is empty.", b"", ""
    if len(data) > MAX_SINGLE_VIDEO_BYTES:
        return False, f"Video '{filename}' exceeds the 45 MB limit.", b"", ""

    return True, "", data, filename


def build_video_embed_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if "youtu.be" in host:
            vid = parsed.path.lstrip("/").split("/")[0]
            if vid:
                return f"https://www.youtube.com/embed/{vid}"
        if "youtube.com" in host:
            qs = parse_qs(parsed.query)
            if "v" in qs and qs["v"][0]:
                return f"https://www.youtube.com/embed/{qs['v'][0]}"
            if "/embed/" in parsed.path:
                return url
        if "drive.google.com" in host and "/file/d/" in parsed.path:
            parts = parsed.path.split("/file/d/")[1].split("/")
            if parts[0]:
                return f"https://drive.google.com/file/d/{parts[0]}/preview"
    except Exception:
        pass
    return ""


# ---------- Public API: Contact Form ----------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+()\-\s]{7,20}$")


@app.post("/api/contact")
@app.post("/contact")
def api_contact():
    data = request.get_json(silent=True) or {}

    if data.get("website"):
        return jsonify(ok=True)

    if too_many("contact:" + client_ip(), 6, 3600):
        return jsonify(ok=False, error="Too many enquiries submitted. Please try again later."), 429

    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip()
    phone = str(data.get("phone", "")).strip()
    service_key = str(data.get("service", "")).strip()
    subject_input = str(data.get("subject", "")).strip()
    message = str(data.get("message", "")).strip()

    errors = {}
    if not 2 <= len(name) <= 100:
        errors["name"] = "Please enter your full name (2–100 characters)."
    if len(email) > 150 or not EMAIL_RE.match(email):
        errors["email"] = "Please enter a valid email address."
    if not PHONE_RE.match(phone):
        errors["phone"] = "Please enter a valid phone number."
    if not 5 <= len(message) <= 3000:
        errors["message"] = "Please enter a message between 5 and 3000 characters."

    if errors:
        return jsonify(ok=False, errors=errors), 400

    service_label = SERVICES.get(service_key, service_key)
    if subject_input and service_label:
        subject = f"{subject_input} ({service_label})"
    else:
        subject = subject_input or service_label or "General Website Enquiry"

    fp = hashlib.sha256(f"{email.lower()}|{subject}|{message}".encode("utf-8")).hexdigest()
    if is_duplicate_submission(fp, ttl=300):
        return jsonify(ok=True, message="Thank you! Your enquiry has already been received.")

    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    payload = {
        "name": name,
        "email": email,
        "phone": phone,
        "subject": subject,
        "message": message,
        "submitted_at": submitted_at,
    }

    result = gas_post("add_enquiry", payload)
    if not result.get("ok"):
        return jsonify(
            ok=False,
            error="We couldn't submit your enquiry right now. Please try again shortly or call us directly.",
        ), 502

    threading.Thread(target=send_mail, args=(payload,), daemon=True).start()
    return jsonify(ok=True, message="Thank you! Your enquiry has been received. Our team will contact you shortly.")


# ---------- Public API: Dynamic Gallery ----------
def _fetch_all_gallery_items() -> list[dict]:
    now = time.time()
    if _gallery_cache["items"] is not None and (now - _gallery_cache["ts"] < GALLERY_CACHE_TTL):
        return _gallery_cache["items"]

    if not gas_configured():
        return []

    res = gas_get("get_gallery")
    if not res.get("ok"):
        if _gallery_cache["items"] is not None:
            return _gallery_cache["items"]
        return []

    items = res.get("items", [])
    _gallery_cache["items"] = items
    _gallery_cache["ts"] = now
    return items


@app.get("/api/gallery")
def api_gallery():
    if not gas_configured():
        return jsonify(ok=True, items=[], configured=False)

    items = _fetch_all_gallery_items()
    return jsonify(ok=True, items=items, configured=True)


@app.get("/project/<project_id>")
def project_detail(project_id):
    items = _fetch_all_gallery_items()
    project = next((p for p in items if str(p.get("id")) == str(project_id)), None)
    if not project:
        abort(404)

    photos = project.get("photos") or project.get("images") or []
    raw_videos = project.get("videos") or []
    videos = []
    for v in raw_videos:
        if isinstance(v, dict):
            url = v.get("url", "")
            videos.append({
                "url": url,
                "caption": v.get("caption", ""),
                "embed_url": v.get("embed_url") or build_video_embed_url(url),
            })

    before_after = project.get("before_after") or []

    return render_template(
        "project_detail.html",
        project=project,
        categories=GALLERY_CATEGORIES,
        photos=photos,
        videos=videos,
        before_after=before_after,
    )


# ---------- Admin Authentication & Security ----------
def verify_admin_credentials(username: str, password: str) -> bool:
    expected_user = os.environ.get("ADMIN_USERNAME", "admin").strip()
    pw_hash = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
    plain_pw = os.environ.get("ADMIN_PASSWORD", "").strip()

    if not hmac.compare_digest(username.strip(), expected_user):
        return False

    if pw_hash:
        try:
            if check_password_hash(pw_hash, password):
                return True
        except Exception:
            if hmac.compare_digest(password, pw_hash):
                return True
    if plain_pw and hmac.compare_digest(password, plain_pw):
        return True
    return False


def admin_is_configured() -> bool:
    return bool(os.environ.get("ADMIN_PASSWORD_HASH") or os.environ.get("ADMIN_PASSWORD"))


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


def check_csrf():
    token = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if not token or not expected or not hmac.compare_digest(token, expected):
        abort(400, description="Invalid or missing security token. Please refresh and try again.")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))

    if not admin_is_configured():
        return render_template(
            "admin_login.html",
            error="Admin login is not configured yet. Set ADMIN_USERNAME and ADMIN_PASSWORD_HASH in your environment variables.",
        ), 503

    error = None
    if request.method == "POST":
        if too_many("admin_login:" + client_ip(), 8, 900):
            error = "Too many failed login attempts. Please wait 15 minutes before trying again."
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if verify_admin_credentials(username, password):
                session.clear()
                session["admin_authenticated"] = True
                session["admin_user"] = username.strip()
                session["csrf_token"] = secrets.token_hex(24)
                logger.info("Admin user '%s' logged in from %s", username.strip(), client_ip())
                return redirect(url_for("admin_dashboard"))
            else:
                logger.warning("Failed admin login attempt for '%s' from %s", username.strip(), client_ip())
                error = "Invalid username or password. Please try again."

    return render_template("admin_login.html", error=error)


@app.post("/admin/logout")
@admin_required
def admin_logout():
    check_csrf()
    session.clear()
    return redirect(url_for("admin_login"))


# ---------- Admin Dashboard Pages & Actions ----------
@app.get("/admin")
@admin_required
def admin_dashboard():
    tab = request.args.get("tab", "overview")
    status_filter = request.args.get("status", "all")
    search_q = request.args.get("q", "").strip().lower()

    configured = gas_configured()
    enquiries = []
    projects = []
    api_warning = None

    if configured:
        enq_res = gas_get("get_enquiries")
        if enq_res.get("ok"):
            enquiries = enq_res.get("items", [])
        else:
            api_warning = enq_res.get("error")

        gal_res = gas_get("get_gallery")
        if gal_res.get("ok"):
            projects = gal_res.get("items", [])
        elif not api_warning:
            api_warning = gal_res.get("error")
    else:
        api_warning = "Google Sheets & Google Drive are not connected yet. Add GAS_WEBAPP_URL and GAS_API_SECRET to your .env file."

    total_projects = len(projects)
    total_images = sum(len(p.get("images", [])) for p in projects)
    total_enquiries = len(enquiries)
    new_enquiries_count = sum(1 for e in enquiries if e.get("status", "New").lower() == "new")

    filtered_enquiries = enquiries
    if status_filter != "all":
        filtered_enquiries = [
            e for e in filtered_enquiries
            if e.get("status", "New").lower() == status_filter.lower()
        ]
    if search_q:
        filtered_enquiries = [
            e for e in filtered_enquiries
            if (
                search_q in e.get("name", "").lower()
                or search_q in e.get("email", "").lower()
                or search_q in e.get("phone", "").lower()
                or search_q in e.get("subject", "").lower()
                or search_q in e.get("message", "").lower()
            )
        ]

    return render_template(
        "admin.html",
        tab=tab,
        status_filter=status_filter,
        search_q=search_q,
        configured=configured,
        api_warning=api_warning,
        admin_user=session.get("admin_user", "Admin"),
        csrf_token=session["csrf_token"],
        categories=GALLERY_CATEGORIES,
        stats={
            "total_projects": total_projects,
            "total_images": total_images,
            "total_enquiries": total_enquiries,
            "new_enquiries": new_enquiries_count,
        },
        recent_enquiries=enquiries[:5],
        recent_projects=projects[:4],
        enquiries=filtered_enquiries,
        projects=projects,
    )


@app.post("/admin/gallery/upload")
@admin_required
def admin_gallery_upload():
    check_csrf()
    title = request.form.get("title", "").strip()
    category = request.form.get("category", "completed").strip()
    description = request.form.get("description", "").strip()

    if not title or len(title) < 2:
        flash("Please enter a project title.", "error")
        return redirect(url_for("admin_dashboard", tab="gallery"))

    if category not in GALLERY_CATEGORIES:
        flash("Please select a valid project category.", "error")
        return redirect(url_for("admin_dashboard", tab="gallery"))

    uploaded_images = [f for f in request.files.getlist("images") if f and f.filename]
    if len(uploaded_images) > 10:
        flash("You can upload up to 10 photos per project at once.", "error")
        return redirect(url_for("admin_dashboard", tab="gallery"))

    encoded_images = []
    for f in uploaded_images:
        ok, err, raw_bytes, safe_name = validate_image_file(f)
        if not ok:
            flash(err, "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))
        encoded_images.append({
            "filename": safe_name,
            "mime_type": f.mimetype,
            "base64": base64.b64encode(raw_bytes).decode("ascii"),
        })

    # Videos: uploaded files + optional external video URL
    uploaded_videos = [f for f in request.files.getlist("videos") if f and f.filename]
    video_caption = request.form.get("video_caption", "").strip()
    video_url = request.form.get("video_url", "").strip()

    encoded_videos = []
    for f in uploaded_videos:
        ok, err, raw_bytes, safe_name = validate_video_file(f)
        if not ok:
            flash(err, "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))
        encoded_videos.append({
            "filename": safe_name,
            "mime_type": f.mimetype,
            "caption": video_caption,
            "base64": base64.b64encode(raw_bytes).decode("ascii"),
        })

    external_videos = []
    if video_url:
        external_videos.append({
            "url": video_url,
            "caption": video_caption,
            "embed_url": build_video_embed_url(video_url),
        })

    # Before & After pairs
    before_files = request.files.getlist("ba_before")
    after_files = request.files.getlist("ba_after")
    ba_captions = request.form.getlist("ba_caption")

    ba_pairs = []
    pair_count = max(len(before_files), len(after_files))
    for idx in range(pair_count):
        b_file = before_files[idx] if idx < len(before_files) and before_files[idx].filename else None
        a_file = after_files[idx] if idx < len(after_files) and after_files[idx].filename else None
        cap = ba_captions[idx].strip() if idx < len(ba_captions) else ""

        if not b_file and not a_file:
            continue
        if not b_file or not a_file:
            flash("Each Before & After comparison needs both a Before image and an After image.", "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))

        ok_b, err_b, bytes_b, name_b = validate_image_file(b_file)
        if not ok_b:
            flash(err_b, "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))

        ok_a, err_a, bytes_a, name_a = validate_image_file(a_file)
        if not ok_a:
            flash(err_a, "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))

        ba_pairs.append({
            "caption": cap,
            "before": {
                "filename": name_b,
                "mime_type": b_file.mimetype,
                "base64": base64.b64encode(bytes_b).decode("ascii"),
            },
            "after": {
                "filename": name_a,
                "mime_type": a_file.mimetype,
                "base64": base64.b64encode(bytes_a).decode("ascii"),
            },
        })

    if not encoded_images and not encoded_videos and not external_videos and not ba_pairs:
        flash("Please add at least one photo, video, or before/after comparison.", "error")
        return redirect(url_for("admin_dashboard", tab="gallery"))

    res = gas_post("add_project", {
        "title": title,
        "category": category,
        "description": description,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "files": encoded_images,
        "video_files": encoded_videos,
        "external_videos": external_videos,
        "before_after_pairs": ba_pairs,
    })

    if not res.get("ok"):
        flash(res.get("error") or "Failed to upload project to Google Drive/Sheets.", "error")
    else:
        invalidate_gallery_cache()
        flash(f"Project '{title}' published with photos, videos, and before/after media!", "success")

    return redirect(url_for("admin_dashboard", tab="gallery"))


@app.post("/admin/gallery/<project_id>/edit")
@admin_required
def admin_gallery_edit(project_id):
    check_csrf()
    title = request.form.get("title", "").strip()
    category = request.form.get("category", "completed").strip()
    description = request.form.get("description", "").strip()

    if not title:
        flash("Project title cannot be empty.", "error")
        return redirect(url_for("admin_dashboard", tab="gallery"))

    if category not in GALLERY_CATEGORIES:
        flash("Invalid project category.", "error")
        return redirect(url_for("admin_dashboard", tab="gallery"))

    existing_items = _fetch_all_gallery_items()
    existing_proj = next((p for p in existing_items if str(p.get("id")) == str(project_id)), None)

    remove_photo_ids = set(request.form.getlist("remove_photo_ids"))
    remove_video_keys = set(request.form.getlist("remove_video_keys"))
    remove_ba_keys = set(request.form.getlist("remove_ba_keys"))

    kept_photos = []
    kept_videos = []
    kept_ba = []
    trashed_file_ids = []

    if existing_proj:
        for p in (existing_proj.get("photos") or existing_proj.get("images") or []):
            fid = p.get("file_id", "")
            if fid and fid in remove_photo_ids:
                trashed_file_ids.append(fid)
            else:
                kept_photos.append(p)

        for v in (existing_proj.get("videos") or []):
            vkey = v.get("file_id") or v.get("url") or ""
            if vkey and vkey in remove_video_keys:
                if v.get("file_id"):
                    trashed_file_ids.append(v["file_id"])
            else:
                kept_videos.append(v)

        for ba in (existing_proj.get("before_after") or []):
            bakey = f"{ba.get('before_file_id', '')}|{ba.get('after_file_id', '')}|{ba.get('before_url', '')}"
            if bakey in remove_ba_keys:
                if ba.get("before_file_id"):
                    trashed_file_ids.append(ba["before_file_id"])
                if ba.get("after_file_id"):
                    trashed_file_ids.append(ba["after_file_id"])
            else:
                kept_ba.append(ba)

    # New photos
    new_uploads = [f for f in request.files.getlist("new_images") if f and f.filename]
    encoded_new_photos = []
    for f in new_uploads:
        ok, err, raw_bytes, safe_name = validate_image_file(f)
        if not ok:
            flash(err, "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))
        encoded_new_photos.append({
            "filename": safe_name,
            "mime_type": f.mimetype,
            "base64": base64.b64encode(raw_bytes).decode("ascii"),
        })

    # New videos (uploaded files + optional URL)
    new_video_files = [f for f in request.files.getlist("edit_videos") if f and f.filename]
    edit_video_caption = request.form.get("edit_video_caption", "").strip()
    edit_video_url = request.form.get("edit_video_url", "").strip()

    encoded_new_videos = []
    for f in new_video_files:
        ok, err, raw_bytes, safe_name = validate_video_file(f)
        if not ok:
            flash(err, "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))
        encoded_new_videos.append({
            "filename": safe_name,
            "mime_type": f.mimetype,
            "caption": edit_video_caption,
            "base64": base64.b64encode(raw_bytes).decode("ascii"),
        })

    new_external_videos = []
    if edit_video_url:
        new_external_videos.append({
            "url": edit_video_url,
            "embed_url": build_video_embed_url(edit_video_url),
            "caption": edit_video_caption or "Project Video",
        })

    # New Before & After pairs
    before_files = request.files.getlist("edit_ba_before")
    after_files = request.files.getlist("edit_ba_after")
    ba_captions = request.form.getlist("edit_ba_caption")

    new_ba_pairs = []
    pair_count = max(len(before_files), len(after_files))
    for idx in range(pair_count):
        b_file = before_files[idx] if idx < len(before_files) and before_files[idx].filename else None
        a_file = after_files[idx] if idx < len(after_files) and after_files[idx].filename else None
        cap = ba_captions[idx].strip() if idx < len(ba_captions) else ""

        if not b_file and not a_file:
            continue
        if not b_file or not a_file:
            flash("Each new Before & After comparison needs both a Before image and an After image.", "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))

        ok_b, err_b, bytes_b, name_b = validate_image_file(b_file)
        if not ok_b:
            flash(err_b, "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))

        ok_a, err_a, bytes_a, name_a = validate_image_file(a_file)
        if not ok_a:
            flash(err_a, "error")
            return redirect(url_for("admin_dashboard", tab="gallery"))

        new_ba_pairs.append({
            "caption": cap,
            "before": {
                "filename": name_b,
                "mime_type": b_file.mimetype,
                "base64": base64.b64encode(bytes_b).decode("ascii"),
            },
            "after": {
                "filename": name_a,
                "mime_type": a_file.mimetype,
                "base64": base64.b64encode(bytes_a).decode("ascii"),
            },
        })

    res = gas_post("update_project", {
        "id": project_id,
        "title": title,
        "category": category,
        "description": description,
        "kept_photos": kept_photos,
        "kept_videos": kept_videos,
        "kept_before_after": kept_ba,
        "trashed_file_ids": trashed_file_ids,
        "files": encoded_new_photos,
        "video_files": encoded_new_videos,
        "external_videos": new_external_videos,
        "before_after_pairs": new_ba_pairs,
    })

    if not res.get("ok"):
        flash(res.get("error") or "Failed to update project.", "error")
    else:
        invalidate_gallery_cache()
        flash(f"Project '{title}' updated successfully.", "success")

    return redirect(url_for("admin_dashboard", tab="gallery"))


@app.post("/admin/gallery/<project_id>/delete")
@admin_required
def admin_gallery_delete(project_id):
    check_csrf()
    res = gas_post("delete_project", {"id": project_id})
    if not res.get("ok"):
        flash(res.get("error") or "Failed to delete project.", "error")
    else:
        invalidate_gallery_cache()
        flash("Project and its Google Drive files have been deleted.", "success")
    return redirect(url_for("admin_dashboard", tab="gallery"))


@app.post("/admin/enquiries/<enquiry_id>/status")
@admin_required
def admin_enquiry_status(enquiry_id):
    check_csrf()
    new_status = request.form.get("status", "").strip()
    if new_status not in ("New", "Contacted", "Completed"):
        flash("Invalid enquiry status.", "error")
        return redirect(url_for("admin_dashboard", tab="enquiries"))

    res = gas_post("update_enquiry_status", {"id": enquiry_id, "status": new_status})
    if not res.get("ok"):
        flash(res.get("error") or "Failed to update enquiry status.", "error")
    else:
        flash(f"Enquiry marked as '{new_status}'.", "success")

    return redirect(url_for("admin_dashboard", tab="enquiries"))


@app.post("/admin/enquiries/<enquiry_id>/delete")
@admin_required
def admin_enquiry_delete(enquiry_id):
    check_csrf()
    res = gas_post("delete_enquiry", {"id": enquiry_id})
    if not res.get("ok"):
        flash(res.get("error") or "Failed to delete enquiry.", "error")
    else:
        flash("Enquiry deleted from Google Sheets.", "success")
    return redirect(url_for("admin_dashboard", tab="enquiries"))


@app.get("/admin/enquiries/export.csv")
@admin_required
def admin_export_csv():
    res = gas_get("get_enquiries")
    rows = res.get("items", []) if res.get("ok") else []

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ID", "Submitted At", "Customer Name", "Email", "Phone", "Subject", "Message", "Status"])

    def safe_cell(val):
        s = str(val or "")
        return ("'" + s) if s[:1] in "=+-@" else s

    for r in rows:
        writer.writerow([
            safe_cell(r.get("id")),
            safe_cell(r.get("submitted_at")),
            safe_cell(r.get("name")),
            safe_cell(r.get("email")),
            safe_cell(r.get("phone")),
            safe_cell(r.get("subject")),
            safe_cell(r.get("message")),
            safe_cell(r.get("status")),
        ])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=wwec-enquiries.csv"},
    )


# ---------- Health Check (for Render) ----------
@app.get("/healthz")
def healthz():
    return jsonify(ok=True, status="healthy")




# ---------- Serve Public pictures_vids Folder ----------
PICTURES_VIDS_DIR = os.path.join(PUBLIC_DIR, "pictures_vids")


@app.get("/pictures_vids/<path:filename>")
def serve_pictures_vids(filename):
    return send_from_directory(PICTURES_VIDS_DIR, filename)

# ---------- Serve Existing Static Website ----------
@app.get("/")
def home():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path):
    return send_from_directory(PUBLIC_DIR, path)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
