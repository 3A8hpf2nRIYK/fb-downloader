"""
Dijalankan oleh GitHub Actions: unduh video Facebook lalu kirim ke Telegram.

- File <= 49 MB  : dikirim lewat Bot API biasa (HTTP).
- File  > 49 MB  : dikirim lewat MTProto (Telethon, pakai API_ID + API_HASH),
                   batas sampai ~2 GB.

Sebelum dikirim, file hasil unduhan di-probe pakai ffprobe dan dibuatkan
thumbnail JPEG pakai ffmpeg, lalu keduanya dilampirkan secara eksplisit
di kedua jalur kirim — tanpa itu pratinjau video tampil blank di Telegram.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yt_dlp

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
CHAT_ID = int(os.environ["CHAT_ID"])
URL = os.environ["URL"].strip()
USER_MSG_ID = int(os.environ["USER_MSG_ID"])
STATUS_MSG_ID = int(os.environ["STATUS_MSG_ID"])
QUALITY = os.environ.get("QUALITY", "best").strip().lower() or "best"
if QUALITY not in ("best", "hd", "sd") and not QUALITY.isdigit():
    QUALITY = "best"
MODE = os.environ.get("MODE", "video").strip().lower()
if MODE not in ("video", "caption", "both", "formats"):
    MODE = "video"

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOT_API_LIMIT = 49 * 1024 * 1024
MAX_SIZE = 2000 * 1024 * 1024

FB_REGEX = re.compile(
    r"https?://(?:[\w-]+\.)?(?:facebook\.com|fb\.watch|fb\.com)/\S+",
    re.IGNORECASE,
)


def set_status(text: str) -> None:
    try:
        requests.post(
            f"{API}/editMessageText",
            json={"chat_id": CHAT_ID, "message_id": STATUS_MSG_ID, "text": text},
            timeout=30,
        )
    except Exception as e:  # jangan cetak URL (ada token)
        print("set_status gagal:", type(e).__name__)


def delete_status() -> None:
    try:
        requests.post(
            f"{API}/deleteMessage",
            json={"chat_id": CHAT_ID, "message_id": STATUS_MSG_ID},
            timeout=30,
        )
    except Exception as e:
        print("delete_status gagal:", type(e).__name__)


def fetch_info(url: str) -> dict:
    """Ambil metadata postingan saja, tanpa mengunduh video."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def build_text(info: dict) -> str:
    title = (info.get("title") or "").strip()
    desc = (info.get("description") or "").strip()
    who = (info.get("uploader") or info.get("channel") or "").strip()

    parts = []
    # Judul di Facebook sering hanya potongan awal caption: jangan ditampilkan dua kali
    if title and not (desc and desc.startswith(title.rstrip(".… "))):
        parts.append(f"📝 Judul:\n{title}")
    parts.append(f"💬 Caption:\n{desc}" if desc else "💬 Caption: (tidak ada)")
    if who:
        parts.append(f"👤 {who}")
    return "\n\n".join(parts)


def send_text(text: str) -> None:
    chunks = [text[i : i + 3800] for i in range(0, len(text), 3800)] or ["(kosong)"]
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if i == 0:
            payload["reply_to_message_id"] = USER_MSG_ID
        r = requests.post(f"{API}/sendMessage", json=payload, timeout=30)
        if not r.ok:
            raise RuntimeError(f"sendMessage gagal (HTTP {r.status_code})")


def _size_of(fmt: dict, duration) -> int | None:
    """Ukuran format (byte). Bila tidak dilaporkan, perkirakan dari bitrate x durasi."""
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    tbr = fmt.get("tbr")
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


