/**
 * Cloudflare Worker — Discord 슬래시 커맨드(/영상) 수신 엔드포인트.
 *
 * 디스코드의 "Interactions Endpoint URL"로 등록해서 씁니다.
 *   /영상 예약 channel topic [date]  — 다음 실행(또는 특정 날짜)에 쓸 주제 예약
 *   /영상 목록 [channel]            — 예약된 주제 목록 조회
 *   /영상 취소 channel [date]       — 예약 취소 (date 생략 시 "다음 실행용" 취소)
 *
 * config/topic_queue.json에 {channel: [{id, date, topic}, ...]} 형태로
 * GitHub Contents API를 통해 직접 커밋합니다. 평소엔 daily.yml이 완전
 * 자동(주제 랜덤 선택)으로 돌지만, 해당 채널의 실행 시점에 맞는 예약이 있으면
 * 그 주제를 대신 씁니다.
 *
 * 필요한 환경변수 (Cloudflare 대시보드 → Worker → Settings → Variables):
 *   DISCORD_PUBLIC_KEY   디스코드 개발자 포털 → General Information → Public Key
 *   GITHUB_TOKEN         repo(Contents: write) 권한의 GitHub 개인 액세스 토큰
 *   GITHUB_REPO          "kimchi-pancake/auto-video-pipeline" 형태
 *
 * 슬래시 커맨드 정의를 바꿨으면 tools/discord_worker/register_command.py를
 * 다시 실행해서 디스코드에 반영해야 합니다 (이 Worker 코드 자체는 등록을
 * 안 함 — 등록은 별도 일회성 작업).
 *
 * 배포: Cloudflare 대시보드에서 Worker 만들고 이 파일 내용을 그대로 붙여넣기.
 */

const QUEUE_PATH = "config/topic_queue.json";
const BRANCH = "master";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 슬래시 커맨드(재)등록용 경로. DISCORD_BOT_TOKEN/DISCORD_APP_ID 변수가
    // 없으면 그냥 404 — 필요할 때만 그 두 변수를 잠깐 추가하고 이 경로를
    // 한 번 호출한 뒤 다시 지우면 됩니다 (로컬/GitHub Actions에서는 디스코드
    // API 호출이 클라우드플레어에 막혀서 이 경로를 통해서만 등록이 됩니다).
    if (url.pathname === "/register-command-x7k2m9") {
      if (!env.DISCORD_BOT_TOKEN || !env.DISCORD_APP_ID) {
        return new Response("not configured", { status: 404 });
      }
      return registerCommand(env);
    }

    if (request.method !== "POST") {
      return new Response("OK", { status: 200 });
    }

    const signature = request.headers.get("X-Signature-Ed25519");
    const timestamp = request.headers.get("X-Signature-Timestamp");
    const body = await request.text();

    if (!signature || !timestamp) {
      return new Response("missing signature", { status: 401 });
    }

    const isValid = await verifyDiscordRequest(
      signature, timestamp, body, env.DISCORD_PUBLIC_KEY
    );
    if (!isValid) {
      return new Response("invalid request signature", { status: 401 });
    }

    const interaction = JSON.parse(body);

    // PING (디스코드가 엔드포인트 등록 시 자동으로 검증차 보냄)
    if (interaction.type === 1) {
      return json({ type: 1 });
    }

    if (interaction.type === 2) {
      return handleCommand(env, interaction);
    }

    return new Response("unknown interaction type", { status: 400 });
  },
};

async function handleCommand(env, interaction) {
  const sub = interaction.data.options?.[0];
  if (!sub) {
    return json({ type: 4, data: { content: "알 수 없는 명령어" } });
  }
  const opts = Object.fromEntries((sub.options || []).map((o) => [o.name, o.value]));

  try {
    if (sub.name === "예약") return await handleReserve(env, opts);
    if (sub.name === "목록") return await handleList(env, opts);
    if (sub.name === "취소") return await handleCancel(env, opts);
  } catch (e) {
    return json({ type: 4, data: { content: `오류: ${String(e).slice(0, 300)}` } });
  }

  return json({ type: 4, data: { content: "알 수 없는 명령어" } });
}

async function handleReserve(env, opts) {
  if (!opts.channel || !opts.topic) {
    return json({ type: 4, data: { content: "채널과 주제를 둘 다 입력해줘." } });
  }
  if (opts.date && !/^\d{4}-\d{2}-\d{2}$/.test(opts.date)) {
    return json({ type: 4, data: { content: "날짜는 YYYY-MM-DD 형식으로 입력해줘 (예: 2026-07-25)." } });
  }

  await withQueue(env, `queue topic for ${opts.channel}`, (queue) => {
    const entries = queue[opts.channel] || (queue[opts.channel] = []);
    entries.push({ id: randomId(), date: opts.date || null, topic: opts.topic });
  });

  const when = opts.date ? opts.date : "다음 정기 생성(매일 20:10 KST)";
  return json({
    type: 4,
    data: { content: `📌 [${opts.channel}] ${when} 때 "${opts.topic}" 주제로 만들게.` },
  });
}

async function handleList(env, opts) {
  const queue = await readQueue(env);
  const channels = opts.channel ? [opts.channel] : Object.keys(queue);
  const lines = [];
  for (const ch of channels) {
    for (const e of queue[ch] || []) {
      lines.push(`[${ch}] ${e.date || "다음 실행"} — "${e.topic}"`);
    }
  }
  return json({ type: 4, data: { content: lines.length ? lines.join("\n") : "예약된 주제 없음." } });
}

