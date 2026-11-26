import base64
import html as html_lib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .media_utils import convert_to_ogg_opus, convert_to_webp, convert_video_with_own_audio
from .wa_client import wa_manager

MEME_SOUNDS_DIR = os.path.join(settings.MEDIA_ROOT, "meme_sounds")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_REMOTE_SOUND_RE = re.compile(r"^/media/sounds/[a-zA-Z0-9._-]+\.mp3$")

# Simple in-memory per-IP rate limiter
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[tuple[str, str], list[float]] = {}


def _check_rate_limit(request, bucket: str, limit: int, window_sec: int = 60) -> bool:
    ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or request.META.get("REMOTE_ADDR", "unknown")
    now = time.time()
    key = (ip, bucket)
    with _RATE_LOCK:
        stamps = [t for t in _RATE_BUCKETS.get(key, []) if now - t < window_sec]
        if len(stamps) >= limit:
            _RATE_BUCKETS[key] = stamps
            return False
        stamps.append(now)
        _RATE_BUCKETS[key] = stamps
    return True


def _get_sid(request) -> str:
    sid = request.session.get("wa_sid")
    if not sid or not isinstance(sid, str) or len(sid) < 16:
        sid = secrets.token_urlsafe(24)
        request.session["wa_sid"] = sid
    return sid


@ensure_csrf_cookie
def index(request):
    _get_sid(request)
    return render(request, "stickers/index.html")


@require_GET
def api_status(request):
    try:
        client = wa_manager.get_client(_get_sid(request))
        data = client.get_status()
        return JsonResponse({"ok": True, **data})
    except Exception:
        return JsonResponse({"ok": False, "status": "error", "error": "Unable to reach WhatsApp Web right now."}, status=500)


@require_POST
def api_pair(request):
    if not _check_rate_limit(request, "pair", limit=6, window_sec=60):
        return JsonResponse({"ok": False, "error": "Too many pairing attempts. Please wait a minute."}, status=429)
    try:
        phone = request.POST.get("phone", "").strip()
        client = wa_manager.get_client(_get_sid(request))
        data = client.request_pairing_code(phone)
        return JsonResponse({"ok": True, **data})
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"ok": False, "error": "Could not generate pairing code. Check number and try again."}, status=400)


@require_GET
def api_chats(request):
    try:
        client = wa_manager.get_client(_get_sid(request))
        chats = client.get_chats()
        return JsonResponse({"ok": True, "chats": chats})
    except Exception:
        return JsonResponse({"ok": False, "error": "Failed to load chats."}, status=500)


@require_GET
def api_sounds_list(request):
    catalog_path = os.path.join(MEME_SOUNDS_DIR, "catalog.json")
    if not os.path.exists(catalog_path):
        return JsonResponse({"ok": True, "sounds": []})
    with open(catalog_path, "r", encoding="utf-8") as f:
        sounds = json.load(f)
    return JsonResponse({"ok": True, "sounds": sounds})


@require_GET
def api_sounds_search(request):
    if not _check_rate_limit(request, "search", limit=20, window_sec=60):
        return JsonResponse({"ok": False, "error": "Too many search requests. Please wait a moment."}, status=429)
    q = (request.GET.get("q") or "").strip()[:60]
    if not q:
        return JsonResponse({"ok": False, "error": "Enter a search keyword."}, status=400)
    try:
        url = f"https://www.myinstants.com/en/search/?name={urllib.parse.quote_plus(q)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        body = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", errors="ignore")
        matches = re.findall(r"play\('(/media/sounds/[^']+\.mp3)'[^)]*\)[^>]*title=\"([^\"]+)\"", body)
        results = []
        seen = set()
        for mp3_path, raw_title in matches:
            if mp3_path in seen or not ALLOWED_REMOTE_SOUND_RE.match(mp3_path):
                continue
            seen.add(mp3_path)
            title = html_lib.unescape(raw_title)
            if title.startswith("Play "):
                title = title[5:]
            if title.endswith(" sound"):
                title = title[:-6]
            results.append({
                "id": mp3_path,
                "title": title.strip(),
                "category": "Online Search",
                "url": "https://www.myinstants.com" + mp3_path,
            })
            if len(results) >= 24:
                break
        return JsonResponse({"ok": True, "sounds": results})
    except Exception:
        return JsonResponse({"ok": False, "error": "Online search failed. Please try again."}, status=500)


@require_GET
def serve_meme_sound(request, filename: str):
    if not re.match(r"^[a-zA-Z0-9._-]+\.mp3$", filename):
        raise Http404()
    full_path = os.path.realpath(os.path.join(MEME_SOUNDS_DIR, filename))
    if not full_path.startswith(os.path.realpath(MEME_SOUNDS_DIR) + os.sep) or not os.path.isfile(full_path):
        raise Http404()
    return FileResponse(open(full_path, "rb"), content_type="audio/mpeg")


def _load_preset_audio(preset_id: str) -> tuple[bytes, str]:
    preset_id = (preset_id or "").strip()
    if ALLOWED_REMOTE_SOUND_RE.match(preset_id):
        remote_url = "https://www.myinstants.com" + preset_id
        req = urllib.request.Request(remote_url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=15).read(MAX_UPLOAD_BYTES)
        return data, os.path.basename(preset_id)

    if re.match(r"^[a-zA-Z0-9._-]+\.mp3$", preset_id):
        local_path = os.path.realpath(os.path.join(MEME_SOUNDS_DIR, preset_id))
        if local_path.startswith(os.path.realpath(MEME_SOUNDS_DIR) + os.sep) and os.path.isfile(local_path):
            with open(local_path, "rb") as f:
                return f.read(), preset_id

    raise ValueError("Selected meme sound was not found.")


