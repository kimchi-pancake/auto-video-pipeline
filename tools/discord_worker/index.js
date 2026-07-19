/**
 * Cloudflare Worker — Discord 슬래시 커맨드(/영상) 수신 엔드포인트.
 *
 * 디스코드의 "Interactions Endpoint URL"로 등록해서 씁니다.
 *   /영상 예약 channel topic [date]     — 다음 실행(또는 특정 날짜)에 쓸 주제 예약
 *   /영상 목록 [channel] [검색어]        — 예약된 주제 목록 조회 (검색어로 필터 가능)
 *   /영상 취소 channel [date]           — 예약 취소 (date 생략 시 "다음 실행용" 취소)
 *   /영상 분석 [channel]                — 소재 카테고리별 성과(조회수) 요약
 *   /영상 주제생성 channel count [category] — 성과 데이터 참고해서 주제 N개 생성+예약
 *   /영상 상태                          — 현재 실행 중인 작업 / 오늘 업로드 수 / 대기열
 *   /영상 로그                          — 최근 워크플로우 실행 기록 링크
 *   /영상 재생성 channel topic          — 그 자리에서 즉시 새로 생성(전체 재생성)
 *   /영상 cta설정 channel position on   — 구독 유도 문구 위치별 on/off
 *   /영상 거절 video_id                 — 소프트 승인 대기 중인 영상 거절(비공개 유지)
 *
 * config/*.json을 GitHub Contents API로 직접 읽고 씁니다. 평소엔 daily.yml이
 * 완전 자동으로 돌지만, 이 큐/설정 파일들에 예약·설정이 있으면 그걸 반영합니다.
 *
 * 필요한 환경변수 (Cloudflare 대시보드 → Worker → Settings → Variables):
 *   DISCORD_PUBLIC_KEY   디스코드 개발자 포털 → General Information → Public Key
 *   GITHUB_TOKEN         repo(Contents: write, Actions: write) 권한의 GitHub PAT
 *   GITHUB_REPO          "kimchi-pancake/auto-video-pipeline" 형태
 *
 * 슬래시 커맨드 정의를 바꿨으면 GITHUB_TOKEN/DISCORD_APP_ID/DISCORD_BOT_TOKEN을
 * 잠깐 추가하고 /register-command-x7k2m9 를 한 번 호출해서 반영해야 합니다.
 */

const BRANCH = "master";
const QUEUE_PATH = "config/topic_queue.json";
const REGISTRY_PATH = "config/video_registry.json";
const CTA_PATH = "config/cta_settings.json";
const REJECTIONS_PATH = "config/rejections.json";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

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
    const isValid = await verifyDiscordRequest(signature, timestamp, body, env.DISCORD_PUBLIC_KEY);
    if (!isValid) {
      return new Response("invalid request signature", { status: 401 });
    }

    const interaction = JSON.parse(body);

    if (interaction.type === 1) {
      return json({ type: 1 }); // PING
    }
    if (interaction.type === 2) {
      return handleCommand(env, interaction);
    }
    return new Response("unknown interaction type", { status: 400 });
  },
};

// ─────────────────────────────────────────
// 커맨드 라우팅
// ─────────────────────────────────────────

async function handleCommand(env, interaction) {
  const sub = interaction.data.options?.[0];
  if (!sub) {
    return json({ type: 4, data: { content: "알 수 없는 명령어" } });
  }
  const opts = Object.fromEntries((sub.options || []).map((o) => [o.name, o.value]));

  try {
    switch (sub.name) {
      case "예약": return await handleReserve(env, opts);
      case "목록": return await handleList(env, opts);
      case "취소": return await handleCancel(env, opts);
      case "분석": return await handleAnalyze(env, opts);
      case "주제생성": return await handleGenerateTopics(env, opts);
      case "상태": return await handleStatus(env);
      case "로그": return await handleLogs(env);
      case "재생성": return await handleRegenerate(env, opts);
      case "cta설정": return await handleCtaSettings(env, opts);
      case "거절": return await handleReject(env, opts);
      default: return json({ type: 4, data: { content: "알 수 없는 명령어" } });
    }
  } catch (e) {
    return json({ type: 4, data: { content: `오류: ${String(e).slice(0, 300)}` } });
  }
}