def _fmt_size(n: int) -> str:
    mb = n / 1024 / 1024
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def _probe_url(url: str, http_headers: dict | None = None) -> dict:
    """Ambil width/height/bitrate asli dari URL stream pakai ffprobe.

    Dipakai untuk format legacy Facebook (sd/hd): yt-dlp tidak menerima
    width/height/tbr untuk format itu dari Facebook, jadi satu-satunya
    cara akurat adalah mem-probe stream-nya langsung. ffprobe hanya
    membaca header lewat HTTP range request (dibatasi probesize 5 MB),
    bukan mengunduh seluruh video. ffmpeg sudah diinstall workflow.
    Return {} kalau gagal, supaya daftar kualitas tetap terkirim.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-probesize", "5000000",
        "-analyzeduration", "5000000",
    ]
    headers = http_headers or {}
    ua = headers.get("User-Agent") or headers.get("user-agent")
    if ua:
        cmd += ["-user_agent", ua]
    referer = headers.get("Referer") or headers.get("referer")
    if referer:
        cmd += ["-headers", f"Referer: {referer}\r\n"]
    cmd += [
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,bit_rate:format=bit_rate",
        "-of", "json",
        url,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        data = json.loads(out.stdout or "{}")
    except Exception as e:
        print("ffprobe gagal:", type(e).__name__)
        return {}
    stream = (data.get("streams") or [{}])[0]
    width, height = stream.get("width"), stream.get("height")
    if not width or not height:
        return {}
    bit_rate = (data.get("format") or {}).get("bit_rate") or stream.get("bit_rate")
    return {
        "width": int(width),
        "height": int(height),
        "bit_rate": int(bit_rate) if bit_rate else None,
    }


def _enrich_unknown_resolutions(formats: list) -> None:
    """Isi resolusi format yang kosong (sd/hd) hasil probe, in-place.

    Format yang berhasil di-probe ditandai `_probed` agar tetap tampil
    sebagai baris sd/hd sendiri (dengan resolusinya), bukan melebur ke
    bucket DASH berdasarkan height. Bitrate hasil probe juga diisi ke
    `tbr` supaya perkiraan ukurannya ikut muncul lewat _size_of().
    """
    targets = [
        f for f in formats
        if f.get("vcodec") != "none" and not f.get("height") and f.get("url")
    ]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
        results = list(
            pool.map(lambda f: _probe_url(f["url"], f.get("http_headers")), targets)
        )
    for fmt, res in zip(targets, results):
        if not res:
            continue
        fmt["width"], fmt["height"] = res["width"], res["height"]
        fmt["_probed"] = True
        if not fmt.get("tbr") and res.get("bit_rate"):
            fmt["tbr"] = res["bit_rate"] / 1000


def build_formats_text(info: dict) -> str:
    """Daftar kualitas video yang tersedia beserta perkiraan ukurannya."""
    duration = info.get("duration")
    formats = info.get("formats") or []
    _enrich_unknown_resolutions(formats)

    audio_only = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
    ]
    audio = max(audio_only, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    audio_size = _size_of(audio, duration) if audio else None

    by_height: dict = {}
    probed: dict = {}
    unknown: dict = {}
    for f in formats:
        if f.get("vcodec") == "none":
            continue
        h = f.get("height")
        if f.get("_probed"):
            # Format legacy (sd/hd) yang resolusinya baru ketahuan dari
            # probe: tampilkan sebagai baris sendiri, jangan dilebur
            # ke bucket DASH walau height-nya sama.
            bucket, key = probed, (f.get("format_id") or f.get("format_note") or "?")
        else:
            bucket = by_height if h else unknown
            key = h or (f.get("format_note") or f.get("format_id") or "?")
        cur = bucket.get(key)
        if cur is None or (f.get("tbr") or 0) > (cur.get("tbr") or 0):
            bucket[key] = f

    if not by_height and not probed and not unknown:
        return "❌ Daftar kualitas untuk video ini tidak ditemukan."

    def line(label: str, f: dict) -> str:
        size = _size_of(f, duration)
        if size and f.get("acodec") == "none" and audio_size:
            size += audio_size  # video-only: tambahkan ukuran audio
        text = f"• {label}"
        if size:
            text += f" — ~{_fmt_size(size)}"
            if size > 2000 * 1024 * 1024:
                text += " ⚠️ di atas 2 GB, tidak bisa dikirim"
        return text

    lines = ["🎞 Kualitas yang tersedia:"]
    for h in sorted(by_height, reverse=True):
        f = by_height[h]
        fps = f.get("fps")
        label = f"{h}p{int(fps)}" if fps and fps >= 50 else f"{h}p"
        lines.append(line(label, f))
    # Baris sd/hd: urut dari resolusi tertinggi (hd) ke terendah (sd).
    # "720p" di sini dari sisi pendek gambar, jadi video portrait
    # 720x1280 tetap terbaca 720p, dan dimensi aslinya ikut ditampilkan.
    for key, f in sorted(
        probed.items(), key=lambda kv: kv[1].get("height") or 0, reverse=True
    ):
        w, h = f.get("width"), f.get("height")
        p = h if w >= h else w
        lines.append(line(f"{key} — {w}x{h} ({p}p)", f))
    for key, f in unknown.items():
        lines.append(line(f"{key} (resolusi tidak dicantumkan)", f))

    footer = []
    if duration:
        d = int(duration)
        footer.append(
            f"⏱ Durasi {d // 3600}:{d % 3600 // 60:02d}:{d % 60:02d}"
            if d >= 3600
            else f"⏱ Durasi {d // 60:02d}:{d % 60:02d}"
        )
    footer.append("Ukuran hanya perkiraan.")
    footer.append("Pilih dengan angka, misalnya: 720 <link>")
    if by_height:
        footer.append(f"Tanpa angka = kualitas tertinggi ({max(by_height)}p).")
    return "\n".join(lines) + "\n\n" + "\n".join(footer)


def compose_caption(info: dict, mode: str, note: str) -> tuple[str, str]:
    """Return (caption untuk video, teks tambahan yang dikirim terpisah).

    Mode "both": judul + caption postingan ikut jadi caption video bila muat
    (batas Telegram 1024 karakter), kalau tidak dikirim sebagai pesan terpisah.
    """
    if mode == "both":
        full = build_text(info)
        joined = full + ("\n\n" + note if note else "")
        if len(joined) <= 1000:
            return joined, ""
        return note, full
    title = (info.get("title") or "")[:850]
    return (title + ("\n" + note if note else "")).strip(), ""


def build_format(quality: str) -> str:
    """Susun format yt-dlp. 'best' = tertinggi; angka = maksimal tinggi video.

    'hd'/'sd' = format progressive asli Facebook (format_id dari yt-dlp).
    Bila format itu tidak ada di video tersebut, jatuh ke DASH terdekat:
    hd -> >=720p terbaik, sd -> <=480p (atau terendah bila tidak ada).
    """
    if quality == "best":
        return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
    if quality == "hd":
        return (
            "hd"
            "/bv*[height>=720][ext=mp4]+ba[ext=m4a]"
            "/b[height>=720][ext=mp4]"
            "/bv*+ba/b"
        )
    if quality == "sd":
        return (
            "sd"
            "/bv*[height<=480][ext=mp4]+ba[ext=m4a]"
            "/b[height<=480][ext=mp4]"
            "/wv*+ba/w"
        )
    h = int(quality)
    return (
        f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]"
        f"/b[height<={h}][ext=mp4]"
        f"/bv*[height<={h}]+ba"
        f"/b[height<=?{h}]"  # tanda ? = ikutkan format yang tinggi videonya tidak diketahui
        "/wv*+ba/w"  # tidak ada yang <= h: ambil yang terendah tersedia
    )


def download_video(url: str, out_dir: str):
    opts = {
        "format": build_format(QUALITY),
        "merge_output_format": "mp4",
        "outtmpl": str(Path(out_dir) / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "max_filesize": MAX_SIZE,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [p for p in Path(out_dir).iterdir() if p.is_file()]
    if not files:
        raise FileNotFoundError("Tidak ada file hasil unduhan.")
    return max(files, key=lambda p: p.stat().st_size), info


def probe_video_metadata(path: Path) -> dict:
    """Durasi & dimensi dari FILE hasil unduhan, pakai ffprobe lokal.

    Jangan mengandalkan metadata yt-dlp: untuk format legacy Facebook
    (sd/hd) yt-dlp memang tidak memberi width/height, jadi atribut video
    yang dikirim ke Telegram kosong. File yang sudah terunduh selalu
    bisa di-probe secara akurat, dan probing file lokal itu cepat.
    Return dict berisi 0 untuk nilai yang gagal dibaca.
    """
    meta = {"duration": 0, "width": 0, "height": 0}
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        meta["width"] = int(stream.get("width") or 0)
        meta["height"] = int(stream.get("height") or 0)
        meta["duration"] = int(
            round(float((data.get("format") or {}).get("duration") or 0))
        )
    except Exception as e:  # metadata gagal tidak boleh menggagalkan kirim
        print("ffprobe metadata gagal:", type(e).__name__)
    return meta


def make_thumbnail(path: Path, duration: int) -> Path | None:
    """Satu frame video sebagai thumbnail (JPEG lebar 320 px, maks 200 KB).

    WAJIB dilampirkan secara eksplisit: tanpa thumbnail, Telegram —
    terutama untuk file besar yang diunggah lewat MTProto — menampilkan
    pratinjau kosong/blank seperti pada tangkapan layar user.

    Frame diambil bukan dari detik 0 (sering hitam/fade-in). Beberapa
    detik kandidat dicoba; frame yang hampir hitam terkompresi jadi
    sangat kecil, jadi kandidat pertama berukuran >= 8 KB langsung
    dipakai. Bila tidak ada, kandidat terbesar yang dipakai.
    Return None bila semua gagal, supaya video tetap terkirim.
    """
    if duration:
        candidates = [min(max(duration // 10, 1), 120), 5, 30, 1]
    else:
        candidates = [1, 5]
    seen: list[int] = []
    for ss in candidates:
        ss = int(ss)
        if ss not in seen and (not duration or ss < duration):
            seen.append(ss)

    best: Path | None = None
    best_size = -1
    for ss in seen:
        thumb = path.parent / f"thumb_{ss}.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(ss), "-i", str(path),
                    "-frames:v", "1",
                    "-vf", "scale=320:-2",
                    "-q:v", "5",
                    str(thumb),
                ],
                capture_output=True, timeout=60,
            )
        except Exception as e:
            print("ffmpeg thumbnail gagal:", type(e).__name__)
            continue
        if not thumb.is_file():
            continue
        size = thumb.stat().st_size
        if not (0 < size <= 200 * 1024):
            continue
        if size > best_size:
            best, best_size = thumb, size
        if size >= 8 * 1024:  # bukan frame (hampir) hitam
            return thumb
    return best


def _video_attrs(info: dict, meta: dict) -> dict:
    """Atribut video final: hasil probe file didahulukan, info yt-dlp cadangan."""
    return {
        "duration": meta.get("duration") or int(info.get("duration") or 0),
        "width": meta.get("width") or int(info.get("width") or 0),
        "height": meta.get("height") or int(info.get("height") or 0),
    }


def send_small(path: Path, info: dict, caption: str) -> None:
    attrs = _video_attrs(info, probe_video_metadata(path))
    data = {
        "chat_id": CHAT_ID,
        "caption": caption,
        "supports_streaming": "true",
        "reply_to_message_id": USER_MSG_ID,
    }
    for key in ("duration", "width", "height"):
        if attrs.get(key):
            data[key] = attrs[key]
    thumb = make_thumbnail(path, attrs["duration"])
    thumb_fh = thumb.open("rb") if thumb else None
    try:
        with path.open("rb") as f:
            files = {"video": (path.name, f, "video/mp4")}
            if thumb_fh is not None:
                files["thumbnail"] = (thumb.name, thumb_fh, "image/jpeg")
            r = requests.post(
                f"{API}/sendVideo",
                data=data,
                files=files,
                timeout=900,
            )
    finally:
        if thumb_fh is not None:
            thumb_fh.close()
    if not r.ok:
        raise RuntimeError(f"sendVideo gagal (HTTP {r.status_code})")


async def send_large(path: Path, info: dict, caption: str) -> None:
    from telethon import TelegramClient, functions, types
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), int(API_ID), API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    try:
        # Sesi baru belum "mengenal" user. Mengambil pesan masuk dari user
        # membuat Telethon menyimpan access_hash-nya, sehingga bisa kirim file.
        await client(
            functions.messages.GetMessagesRequest(
                id=[types.InputMessageID(USER_MSG_ID)]
            )
        )
        values = _video_attrs(info, probe_video_metadata(path))
        attrs = [
            types.DocumentAttributeVideo(
                duration=values["duration"],
                w=values["width"],
                h=values["height"],
                supports_streaming=True,
            )
        ]
        # Tanpa thumb=, unggahan MTProto tampil tanpa pratinjau (blank)
        # di klien Telegram — inilah jalur file >49 MB pada screenshot user.
        thumb = make_thumbnail(path, values["duration"])
        await client.send_file(
            CHAT_ID,
            path,
            caption=caption,
            attributes=attrs,
            thumb=str(thumb) if thumb else None,
            supports_streaming=True,
            reply_to=USER_MSG_ID,
        )
    finally:
        await client.disconnect()


def main() -> None:
    if not FB_REGEX.match(URL):
        set_status("❌ Link tidak valid.")
        sys.exit(1)

    if MODE in ("caption", "formats"):
        set_status(
            "⏳ Mengambil daftar kualitas..."
            if MODE == "formats"
            else "⏳ Mengambil caption..."
        )
        try:
            info = fetch_info(URL)
            send_text(
                build_formats_text(info) if MODE == "formats" else build_text(info)
            )
            delete_status()
        except yt_dlp.utils.DownloadError as e:
            print("DownloadError:", str(e)[:300])
            set_status(
                "❌ Gagal mengambil data. Kemungkinan postingan privat, "
                "dihapus, atau butuh login."
            )
            sys.exit(1)
        except Exception as e:
            print("Error:", type(e).__name__, str(e)[:300])
            set_status("❌ Terjadi kesalahan saat mengambil data.")
            sys.exit(1)
        return

    set_status("⏳ Sedang mengunduh video...")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path, info = download_video(URL, tmp)
            size = path.stat().st_size
            mb = size / 1024 / 1024
            got = info.get("height")
            note = ""
            if QUALITY.isdigit() and got and int(got) != int(QUALITY):
                note = f"[{got}p, karena {QUALITY}p tidak tersedia]"
            caption, extra_text = compose_caption(info, MODE, note)
            print(f"Terunduh: {mb:.1f} MB")

            if size <= BOT_API_LIMIT:
                set_status(f"📤 Mengirim video ({mb:.1f} MB)...")
                send_small(path, info, caption)
            elif API_ID and API_HASH:
                set_status(f"📤 Mengunggah file besar ({mb:.1f} MB)...")
                asyncio.run(send_large(path, info, caption))
            else:
                set_status(
                    f"❌ Video {mb:.1f} MB (>50 MB) dan API_ID/API_HASH belum diisi."
                )
                sys.exit(1)

            if extra_text:
                try:
                    send_text(extra_text)
                except Exception as e:
                    print("send_text gagal:", type(e).__name__)

            delete_status()

        except yt_dlp.utils.DownloadError as e:
            print("DownloadError:", str(e)[:300])
            set_status(
                "❌ Gagal mengunduh. Kemungkinan video privat, dihapus, "
                "butuh login, atau Facebook memblokir server."
            )
            sys.exit(1)
        except FileNotFoundError:
            set_status("❌ Video tidak ditemukan atau lebih besar dari 2 GB.")
            sys.exit(1)
        except Exception as e:
            print("Error:", type(e).__name__, str(e)[:300])
            set_status("❌ Terjadi kesalahan saat memproses video.")
            sys.exit(1)


if __name__ == "__main__":
    main()