async function handleCancel(env, opts) {
  if (!opts.channel) {
    return json({ type: 4, data: { content: "채널을 입력해줘." } });
  }

  let cancelled = null;
  await withQueue(env, `cancel topic for ${opts.channel}`, (queue) => {
    const entries = queue[opts.channel] || [];
    const idx = opts.date
      ? entries.findIndex((e) => e.date === opts.date)
      : entries.findIndex((e) => !e.date);
    if (idx >= 0) {
      cancelled = entries[idx];
      entries.splice(idx, 1);
      if (entries.length) queue[opts.channel] = entries;
      else delete queue[opts.channel];
    }
  });

  if (!cancelled) {
    return json({ type: 4, data: { content: "취소할 예약을 못 찾음." } });
  }
  return json({ type: 4, data: { content: `🗑️ [${opts.channel}] "${cancelled.topic}" 예약 취소함.` } });
}

async function registerCommand(env) {
  const channelOption = {
    name: "channel",
    description: "채널 이름",
    type: 3,
    required: true,
    choices: [
      { name: "웃짬", value: "웃짬" },
      { name: "도개", value: "도개" },
    ],
  };

  const command = {
    name: "영상",
    description: "영상 주제 예약 관리",
    options: [
      {
        name: "예약",
        description: "다음(또는 특정 날짜) 생성 때 쓸 주제를 예약합니다",
        type: 1,
        options: [
          channelOption,
          { name: "topic", description: "영상 주제", type: 3, required: true },
          {
            name: "date",
            description: "특정 날짜 (YYYY-MM-DD, 생략하면 다음 실행)",
            type: 3,
            required: false,
          },
        ],
      },
      {
        name: "목록",
        description: "예약된 주제 목록을 봅니다",
        type: 1,
        options: [{ ...channelOption, required: false }],
      },
      {
        name: "취소",
        description: "예약된 주제를 취소합니다",
        type: 1,
        options: [
          channelOption,
          {
            name: "date",
            description: "취소할 예약의 날짜 (생략하면 '다음 실행용' 예약 취소)",
            type: 3,
            required: false,
          },
        ],
      },
    ],
  };

  const resp = await fetch(
    `https://discord.com/api/v10/applications/${env.DISCORD_APP_ID}/commands`,
    {
      method: "PUT", // 전체 덮어쓰기 — 이름 겹치는 예전 정의가 안 남음
      headers: {
        Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify([command]),
    }
  );
  const text = await resp.text();
  return new Response(`${resp.status} ${text}`, { status: 200 });
}

// ─────────────────────────────────────────
// config/topic_queue.json ↔ GitHub Contents API
// ─────────────────────────────────────────

async function fetchQueueWithSha(env) {
  const apiUrl = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${QUEUE_PATH}`;
  const headers = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "auto-video-pipeline-discord-bot",
  };
  const resp = await fetch(`${apiUrl}?ref=${BRANCH}`, { headers });
  if (resp.status === 404) return { queue: {}, sha: undefined };
  if (!resp.ok) throw new Error(`파일 조회 실패 (${resp.status})`);

  const data = await resp.json();
  const raw = JSON.parse(decodeBase64(data.content));
  // 예전 스키마({channel: "주제 문자열"}) 호환
  const queue = {};
  for (const [ch, val] of Object.entries(raw)) {
    queue[ch] = typeof val === "string" ? [{ id: randomId(), date: null, topic: val }] : val;
  }
  return { queue, sha: data.sha };
}

async function readQueue(env) {
  return (await fetchQueueWithSha(env)).queue;
}

async function withQueue(env, message, mutateFn) {
  const { queue, sha } = await fetchQueueWithSha(env);
  mutateFn(queue);

  const apiUrl = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${QUEUE_PATH}`;
  const putResp = await fetch(apiUrl, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "auto-video-pipeline-discord-bot",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      message: `${message} [skip ci]`,
      content: encodeBase64(JSON.stringify(queue, null, 2)),
      branch: BRANCH,
      ...(sha ? { sha } : {}),
    }),
  });
  if (!putResp.ok) {
    const errText = await putResp.text();
    throw new Error(`파일 갱신 실패 (${putResp.status}): ${errText.slice(0, 200)}`);
  }
}

function randomId() {
  return crypto.randomUUID().slice(0, 8);
}

function decodeBase64(b64) {
  const bytes = Uint8Array.from(atob(b64.replace(/\n/g, "")), (c) => c.charCodeAt(0));
  return new TextDecoder("utf-8").decode(bytes);
}

function encodeBase64(str) {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary);
}

function json(obj) {
  return new Response(JSON.stringify(obj), {
    headers: { "Content-Type": "application/json" },
  });
}

async function verifyDiscordRequest(signature, timestamp, body, publicKeyHex) {
  try {
    const key = await crypto.subtle.importKey(
      "raw",
      hexToBytes(publicKeyHex),
      { name: "Ed25519" },
      false,
      ["verify"]
    );
    const sig = hexToBytes(signature);
    const data = new TextEncoder().encode(timestamp + body);
    return await crypto.subtle.verify("Ed25519", key, sig, data);
  } catch (e) {
    return false;
  }
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  }
  return bytes;
}
