# Fix resolusi sd/hd (18 Nov 2026)

**Hanya 1 file yang berubah: `job.py`.** File lain (workflow, worker
Cloudflare, requirements) tidak perlu diubah dan tidak perlu deploy ulang.
Workflow kamu sudah menginstall `ffmpeg`, jadi `ffprobe` langsung tersedia.

---

# Update: perintah /hd dan /sd untuk download (18 Nov 2026)

Sekarang bot mengenali (tanpa slash juga bisa: `hd <link>` / `sd <link>`):
- `/hd <link>` — unduh format progressive HD asli Facebook
- `/sd <link>` — unduh format progressive SD asli Facebook
- Kombinasi tetap jalan: `caption /hd <link>` = video HD + caption

Kali ini **2 file berubah**:
1. `worker/src/index.js` — parsing kata hd/sd, teks bantuan /start & /help,
   label status (HD)/(SD). **File ini harus di-deploy ulang ke Cloudflare.**
2. `job.py` — menerima quality hd/sd (sebelumnya dipaksa jadi "best"),
   memilih format_id hd/sd di yt-dlp, dengan fallback: hd -> DASH >=720p
   terbaik, sd -> DASH <=480p, bila format aslinya tidak tersedia di video itu.
   Cukup push ke GitHub.

Sudah dites: 8 kasus parsing Worker (termasuk perintah lama 720, caption,
info — semuanya tetap sama) dan build_format job.py.

## Yang diubah di job.py
1. Tambah `_probe_url()` — probe URL stream pakai ffprobe
   (`-probesize 5000000`, hanya baca header, bukan unduh video penuh)
   untuk mengambil width/height/bitrate asli.
2. Tambah `_enrich_unknown_resolutions()` — semua format video yang
   resolusinya kosong dari yt-dlp (format legacy `sd`/`hd`) di-probe
   paralel sebelum daftar kualitas dibuat. Bitrate hasil probe diisi
   ke `tbr`, jadi perkiraan ukurannya juga muncul.
3. `build_formats_text()` — format hasil probe tampil sebagai baris
   sendiri, contoh: `• hd — 1280x720 (720p) — ~2.9 GB ⚠️ ...`
   Kalau probe gagal, fallback lama `(resolusi tidak dicantumkan)`
   tetap dipakai, daftar tetap terkirim.

## Cara pasang
Ganti `job.py` di repo GitHub dengan file ini (upload lewat web GitHub
atau `git push`). Selesai — tidak ada setting/secret baru.
