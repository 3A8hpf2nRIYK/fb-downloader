// Cloudflare Worker: menerima webhook Telegram -> memicu GitHub Actions
// yang mengunduh video Facebook lalu mengirimnya ke chat.

const FB_REGEX =
  /https?:\/\/(?:[\w-]+\.)?(?:facebook\.com|fb\.watch|fb\.com)\/\S+/i;

async function tg(env, method, payload) {
  const res = await fetch(
    `https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  return res.json();
}

async function dispatchWorkflow(env, inputs) {
  return fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "fb-downloader-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs }),
    }
  );
}

async function handle(msg, env) {
  if (!msg || !msg.text || !msg.chat || msg.chat.type !== "private") return;

  const chatId = msg.chat.id;
  const text = msg.text;

  // Batasi akses (opsional)
  const allowed = (env.ALLOWED_USERS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (allowed.length && !allowed.includes(String(msg.from.id))) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: "Maaf, kamu tidak punya akses ke bot ini.",
    });
    return;
  }

  if (text.startsWith("/start") || text.startsWith("/help")) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        "Halo! 👋\nKirim link video Facebook (publik), nanti aku kirim videonya.\n\n" +
        "Contoh: https://www.facebook.com/watch/?v=123456789\n" +
        "atau: https://fb.watch/xxxxxx/\n\n" +
        "Pilih kualitas (opsional): tambahkan angka di pesan, misalnya\n" +
        "720 https://fb.watch/xxxxxx/\n" +
        "Pilihan: 360, 480, 720, 1080, dst. Tanpa angka = kualitas tertinggi.\n\n" +
        "Atau unduh format asli Facebook: /hd atau /sd, misalnya\n" +
        "/hd https://fb.watch/xxxxxx/\n" +
        "/sd https://fb.watch/xxxxxx/\n\n" +
        "Hanya ambil judul & caption (tanpa video): tambahkan kata caption\n" +
        "caption https://fb.watch/xxxxxx/\n\n" +
        "Video + caption sekaligus: tambahkan kata caption dan angka kualitas (atau kata video)\n" +
        "caption 720p https://fb.watch/xxxxxx/\n\n" +
        "Lihat kualitas yang tersedia (tanpa mengunduh): tambahkan kata info\n" +
        "info https://fb.watch/xxxxxx/\n\n" +
        "Proses bisa makan waktu sekitar 1 menit. " +
        "Hanya unduh konten milikmu atau yang kamu punya izin.",
    });
    return;
  }

  const match = text.match(FB_REGEX);
  if (!match) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: "Kirim link video Facebook yang valid ya (facebook.com / fb.watch).",
    });
    return;
  }

  // Kualitas opsional: "720", "720p" atau "/720" di luar link. Default: tertinggi.
  // Format asli Facebook juga bisa dipilih: "hd"/"sd", "/hd" atau "/sd".
  const rest = text.replace(match[0], " ");
  const q = rest.match(
    /(?:^|[\s/])(144|240|360|480|540|720|1080|1440|2160)p?(?=$|\s)/i
  );
  const named = rest.match(/(?:^|\s)\/?(hd|sd)(?=$|\s)/i);
  const quality = named ? named[1].toLowerCase() : q ? q[1] : "best";

  // Kata "caption" / "judul" / "teks" di luar link meminta teks postingan:
  //  - sendirian               -> teks saja
  //  - + angka kualitas/"video" -> video + teks
  const wantsText = /(?:^|[\s/])(caption|judul|teks)(?=$|\s)/i.test(rest);
  const wantsVideo = !!q || !!named || /(?:^|[\s/])video(?=$|\s)/i.test(rest);
  const wantsFormats = /(?:^|[\s/])info(?=$|\s)/i.test(rest);
  const mode = wantsFormats
    ? "formats"
    : wantsText
    ? wantsVideo
      ? "both"
      : "caption"
    : "video";

  const qualityLabel = /^\d+$/.test(quality)
    ? `${quality}p`
    : quality.toUpperCase(); // hd -> "HD", sd -> "SD"
  const label =
    mode === "formats"
      ? " (cek kualitas)"
      : mode === "caption"
      ? " (caption)"
      : mode === "both"
      ? ` (video + caption${quality === "best" ? "" : `, ${qualityLabel}`})`
      : quality === "best"
      ? ""
      : ` (${qualityLabel})`;

  const status = await tg(env, "sendMessage", {
    chat_id: chatId,
    text: `⏳ Permintaan diterima${label}, memproses...`,
    reply_to_message_id: msg.message_id,
  });
  const statusId = status?.result?.message_id;

  const res = await dispatchWorkflow(env, {
    chat_id: String(chatId),
    url: match[0],
    user_msg_id: String(msg.message_id),
    status_msg_id: String(statusId),
    quality,
    mode,
  });

  if (res.status !== 204) {
    console.log("GitHub dispatch gagal:", res.status, await res.text());
    await tg(env, "editMessageText", {
      chat_id: chatId,
      message_id: statusId,
      text: `❌ Gagal memulai proses (GitHub ${res.status}). Coba lagi nanti.`,
    });
  }
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("OK");

    // Pastikan request benar-benar dari Telegram
    if (
      request.headers.get("X-Telegram-Bot-Api-Secret-Token") !==
      env.WEBHOOK_SECRET
    ) {
      return new Response("Forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad Request", { status: 400 });
    }

    // Balas Telegram cepat, kerjakan di belakang layar
    ctx.waitUntil(handle(update.message, env).catch((e) => console.log(e)));
    return new Response("OK");
  },
};
