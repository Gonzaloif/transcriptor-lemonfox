#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Transcriptor Lemonfox — Servidor web
"""

import os
import json
import time
import tempfile
import re
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import requests as req_lib

APP_DIR = Path(__file__).parent.resolve()

app = Flask(__name__, static_folder=str(APP_DIR / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

LEMONFOX_URL = "https://api.lemonfox.ai/v1/audio/transcriptions"
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".webm", ".mp4", ".ogg", ".flac", ".aac", ".mpga", ".opus"}


def get_api_key():
    return os.environ.get("LEMONFOX_API_KEY", "").strip()


def get_rapidapi_key():
    return os.environ.get("RAPIDAPI_KEY", "").strip()


def send_to_lemonfox(audio_path, filename, language, speaker_labels):
    api_key = get_api_key()
    if not api_key:
        return None, "API key no configurada en el servidor."

    response_format = "verbose_json" if speaker_labels else "text"
    mime_map = {
        ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".webm": "audio/webm", ".mp4": "audio/mp4", ".ogg": "audio/ogg",
        ".flac": "audio/flac", ".aac": "audio/aac", ".opus": "audio/opus",
    }
    ext = Path(filename).suffix.lower()
    mime = mime_map.get(ext, "application/octet-stream")

    with open(audio_path, "rb") as f:
        data = {"response_format": response_format}
        if language:
            data["language"] = language
        if speaker_labels:
            data["speaker_labels"] = "true"
        headers = {"Authorization": f"Bearer {api_key}"}

        max_attempts = 3
        resp = None
        for attempt in range(1, max_attempts + 1):
            f.seek(0)
            files = {"file": (filename, f, mime)}
            try:
                resp = req_lib.post(
                    LEMONFOX_URL, files=files, data=data,
                    headers=headers, timeout=(15, 900),
                )
            except req_lib.exceptions.Timeout:
                return None, "Timeout al contactar Lemonfox."
            except req_lib.exceptions.ConnectionError:
                return None, "Error de conexion con Lemonfox."

            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                time.sleep(2 ** attempt)
                continue

            try:
                body = resp.json()
                msg = body.get("error", {}).get("message", "") or json.dumps(body, ensure_ascii=False)
            except Exception:
                msg = resp.text[:500]
            return None, "Error HTTP %d: %s" % (resp.status_code, msg)

        if resp is None:
            return None, "No se pudo contactar a Lemonfox."

    if response_format == "text":
        return resp.text.strip(), None
    else:
        try:
            rdata = resp.json()
        except Exception:
            return None, "Respuesta inesperada de Lemonfox."
        segments = rdata.get("segments") or rdata.get("data") or []
        lines = []
        for seg in segments:
            spk = seg.get("speaker") or seg.get("speaker_label") or ""
            txt = (seg.get("text") or "").strip()
            if not txt:
                continue
            prefix = "[%s] " % spk if spk else ""
            lines.append(prefix + txt)
        text = "\n".join(lines) if lines else (rdata.get("text") or "").strip()
        return text, None


def extract_video_id(url):
    m = re.search(r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else None


def download_youtube_audio(video_id):
    rapidapi_key = get_rapidapi_key()
    if not rapidapi_key:
        return None, None, "RAPIDAPI_KEY no configurada."

    yt_url = "https://www.youtube.com/watch?v=" + video_id

    # Try primary API
    try:
        resp = req_lib.get(
            "https://youtube-to-mp315.p.rapidapi.com/download",
            params={"url": yt_url, "format": "mp3"},
            headers={
                "x-rapidapi-key": rapidapi_key,
                "x-rapidapi-host": "youtube-to-mp315.p.rapidapi.com",
            },
            timeout=120,
        )
        if resp.status_code == 200:
            data = resp.json()
            download_link = data.get("url") or data.get("link") or data.get("downloadUrl") or ""
            title = data.get("title", "youtube_audio")
            if download_link:
                audio_path, dl_err = fetch_audio_file(download_link)
                if not dl_err:
                    return audio_path, title, None
    except Exception:
        pass

    # Fallback: try youtube-mp36
    try:
        resp = req_lib.get(
            "https://youtube-mp36.p.rapidapi.com/dl",
            params={"id": video_id},
            headers={
                "x-rapidapi-key": rapidapi_key,
                "x-rapidapi-host": "youtube-mp36.p.rapidapi.com",
            },
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "")
            title = data.get("title", "youtube_audio")
            download_link = data.get("link", "")

            # Poll if processing
            polls = 0
            while status in ("processing", "in process") and polls < 8:
                time.sleep(8)
                polls += 1
                resp2 = req_lib.get(
                    "https://youtube-mp36.p.rapidapi.com/dl",
                    params={"id": video_id},
                    headers={
                        "x-rapidapi-key": rapidapi_key,
                        "x-rapidapi-host": "youtube-mp36.p.rapidapi.com",
                    },
                    timeout=60,
                )
                if resp2.status_code == 200:
                    data = resp2.json()
                    status = data.get("status", "")
                    download_link = data.get("link", "")
                    title = data.get("title", title)

            if status == "ok" and download_link:
                audio_path, dl_err = fetch_audio_file(download_link)
                if not dl_err:
                    return audio_path, title, None
                return None, title, dl_err
    except Exception:
        pass

    return None, None, "No se pudo descargar el audio de YouTube. Intenta con otro video."


def fetch_audio_file(download_link):
    try:
        audio_resp = req_lib.get(
            download_link,
            timeout=300,
            stream=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
            allow_redirects=True,
        )
        if audio_resp.status_code != 200:
            return None, "Error descargando MP3: HTTP %d" % audio_resp.status_code
    except Exception as e:
        return None, "Error descargando: %s" % str(e)[:200]

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    for chunk in audio_resp.iter_content(chunk_size=8192):
        tmp.write(chunk)
    tmp.close()

    if os.path.getsize(tmp.name) < 1000:
        os.unlink(tmp.name)
        return None, "Archivo descargado muy pequeno."

    return tmp.name, None


@app.route("/")
def index():
    return send_from_directory(str(APP_DIR / "static"), "index.html")


@app.route("/api/status", methods=["GET"])
def status():
    return jsonify({"ready": bool(get_api_key())})


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    audio = request.files.get("file")
    if not audio:
        return jsonify({"error": "No se recibio archivo de audio."}), 400

    ext = "." + audio.filename.rsplit(".", 1)[-1].lower() if "." in audio.filename else ""
    if ext not in AUDIO_EXTS:
        return jsonify({"error": "Formato no soportado: %s" % ext}), 400

    language = request.form.get("language", "spanish")
    speaker_labels = request.form.get("speaker_labels", "false") == "true"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        audio.save(tmp)
        tmp_path = tmp.name

    try:
        text, error = send_to_lemonfox(tmp_path, audio.filename, language, speaker_labels)
    finally:
        os.unlink(tmp_path)

    if error:
        return jsonify({"error": error}), 500
    return jsonify({"text": text})


@app.route("/api/transcribe-youtube", methods=["POST"])
def transcribe_youtube():
    data = request.json or {}
    url = data.get("url", "").strip()
    language = data.get("language", "spanish")
    speaker_labels = data.get("speaker_labels", False)

    if not url:
        return jsonify({"error": "No se proporciono URL."}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "URL no parece ser de YouTube."}), 400

    audio_path, title, dl_error = download_youtube_audio(video_id)
    if dl_error:
        return jsonify({"error": dl_error}), 400

    try:
        safe_title = re.sub(r'[^\w\s-]', '', title or "youtube_audio")[:80].strip()
        filename = "%s.mp3" % safe_title
        text, tx_error = send_to_lemonfox(audio_path, filename, language, speaker_labels)
    finally:
        if audio_path and os.path.exists(audio_path):
            os.unlink(audio_path)

    if tx_error:
        return jsonify({"error": tx_error}), 500
    return jsonify({"text": text, "title": title or "YouTube video"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print("\n  Transcriptor Lemonfox — http://localhost:%d\n" % port)
    app.run(host="0.0.0.0", port=port, debug=False)
