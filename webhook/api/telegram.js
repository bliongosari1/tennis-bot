// Telegram webhook for tennis-booker.
//
// Receives Telegram updates, sends an instant ack, and dispatches a
// GitHub `repository_dispatch` event so the heavy Playwright work runs
// in the existing GH Actions workflow.
//
// Required env vars (set in Vercel project settings):
//
//   TELEGRAM_BOT_TOKEN        — bot token from @BotFather
//   TELEGRAM_CHAT_ID          — allowed group chat id (negative number)
//   TELEGRAM_WEBHOOK_SECRET   — random string; also set on Telegram's setWebhook
//                                so we can verify inbound requests are from
//                                Telegram (compared against the
//                                X-Telegram-Bot-Api-Secret-Token header)
//   GITHUB_TOKEN              — PAT with `repo` scope
//   GITHUB_REPO               — "owner/repo" (e.g. "bliongosari1/tennis-bot")

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_CHAT_ID = process.env.TELEGRAM_CHAT_ID;
const WEBHOOK_SECRET = process.env.TELEGRAM_WEBHOOK_SECRET;
const GH_TOKEN = process.env.GITHUB_TOKEN;
const GH_REPO = process.env.GITHUB_REPO;

// Commands that run entirely in the webhook (no Playwright needed).
function quickReply(cmd, args) {
  if (cmd === "ping") return "🎾 pong";
  if (cmd === "help" || cmd === "start") {
    return [
      "<b>Tennis Booker commands</b>",
      "",
      "<b>See</b>",
      "/list — upcoming bookings",
      "/summary — today + next 3 days",
      "/scan — find free slots in next 5 days",
      "/settings — show current config",
      "",
      "<b>Book</b>",
      "/snipe_today /snipe_tomorrow /snipe_fri",
      "/snipe_tomorrow_8:30am /snipe_fri_7pm",
      "/snipe_2026-05-29_7pm",
      "",
      "<b>Cancel</b>",
      "/cancel_next — soonest booking",
      "/cancel_mon_2030 — Monday at 8:30 PM",
      "/cancel_fri_7pm — Friday at 7 PM",
      "",
      "<b>Utility</b>",
      "/ping /help",
    ].join("\n");
  }
  return null;
}

// Same parser as telegram_poller.py — extract (cmd, args) from text.
function parseCommand(text) {
  if (!text || !text.startsWith("/")) return [null, null];
  let rest = text.slice(1);
  let head, args;
  const spaceIdx = rest.indexOf(" ");
  if (spaceIdx === -1) {
    head = rest;
    args = "";
  } else {
    head = rest.slice(0, spaceIdx);
    args = rest.slice(spaceIdx + 1);
  }
  // Strip @botname suffix.
  if (head.includes("@")) head = head.split("@")[0];
  // Underscore syntax: /cancel_20260525_2030 → cmd='cancel', args='20260525_2030'
  let cmd;
  if (head.includes("_")) {
    const idx = head.indexOf("_");
    cmd = head.slice(0, idx);
    const argPart = head.slice(idx + 1);
    args = args ? `${argPart} ${args}` : argPart;
  } else {
    cmd = head;
  }
  return [cmd.toLowerCase(), args];
}

async function sendTelegram(chatId, text) {
  const res = await fetch(
    `https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
    },
  );
  if (!res.ok) {
    const body = await res.text();
    console.error("sendMessage failed:", res.status, body);
  }
}

async function dispatchToGitHub(payload) {
  const res = await fetch(
    `https://api.github.com/repos/${GH_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        event_type: "telegram_command",
        client_payload: payload,
      }),
    },
  );
  if (!res.ok) {
    const body = await res.text();
    console.error("repository_dispatch failed:", res.status, body);
    return false;
  }
  return true;
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "method not allowed" });
    return;
  }

  // Verify Telegram secret header.  If WEBHOOK_SECRET isn't set we allow
  // everything (dev mode), but log a warning.
  if (WEBHOOK_SECRET) {
    const provided = req.headers["x-telegram-bot-api-secret-token"];
    if (provided !== WEBHOOK_SECRET) {
      console.warn("Rejecting request — bad secret token");
      res.status(401).json({ error: "bad secret" });
      return;
    }
  } else {
    console.warn("TELEGRAM_WEBHOOK_SECRET not set — accepting all requests");
  }

  const update = req.body;
  const msg = update?.message;
  const chatId = msg?.chat?.id;
  const text = msg?.text || "";

  // Do all the work BEFORE responding.  Vercel serverless functions can
  // be terminated as soon as the response is sent, so we respond last.
  // Total time is ~500-1000ms which is fine — Telegram waits up to 60s.

  try {
    if (!chatId) {
      // Update without a chat (channel post, edited, etc.) — ack and skip.
    } else if (ALLOWED_CHAT_ID && String(chatId) !== String(ALLOWED_CHAT_ID)) {
      console.log(`Ignoring message from chat ${chatId} (not allowed)`);
    } else {
      const [cmd, args] = parseCommand(text);
      if (cmd) {
        const quick = quickReply(cmd, args);
        if (quick !== null) {
          await sendTelegram(chatId, quick);
        } else {
          const allowed = new Set([
            "list",
            "summary",
            "scan",
            "snipe",
            "cancel",
            "settings",
          ]);
          if (!allowed.has(cmd)) {
            await sendTelegram(
              chatId,
              `Unknown command <code>/${cmd}</code>. Try /help.`,
            );
          } else {
            // Send instant ack + dispatch to GH for the Playwright work.
            // sendTelegram + dispatchToGitHub in parallel for speed.
            await Promise.all([
              sendTelegram(chatId, `⏳ Got it — running <code>/${cmd}</code>…`),
              dispatchToGitHub({
                command: cmd,
                args: args || "",
                chat_id: String(chatId),
              }).then((ok) => {
                if (!ok) {
                  return sendTelegram(
                    chatId,
                    "❌ Failed to dispatch to GitHub. Check the webhook logs.",
                  );
                }
              }),
            ]);
          }
        }
      }
    }
  } catch (exc) {
    console.error("handler error:", exc);
  }

  res.status(200).json({ ok: true });
}