// ─────────────────────────────────────────
// 예약 / 목록 / 취소
// ─────────────────────────────────────────

async function handleReserve(env, opts) {
  if (!opts.channel || !opts.topic) {
    return json({ type: 4, data: { content: "채널과 주제를 둘 다 입력해줘." } });
  }
  if (opts.date && !/^\d{4}-\d{2}-\d{2}$/.test(opts.date)) {
    return json({ type: 4, data: { content: "날짜는 YYYY-MM-DD 형식으로 입력해줘 (예: 2026-07-25)." } });
  }

  await ghUpdateFile(env, QUEUE_PATH, migrateQueue, `queue topic for ${opts.channel}`, (queue) => {
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
  const { data } = await ghGetFile(env, QUEUE_PATH);
  const queue = migrateQueue(data || {});
  const channels = opts.channel ? [opts.channel] : Object.keys(queue);
  const keyword = (opts["검색어"] || "").trim();

  const lines = [];
  for (const ch of channels) {
    for (const e of queue[ch] || []) {
      if (keyword && !e.topic.includes(keyword)) continue;
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
  await ghUpdateFile(env, QUEUE_PATH, migrateQueue, `cancel topic for ${opts.channel}`, (queue) => {
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

function migrateQueue(raw) {
  // 예전 스키마({channel: "주제 문자열"}) 호환
  const queue = {};
  for (const [ch, val] of Object.entries(raw || {})) {
    queue[ch] = typeof val === "string" ? [{ id: randomId(), date: null, topic: val }] : val;
  }
  return queue;
}

// ─────────────────────────────────────────
// 분석
// ─────────────────────────────────────────

async function handleAnalyze(env, opts) {
  const { data } = await ghGetFile(env, REGISTRY_PATH);
  const entries = (data || []).filter((e) => e.stats && (!opts.channel || e.channel === opts.channel));
  if (!entries.length) {
    return json({ type: 4, data: { content: "아직 분석할 데이터가 없음 (통계 수집 전이거나 업로드된 영상이 없음)." } });
  }

  const byCat = {};
  for (const e of entries) {
    const cat = e.category || "미분류";
    (byCat[cat] ||= []).push(e.stats);
  }
  const rows = Object.entries(byCat)
    .map(([cat, statsList]) => {
      const n = statsList.length;
      const avgViews = statsList.reduce((s, x) => s + (x.views || 0), 0) / n;
      const avgLikes = statsList.reduce((s, x) => s + (x.likes || 0), 0) / n;
      return { cat, n, avgViews, avgLikes };
    })
    .sort((a, b) => b.avgViews - a.avgViews);

  const lines = ["📊 소재별 성과 (조회수 기준, 평균)"];
  for (const r of rows) {
    lines.push(`${r.cat}: 조회수 ${Math.round(r.avgViews)} · 좋아요 ${Math.round(r.avgLikes)} (${r.n}개)`);
  }
  if (rows.length >= 2) {
    lines.push("", `💡 "${rows[0].cat}" 소재가 제일 잘 됨 — 이 쪽 위주로 더 만들어보는 걸 추천.`);
  }
  return json({ type: 4, data: { content: lines.join("\n") } });
}

// ─────────────────────────────────────────
// 주제생성 (GitHub Actions 트리거)
// ─────────────────────────────────────────

async function handleGenerateTopics(env, opts) {
  if (!opts.channel || !opts.count) {
    return json({ type: 4, data: { content: "채널과 개수를 입력해줘." } });
  }
  const ok = await dispatchWorkflow(env, "generate_topics.yml", {
    channel: opts.channel,
    category: opts.category || "",
    count: String(opts.count),
  });
  if (!ok.success) {
    return json({ type: 4, data: { content: `트리거 실패 (${ok.status}): ${ok.detail}` } });
  }
  return json({
    type: 4,
    data: { content: `🧠 [${opts.channel}] 주제 ${opts.count}개 생성 시작. 완료되면 예약 목록에 자동으로 쌓이고 디스코드로 알림 옴.` },
  });
}

// ─────────────────────────────────────────
// 상태 / 로그
// ─────────────────────────────────────────

async function handleStatus(env) {
  const headers = ghHeaders(env);
  let statusLine = "확인 불가";
  try {
    const resp = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/runs?per_page=5`, { headers });
    if (resp.ok) {
      const runsData = await resp.json();
      const active = (runsData.workflow_runs || []).find((r) => r.status === "in_progress" || r.status === "queued");
      statusLine = active ? `🟡 실행 중: ${active.name} (${active.status})` : "🟢 대기 중 (실행 중인 작업 없음)";
    }
  } catch (e) {
    // statusLine 기본값 유지
  }

  const { data: registry } = await ghGetFile(env, REGISTRY_PATH);
  const today = new Date().toISOString().slice(0, 10);
  const todayCount = (registry || []).filter((e) => (e.uploaded_at || "").slice(0, 10) === today).length;

  const { data: rawQueue } = await ghGetFile(env, QUEUE_PATH);
  const queue = migrateQueue(rawQueue || {});
  const pendingCount = Object.values(queue).reduce((s, arr) => s + arr.length, 0);

  const lines = [
    "🎬 현재 상태",
    statusLine,
    `오늘 업로드된 영상: ${todayCount}개`,
    `대기 중인 예약 주제: ${pendingCount}개`,
  ];
  return json({ type: 4, data: { content: lines.join("\n") } });
}

async function handleLogs(env) {
  const headers = ghHeaders(env);
  const resp = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/runs?per_page=5`, { headers });
  if (!resp.ok) {
    return json({ type: 4, data: { content: "로그 조회 실패." } });
  }
  const data = await resp.json();
  const runs = data.workflow_runs || [];
  if (!runs.length) {
    return json({ type: 4, data: { content: "실행 기록이 없음." } });
  }
  const lines = ["📜 최근 실행 기록"];
  for (const r of runs.slice(0, 5)) {
    lines.push(`${r.name} — ${r.status}/${r.conclusion || "-"}\n${r.html_url}`);
  }
  return json({ type: 4, data: { content: lines.join("\n") } });
}

// ─────────────────────────────────────────
// 재생성 (즉시, 전체 재생성만 지원)
// ─────────────────────────────────────────

async function handleRegenerate(env, opts) {
  if (!opts.channel || !opts.topic) {
    return json({ type: 4, data: { content: "채널과 주제를 입력해줘." } });
  }
  const ok = await dispatchWorkflow(env, "on_demand.yml", { channel: opts.channel, topic: opts.topic });
  if (!ok.success) {
    return json({ type: 4, data: { content: `재생성 트리거 실패 (${ok.status}): ${ok.detail}` } });
  }
  return json({ type: 4, data: { content: `🔁 [${opts.channel}] "${opts.topic}" 재생성 시작. 완료되면 알림 옴.` } });
}

// ─────────────────────────────────────────
// CTA 설정
// ─────────────────────────────────────────

async function handleCtaSettings(env, opts) {
  if (!opts.channel || !opts.position || opts.on === undefined) {
    return json({ type: 4, data: { content: "채널, 위치, on/off를 다 입력해줘." } });
  }
  const enabled = !!opts.on;
  await ghUpdateFile(env, CTA_PATH, (x) => x || {}, `update cta settings for ${opts.channel}`, (obj) => {
    const cur = obj[opts.channel] || { early: false, middle: true, before_end: true, ending: true };
    cur[opts.position] = enabled;
    obj[opts.channel] = cur;
  });
  return json({ type: 4, data: { content: `⚙️ [${opts.channel}] ${opts.position} CTA ${enabled ? "켜짐" : "꺼짐"}` } });
}

// ─────────────────────────────────────────
// 거절 (소프트 승인)
// ─────────────────────────────────────────

async function handleReject(env, opts) {
  if (!opts.video_id) {
    return json({ type: 4, data: { content: "영상 ID(또는 링크)를 입력해줘." } });
  }
  const videoId = extractVideoId(opts.video_id);
  await ghUpdateFile(env, REJECTIONS_PATH, (x) => x || { rejected: [] }, `reject video ${videoId}`, (obj) => {
    obj.rejected = obj.rejected || [];
    if (!obj.rejected.includes(videoId)) obj.rejected.push(videoId);
  });
  return json({
    type: 4,
    data: { content: `🚫 ${videoId} 거절 처리함 — 공개 대기 중이면 비공개로 남고, 이미 공개됐으면 유튜브 스튜디오에서 직접 내려야 함.` },
  });
}

function extractVideoId(input) {
  const m = input.match(/(?:youtu\.be\/|v=)([a-zA-Z0-9_-]{6,})/);
  return m ? m[1] : input.trim();
}

// ─────────────────────────────────────────
// GitHub API 공통 헬퍼
// ─────────────────────────────────────────

function ghHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "auto-video-pipeline-discord-bot",
  };
}

async function ghGetFile(env, path) {
  const apiUrl = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${path}`;
  const resp = await fetch(`${apiUrl}?ref=${BRANCH}`, { headers: ghHeaders(env) });
  if (resp.status === 404) return { data: null, sha: undefined };
  if (!resp.ok) throw new Error(`파일 조회 실패 (${resp.status}): ${path}`);
  const json_ = await resp.json();
  return { data: JSON.parse(decodeBase64(json_.content)), sha: json_.sha };
}

async function ghPutFile(env, path, data, sha, message) {
  const apiUrl = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${path}`;
  const resp = await fetch(apiUrl, {
    method: "PUT",
    headers: { ...ghHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify({
      message: `${message} [skip ci]`,
      content: encodeBase64(JSON.stringify(data, null, 2)),
      branch: BRANCH,
      ...(sha ? { sha } : {}),
    }),
  });
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`파일 갱신 실패 (${resp.status}): ${errText.slice(0, 200)}`);
  }
}

/** path의 JSON을 읽어(defaultFn으로 정규화) mutateFn으로 수정한 뒤 다시 커밋합니다. */
async function ghUpdateFile(env, path, defaultFn, message, mutateFn) {
  const { data, sha } = await ghGetFile(env, path);
  const obj = defaultFn(data);
  mutateFn(obj);
  await ghPutFile(env, path, obj, sha, message);
  return obj;
}

async function dispatchWorkflow(env, workflowFile, inputs) {
  const resp = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflowFile}/dispatches`,
    {
      method: "POST",
      headers: { ...ghHeaders(env), "Content-Type": "application/json" },
      body: JSON.stringify({ ref: BRANCH, inputs }),
    }
  );
  if (!resp.ok) {
    const detail = await resp.text();
    return { success: false, status: resp.status, detail: detail.slice(0, 200) };
  }
  return { success: true };
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
  return new Response(JSON.stringify(obj), { headers: { "Content-Type": "application/json" } });
}

// ─────────────────────────────────────────
// 서명 검증
// ─────────────────────────────────────────

async function verifyDiscordRequest(signature, timestamp, body, publicKeyHex) {
  try {
    const key = await crypto.subtle.importKey("raw", hexToBytes(publicKeyHex), { name: "Ed25519" }, false, ["verify"]);
    const sig = hexToBytes(signature);
    const data = new TextEncoder().encode(timestamp + body);
    return await crypto.subtle.verify("Ed25519", key, sig, data);
  } catch (e) {
    return false;
  }
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  return bytes;
}

// ─────────────────────────────────────────
// 슬래시 커맨드 (재)등록
// ─────────────────────────────────────────

async function registerCommand(env) {
  const channelChoice = {
    name: "channel",
    description: "채널 이름",
    type: 3,
    required: true,
    choices: [
      { name: "웃짬", value: "웃짬" },
      { name: "도개", value: "도개" },
    ],
  };
  const channelChoiceOptional = { ...channelChoice, required: false };
  const categoryChoice = {
    name: "category",
    description: "소재 카테고리 (생략하면 성과 좋은 소재 위주로 자동 선택)",
    type: 3,
    required: false,
    choices: [
      "고부갈등", "부모자식갈등", "손주문제", "형제유산", "노년외로움",
      "부부갈등", "부모부양", "명절갈등", "노후자금", "가족오해", "반전사연", "재회",
    ].map((c) => ({ name: c, value: c })),
  };

  const command = {
    name: "영상",
    description: "영상 자동화 관리",
    options: [
      {
        name: "예약", description: "다음(또는 특정 날짜) 생성 때 쓸 주제를 예약합니다", type: 1,
        options: [
          channelChoice,
          { name: "topic", description: "영상 주제", type: 3, required: true },
          { name: "date", description: "특정 날짜 (YYYY-MM-DD, 생략하면 다음 실행)", type: 3, required: false },
        ],
      },
      {
        name: "목록", description: "예약된 주제 목록을 봅니다", type: 1,
        options: [
          channelChoiceOptional,
          { name: "검색어", description: "주제 내용에 포함된 키워드로 필터", type: 3, required: false },
        ],
      },
      {
        name: "취소", description: "예약된 주제를 취소합니다", type: 1,
        options: [
          channelChoice,
          { name: "date", description: "취소할 예약의 날짜 (생략하면 '다음 실행용' 예약 취소)", type: 3, required: false },
        ],
      },
      {
        name: "분석", description: "소재 카테고리별 성과(조회수) 요약을 봅니다", type: 1,
        options: [channelChoiceOptional],
      },
      {
        name: "주제생성", description: "과거 성과를 참고해서 새 주제를 여러 개 생성/예약합니다", type: 1,
        options: [
          channelChoice,
          { name: "count", description: "생성할 개수", type: 4, required: true, min_value: 1, max_value: 30 },
          categoryChoice,
        ],
      },
      { name: "상태", description: "현재 실행 중인 작업과 오늘의 진행 상황을 봅니다", type: 1, options: [] },
      { name: "로그", description: "최근 워크플로우 실행 기록을 봅니다", type: 1, options: [] },
      {
        name: "재생성", description: "지정한 주제로 지금 바로 새로 생성합니다 (전체 재생성)", type: 1,
        options: [channelChoice, { name: "topic", description: "영상 주제", type: 3, required: true }],
      },
      {
        name: "cta설정", description: "구독/좋아요 유도 문구 위치를 켜고 끕니다", type: 1,
        options: [
          channelChoice,
          {
            name: "position", description: "위치", type: 3, required: true,
            choices: [
              { name: "초반(비추천)", value: "early" },
              { name: "중간", value: "middle" },
              { name: "결말직전", value: "before_end" },
              { name: "마지막", value: "ending" },
            ],
          },
          { name: "on", description: "켤지 여부", type: 5, required: true },
        ],
      },
      {
        name: "거절", description: "소프트 승인 대기 중인 영상을 거절합니다 (비공개로 유지)", type: 1,
        options: [{ name: "video_id", description: "유튜브 영상 ID 또는 링크", type: 3, required: true }],
      },
    ],
  };

  const resp = await fetch(`https://discord.com/api/v10/applications/${env.DISCORD_APP_ID}/commands`, {
    method: "PUT", // 전체 덮어쓰기 — 이름 겹치는 예전 정의가 안 남음
    headers: { Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify([command]),
  });
  const text = await resp.text();
  return new Response(`${resp.status} ${text}`, { status: 200 });
}