def _extract_edit_params(post):
    return {
        "fit_mode": post.get("fit_mode", "crop"),
        "zoom": float(post.get("zoom", 1.0) or 1.0),
        "pan_x": float(post.get("pan_x", 0.0) or 0.0),
        "pan_y": float(post.get("pan_y", 0.0) or 0.0),
        "start_sec": float(post.get("start_sec", 0.0) or 0.0),
        "max_seconds": float(post.get("clip_duration", 5.0) or 5.0),
        "rotate": int(post.get("rotate", 0) or 0),
        "flip_h": post.get("flip_h") == "1",
    }


@require_POST
def api_convert(request):
    if not _check_rate_limit(request, "convert", limit=25, window_sec=60):
        return JsonResponse({"ok": False, "error": "Too many conversions. Please slow down."}, status=429)
    try:
        visual_file = request.FILES.get("visual")
        audio_file = request.FILES.get("audio")
        preset_sound = request.POST.get("preset_sound", "").strip()
        use_video_audio = request.POST.get("use_video_audio") == "1"

        if not visual_file:
            return JsonResponse({"ok": False, "error": "Please upload an image, GIF, or video."}, status=400)
        if visual_file.size > MAX_UPLOAD_BYTES:
            return JsonResponse({"ok": False, "error": "Visual file must be 20 MB or smaller."}, status=400)
        if audio_file and audio_file.size > MAX_UPLOAD_BYTES:
            return JsonResponse({"ok": False, "error": "Audio file must be 20 MB or smaller."}, status=400)

        edit_params = _extract_edit_params(request.POST)
        visual_bytes = visual_file.read()

        if use_video_audio:
            webp_bytes, is_animated, ogg_bytes, duration_sec = convert_video_with_own_audio(
                visual_bytes, visual_file.name, **edit_params
            )
        else:
            webp_bytes, is_animated = convert_to_webp(visual_bytes, visual_file.name, **edit_params)
            if preset_sound:
                audio_bytes, audio_name = _load_preset_audio(preset_sound)
                ogg_bytes, duration_sec = convert_to_ogg_opus(audio_bytes, audio_name)
            elif audio_file:
                ogg_bytes, duration_sec = convert_to_ogg_opus(audio_file.read(), audio_file.name)
            else:
                return JsonResponse(
                    {"ok": False, "error": "Please pick a Malayalam Meme/BGM, record voice, upload audio, or use video audio."},
                    status=400,
                )

        return JsonResponse({
            "ok": True,
            "webp_b64": base64.b64encode(webp_bytes).decode("ascii"),
            "ogg_b64": base64.b64encode(ogg_bytes).decode("ascii"),
            "webp_kb": round(len(webp_bytes) / 1024, 1),
            "ogg_kb": round(len(ogg_bytes) / 1024, 1),
            "duration_sec": duration_sec,
            "is_animated": is_animated,
        })
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"ok": False, "error": "Could not convert media. Please check the file format."}, status=400)


@require_POST
def api_send(request):
    if not _check_rate_limit(request, "send", limit=20, window_sec=60):
        return JsonResponse({"ok": False, "error": "Rate limit reached. Please wait a minute."}, status=429)
    try:
        client = wa_manager.get_client(_get_sid(request))
        chat_id = request.POST.get("chat_id", "__SELF__").strip() or "__SELF__"
        webp_b64 = request.POST.get("webp_b64")
        ogg_b64 = request.POST.get("ogg_b64")
        duration_sec = request.POST.get("duration_sec")
        is_animated = request.POST.get("is_animated") == "true"

        if webp_b64 and ogg_b64:
            webp_bytes = base64.b64decode(webp_b64)
            ogg_bytes = base64.b64decode(ogg_b64)
            dur = max(1, min(8, int(duration_sec or 1)))
        else:
            visual_file = request.FILES.get("visual")
            audio_file = request.FILES.get("audio")
            preset_sound = request.POST.get("preset_sound", "").strip()
            use_video_audio = request.POST.get("use_video_audio") == "1"
            if not visual_file:
                return JsonResponse({"ok": False, "error": "Missing visual media."}, status=400)
            if visual_file.size > MAX_UPLOAD_BYTES:
                return JsonResponse({"ok": False, "error": "Visual file must be 20 MB or smaller."}, status=400)
            edit_params = _extract_edit_params(request.POST)
            visual_bytes = visual_file.read()
            if use_video_audio:
                webp_bytes, is_animated, ogg_bytes, dur = convert_video_with_own_audio(
                    visual_bytes, visual_file.name, **edit_params
                )
            else:
                webp_bytes, is_animated = convert_to_webp(visual_bytes, visual_file.name, **edit_params)
                if preset_sound:
                    audio_bytes, audio_name = _load_preset_audio(preset_sound)
                    ogg_bytes, dur = convert_to_ogg_opus(audio_bytes, audio_name)
                elif audio_file:
                    if audio_file.size > MAX_UPLOAD_BYTES:
                        return JsonResponse({"ok": False, "error": "Audio file must be 20 MB or smaller."}, status=400)
                    ogg_bytes, dur = convert_to_ogg_opus(audio_file.read(), audio_file.name)
                else:
                    return JsonResponse({"ok": False, "error": "Missing audio/voice."}, status=400)

        res = client.send_voice_sticker(
            chat_id=chat_id,
            webp_bytes=webp_bytes,
            ogg_bytes=ogg_bytes,
            duration_sec=dur,
            is_animated=is_animated,
        )
        return JsonResponse({"ok": True, **res})
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


@require_POST
def api_logout(request):
    try:
        sid = _get_sid(request)
        wa_manager.destroy_client(sid)
        request.session.flush()
        return JsonResponse({"ok": True})
    except Exception:
        return JsonResponse({"ok": False, "error": "Failed to reset session."}, status=500)
