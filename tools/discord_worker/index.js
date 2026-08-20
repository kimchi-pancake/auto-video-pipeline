/**
 * Cloudflare Worker — Discord 슬래시 커맨드(/영상) 수신 엔드포인트.
 *
 * 디스코드의 "Interactions Endpoint URL"로 등록해서 씁니다.
 *   /영상 예약 channel topic [date]     — 다음 실행(또는 특정 날짜)에 쓸 주제 예약
 *   /영상 목록 [channel] [검색어]        — 예약된 주제 목록 조회 (검색어로 필터 가능)
 *   /영상 취소 channel [date]           — 예약 취소 (date 생략 시 "다음 실행용" 취소)
 *   /영상 분석 [channel]                — 소재 카테고리별 성과(조회수) 요약
 *   /영상 주제생성 channel count [category] — 성과 데이터 참고해서 주제 N개 생성+예약
 *   /영상 시작                          — daily.yml(전 채널 대본 생성)을 지금 바로 트리거.
 *                                          이미지 준비되면 조립은 자동으로 이어짐
 *   /영상 상태                          — 현재 실행 중인 작업 / 오늘 업로드 수 / 대기열
 *   /영상 로그                          — 최근 워크플로우 실행 기록 링크
 *   /영상 재생성 channel topic          — 그 자리에서 즉시 새로 생성(전체 재생성)
 *   /영상 cta설정 channel position on   — 구독 유도 문구 위치별 on/off
 *
 * 2026-08-01: "/영상 거절"(소프트 승인 대기 중 거절) 기능 제거 — daily.yml에서
 * review_lock 시각까지 매번 최소 3시간40분씩 대기하던 게 GitHub Actions 과금의
 * 실제 원인으로 드러나서, 대기 없이 생성 직후 바로 예약공개를 확정하는 방식으로
 * 바꿨습니다. 취소하고 싶으면 유튜브 스튜디오에서 직접 비공개로 바꿔야 합니다.
 *
 * config/*.json을 GitHub Contents API로 직접 읽고 씁니다. 평소엔 daily.yml이
 * 완전 자동으로 돌지만, 이 큐/설정 파일들에 예약·설정이 있으면 그걸 반영합니다.
 *
 * 필요한 환경변수 (Cloudflare 대시보드 → Worker → Settings → Variables):
 *   DISCORD_PUBLIC_KEY   디스코드 개발자 포털 → General Information → Public Key
 *   GITHUB_TOKEN         repo(Contents: write, Actions: write) 권한의 GitHub PAT
 *   GITHUB_REPO          "kimchi-pancake/auto-video-pipeline" 형태
 *   OCI_TENANCY_OCID     ~/.oci/config의 tenancy
 *   OCI_USER_OCID        ~/.oci/config의 user
 *   OCI_FINGERPRINT      ~/.oci/config의 fingerprint
 *   OCI_PRIVATE_KEY_PEM  ~/.oci/oci_api_key.pem 파일 내용 그대로(PKCS8, "-----BEGIN
 *                        PRIVATE KEY-----" 포함) — Secret으로 등록 권장
 *   OCI_SSH_PUBLIC_KEY   ~/.ssh/oci_a1_key.pub 파일 내용 그대로
 *
 * 슬래시 커맨드 정의를 바꿨으면 GITHUB_TOKEN/DISCORD_APP_ID/DISCORD_BOT_TOKEN을
 * 잠깐 추가하고 /register-command-x7k2m9 를 한 번 호출해서 반영해야 합니다.
 *
 * 매일 정해진 시각 자동 트리거
 * ----------------------------
 * daily.yml 자체의 `schedule: cron`은 GitHub 무료/private 레포 특성상 최대
 * 1시간 가까이 늦게 도는 걸 실측으로 확인했습니다. Cloudflare Worker의 Cron
 * Triggers는 그런 큐 지연이 없어서, 이 워커가 직접 정시에 깨어나
 * workflow_dispatch를 쏘도록 scheduled() 핸들러를 둡니다.
 *
 * 설정: Cloudflare 대시보드 → 이 Worker → Triggers → Cron Triggers →
 * "0 16 * * *" 추가 (UTC 기준 16:00 = KST 01:00, daily.yml의 cron과 동일한
 * "생성 시작" 시각). 2026-08-07: 20:00 KST 업로드 목표까지 시간을 최대한
 * 벌어서 AI 이미지 생성(현재 NVIDIA FLUX — 2026-08-19)이 순서대로 다 그려질
 * 여유를 주려고 07:00(16:00 KST)에서 01:00 KST로 앞당겼습니다. daily.yml의
 * schedule 트리거는 그대로 둬도 되고(둘 다 도는 게 아니라 아래 scheduled()가
 * "이미 실행 중이면 스킵" 가드를 거치므로 안전), 아예 daily.yml에서 schedule:
 * 을 지워서 이 Worker가 유일한 트리거가 되게 해도 됨.
 *
 * 실제 유튜브 "공개" 시각(20:00 KST)은 이 트리거 시각과 별개입니다 —
 * config/config.json의 youtube.schedule_hour/schedule_minute이 그 값을
 * 결정하고, 업로드 시 유튜브 자체 예약공개(publishAt)로 맞춰집니다. 다만
 * "공개"가 아니라 "업로드(비공개+예약) 자체가 20시 전에 반드시 끝나야
 * 한다"는 건 이 트리거 타이밍(01시 시작)과 아래 안전망 cron이 같이 보장합니다.
 *
 * AI 씬 이미지(NVIDIA FLUX.1-dev) 생성 + 조립 트리거
 * ----------------------------------------------------
 * 2026-08-04: daily.yml(대본 생성)과 실제 영상 조립을 분리했습니다. 대본이
 * 저장되는 순간 파이썬 쪽(core/ai_script_generator.py)이 /generate-images-x9k3m2
 * 를 호출해 이 워커에게 씬 이미지 생성을 맡기고, 이 워커는 ctx.waitUntil()로
 * NVIDIA(build.nvidia.com)의 FLUX.1-dev를 4개씩 병렬 호출해
 * assets/pending_images/{run_id}/에 커밋합니다(2026-08-19, Pollinations에서
 * 교체 — 동시요청 처리가 훨씬 안정적임). 그 생성이 끝나면(generateSceneImages
 * 마지막) 이 워커가 곧바로 assemble_daily.yml(TTS+영상 조립+업로드)을
 * 트리거합니다 — 그 시점엔 이미지가 이미 다 있어서 GH Actions가 기다릴 시간이
 * 거의 없습니다. 혹시 이 즉시 트리거가 실패해도(네트워크 오류 등) 놓치지
 * 않도록, 20:00 KST 목표보다 2시간 여유를 두고 18:00 KST에 도는 안전망
 * cron("0 9 * * *")도 같이 등록해야 합니다 — 그때도 큐가 비어 있으면
 * assemble_daily.yml이 조용히 아무 것도 안 하고 끝납니다.
 * IMAGE_GEN_SECRET과 NVIDIA_API_KEY 환경변수(파이썬 쪽 .env와 동일한 값)도
 * 등록해야 합니다.
 *
 * 매일 23:59 KST 채팅 비우기
 * -------------------------
 * 진행바/결과 메시지가 매일 쌓이는 걸 막기 위해, 매일 밤 이 채널의 활성
 * 스레드와 메시지를 전부 지웁니다. Cron Triggers에 "59 14 * * *"(UTC
 * 14:59 = KST 23:59)도 같이 등록해야 합니다. DISCORD_BOT_TOKEN에
 * Manage Messages/Manage Threads 권한이 있어야 동작합니다(그 채널이
 * 있는 서버에서 봇에게 권한 부여 필요).
 *
 * Oracle Cloud Ampere A1 "Always Free" 서버 자리 재시도
 * -----------------------------------------------------
 * scripts/oci_a1_retry.py(로컬 PC 상시 실행 필요)를 대체. 10분마다 깨어나
 * Oracle에 A1.Flex 인스턴스 생성을 시도하고, 자리가 나서 성공하면(혹은
 * 용량 부족이 아닌 진짜 오류가 나면) 디스코드로 알립니다. "자리 없음"
 * 실패는 매번 있는 정상 상황이라 알림을 보내지 않습니다(스팸 방지).
 * 이미 인스턴스가 있으면 아무것도 안 하는 멱등 로직이라 몇 번을 더 돌아도
 * 안전합니다. Cloudflare 무료 티어 Cron Trigger라 추가 비용 없음.
 *
 * 설정: Cloudflare 대시보드 → 이 Worker → Triggers → Cron Triggers → 아래
 * scheduled()의 retryOracleA1 분기 조건과 정확히 같은 cron 표현식(10분
 * 간격) 추가, 위 OCI_* 환경변수 5개 등록(PEM/SSH 키는 Secret으로), 재배포.
 */

const WORKER_BUILD = "2026-08-04-ctxfix-2"; // 배포 확인용 버전 마커 — 대시보드에 이 줄이 안 보이면 옛날 파일을 붙여넣은 것
const BRANCH = "master";
const QUEUE_PATH = "config/topic_queue.json";
const REGISTRY_PATH = "config/video_registry.json";
const CTA_PATH = "config/cta_settings.json";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/register-command-x7k2m9") {
      if (!env.DISCORD_BOT_TOKEN || !env.DISCORD_APP_ID) {
        return new Response("not configured", { status: 404 });
      }
      return registerCommand(env);
    }

    // GitHub Actions가 대본 파싱 직후(TTS 시작 전) 여기로 씬 프롬프트 목록을
    // 던지면, 응답은 바로 주고(ctx.waitUntil로) 백그라운드에서 NVIDIA
    // AI 이미지를 병렬 생성해 레포에 커밋합니다. GitHub Actions 잡 시간과
    // 무관하게(Cloudflare는 대기시간을 과금하지 않음) 도는 게 핵심이라 —
    // 파이프라인은 그동안 TTS를 진행하다가, 합성 직전에 git pull로 준비된
    // 만큼만 가져다 쓰고 나머지는 Pixabay로 폴백합니다(2026-08-03).
    if (url.pathname === "/generate-images-x9k3m2" && request.method === "POST") {
      if (!env.IMAGE_GEN_SECRET || request.headers.get("X-Secret") !== env.IMAGE_GEN_SECRET) {
        return new Response("not configured", { status: 404 });
      }
      if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
        return new Response("not configured", { status: 404 });
      }
      let payload;
      try {
        payload = await request.json();
      } catch {
        return new Response("invalid json", { status: 400 });
      }
      if (!payload.run_id || !Array.isArray(payload.scenes)) {
        return new Response("missing run_id/scenes", { status: 400 });
      }
      ctx.waitUntil(generateSceneImages(env, payload.run_id, payload.scenes));
      return json({ accepted: true, scene_count: payload.scenes.length });
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

  // Cloudflare Cron Trigger가 정시에 이걸 부릅니다. cron 문자열로 어느
  // 트리거인지 구분합니다(대시보드에 "0 16 * * *", "0 9 * * *", "59 14 * * *",
  // "*/10 * * * *" 네 개 다 등록돼있어야 함).
  async scheduled(event, env, ctx) {
    if (event.cron === "59 14 * * *") {
      ctx.waitUntil(purgeChannel(env));
    } else if (event.cron === "*/10 * * * *") {
      ctx.waitUntil(retryOracleA1(env));
    } else if (event.cron === "0 9 * * *") {
      // daily.yml(대본 생성, 01:00 KST 시작)보다 17시간 늦은 안전망(18:00 KST,
      // 20시 목표 2시간 전) — generateSceneImages()가 끝나자마자 부르는 즉시
      // 트리거(빠른 경로)가 실패했을 때만 의미가 있고, 이미 조립이 끝났으면
      // assemble_daily.yml이 큐가 비어 조용히 종료됩니다.
      ctx.waitUntil(triggerAssembleIfIdle(env));
    } else if (event.cron === "0 16 * * *") {
      ctx.waitUntil(triggerDailyIfIdle(env));
    } else {
      // 모르는 cron 문자열이면 아무것도 안 하고 로그만 남깁니다 — 예전엔 이
      // 자리가 "그 외엔 전부 대본 생성 트리거"인 catch-all else였는데, 대시보드에
      // 지웠어야 할 옛날 cron(예: "0 10 * * *")이 남아있으면 그게 매번 대본
      // 생성을 또 트리거해서 하루에 여러 번 중복 생성되는 사고가 있었습니다
      // (2026-08-09 확인 — 디스코드 알림이 이상한 시각에 여러 번 온 원인이었음).
      // 대시보드 Cron Triggers에 실제로 등록된 값과 위 4개 분기가 정확히 일치하는지
      // 반드시 확인하세요.
      console.log(`[scheduled] 알 수 없는 cron("${event.cron}") — 아무 것도 안 함. 대시보드에서 이 트리거를 확인/삭제하세요.`);
    }
  },
};

/** 이미 도는 daily.yml 실행이 없을 때만 새로 트리거합니다(중복 실행 방지). */
async function triggerDailyIfIdle(env) {
  const active = await findActiveDailyRun(env);
  if (active) return; // 이미 돌고 있음 — 새로 안 만듦
  await dispatchWorkflow(env, "daily.yml", {});
}

// ─────────────────────────────────────────
// 채널 비우기 (매일 23:59 KST)
// ─────────────────────────────────────────

const DISCORD_CHANNEL_ID = "1528006650278187122";
const DISCORD_BOT_UA = "DiscordBot (https://github.com/kimchi-pancake/auto-video-pipeline, 1.0)";

/** 이 채널의 활성 스레드를 전부 삭제하고, 남은 메시지를 전부(오래된 것도
 * 하나씩) 지웁니다. DISCORD_BOT_TOKEN에 Manage Messages/Manage Threads
 * 권한이 있어야 합니다 — 없으면 개별 삭제 호출이 403으로 조용히 실패합니다. */
async function purgeChannel(env) {
  const token = env.DISCORD_BOT_TOKEN;
  if (!token) return;
  const headers = { Authorization: `Bot ${token}`, "User-Agent": DISCORD_BOT_UA };

  try {
    const chResp = await fetch(`https://discord.com/api/v10/channels/${DISCORD_CHANNEL_ID}`, { headers });
    const ch = await chResp.json();
    const guildId = ch.guild_id;
    if (guildId) {
      const threadsResp = await fetch(`https://discord.com/api/v10/guilds/${guildId}/threads/active`, { headers });
      const data = await threadsResp.json();
      const threads = (data.threads || []).filter((t) => t.parent_id === DISCORD_CHANNEL_ID);
      for (const t of threads) {
        await fetch(`https://discord.com/api/v10/channels/${t.id}`, { method: "DELETE", headers });
      }
    }
  } catch (e) {
    // 스레드 삭제 실패해도 메시지 삭제는 계속 시도
  }

  for (let i = 0; i < 30; i++) {
    let msgs;
    try {
      const resp = await fetch(`https://discord.com/api/v10/channels/${DISCORD_CHANNEL_ID}/messages?limit=100`, { headers });
      if (!resp.ok) break;
      msgs = await resp.json();
    } catch (e) {
      break;
    }
    if (!msgs || msgs.length === 0) break;

    const ids = msgs.map((m) => m.id);
    let resp;
    if (ids.length === 1) {
      resp = await fetch(`https://discord.com/api/v10/channels/${DISCORD_CHANNEL_ID}/messages/${ids[0]}`, {
        method: "DELETE",
        headers,
      });
    } else {
      resp = await fetch(`https://discord.com/api/v10/channels/${DISCORD_CHANNEL_ID}/messages/bulk-delete`, {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify({ messages: ids }),
      });
    }
    if (resp.status === 429) {
      const body = await resp.json().catch(() => ({}));
      await new Promise((r) => setTimeout(r, ((body.retry_after || 1) + 0.5) * 1000));
      continue;
    }
    if (!resp.ok) break;
    await new Promise((r) => setTimeout(r, 1200));
  }
}

/** in_progress/queued 상태인 daily.yml 실행이 있으면 그 run 객체를, 없으면 null을 반환합니다. */
async function findActiveDailyRun(env) {
  return findActiveRun(env, "daily.yml");
}

/** in_progress/queued 상태인 workflowFile 실행이 있으면 그 run 객체를, 없으면 null을 반환합니다. */
async function findActiveRun(env, workflowFile) {
  try {
    const resp = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflowFile}/runs?per_page=5`,
      { headers: ghHeaders(env) }
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    return (data.workflow_runs || []).find((r) => r.status === "in_progress" || r.status === "queued") || null;
  } catch (e) {
    return null; // 조회 실패해도 트리거는 계속 진행 (fail-open)
  }
}

/** 이미 도는 assemble_daily.yml 실행이 없을 때만 새로 트리거합니다(중복 실행 방지).
 * daily.yml의 cron 트리거보다 몇 시간 늦게 도는 안전망 — generateSceneImages()가
 * 끝나자마자 곧바로 부르는 트리거(아래)가 실패했을 때를 대비합니다(2026-08-04). */
async function triggerAssembleIfIdle(env) {
  const active = await findActiveRun(env, "assemble_daily.yml");
  if (active) return;
  await dispatchWorkflow(env, "assemble_daily.yml", {});
}

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
      case "시작": return await handleStart(env);
      case "상태": return await handleStatus(env);
      case "로그": return await handleLogs(env);
      case "재생성": return await handleRegenerate(env, opts);
      case "cta설정": return await handleCtaSettings(env, opts);
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
// 시작 (daily.yml 즉시 트리거)
// ─────────────────────────────────────────

async function handleStart(env) {
  // cron이 늦게 겹쳐서 두 실행이 동시에 도는 사고를 겪은 적이 있어서
  // (같은 대기 파일을 두 실행이 동시에 집어가는 위험 + 유튜브 토큰 갱신 경합),
  // 이미 도는 게 있으면 새로 트리거하지 않고 그 실행 링크만 알려줍니다.
  const active = await findActiveDailyRun(env);
  if (active) {
    return json({
      type: 4,
      data: { content: `⏳ 이미 실행 중임 — 새로 안 만들고 기존 걸로 안내함.\n${active.html_url}` },
    });
  }

  const ok = await dispatchWorkflow(env, "daily.yml", {});
  if (!ok.success) {
    return json({ type: 4, data: { content: `트리거 실패 (${ok.status}): ${ok.detail}` } });
  }
  return json({
    type: 4,
    data: { content: "📝 전 채널 대본 생성(daily.yml) 지금 시작함. AI 이미지 준비되는 대로 영상 조립(assemble_daily.yml)이 자동으로 이어짐." },
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

/** ghPutFile/encodeBase64는 JSON 텍스트 전용이라 이미지 원본 바이트에는 못 씁니다.
 *  raw ArrayBuffer를 그대로 base64로 인코딩하는 바이너리 전용 버전. */
function encodeBase64Bytes(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

/** encodeBase64Bytes의 반대 — base64 문자열을 raw 바이트(Uint8Array)로 되돌립니다.
 * decodeBase64()는 UTF-8 텍스트 전용이라 이미지처럼 임의 바이너리에는 못 씁니다. */
function decodeBase64Bytes(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

/** JSON.stringify를 거치지 않고 raw 바이너리(ArrayBuffer)를 그대로 커밋합니다. */
async function ghPutBinaryFile(env, path, buffer, message) {
  const apiUrl = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${path}`;
  const resp = await fetch(apiUrl, {
    method: "PUT",
    headers: { ...ghHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify({
      message: `${message} [skip ci]`,
      content: encodeBase64Bytes(buffer),
      branch: BRANCH,
    }),
  });
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`이미지 커밋 실패 (${resp.status}): ${errText.slice(0, 200)}`);
  }
}

// ─────────────────────────────────────────
// AI 이미지 생성 (NVIDIA build.nvidia.com, FLUX.1-dev) — 백그라운드 커밋
// ─────────────────────────────────────────
// 2026-08-19: Pollinations에서 NVIDIA로 교체 — Pollinations는 전역 동시요청
// 제한이 너무 빡빡해서(익명 티어 사실상 1개씩만) 채널당 하루 3개씩 run_id가
// 겹치면 대부분 429로 실패했습니다(실측: 21씬 중 1개만 성공). NVIDIA는 실측
// 테스트에서 8개 동시 요청 중 7개가 성공했고(13초), 장당 4~13초로 훨씬
// 빠릅니다. IMAGE_GEN_SECRET과 별개로 NVIDIA_API_KEY 환경변수(파이썬 쪽
// .env와 같은 값)를 이 Worker에도 등록해야 합니다.

// 2026-08-04: 사용자가 직접 테스트해서 고른 톤 — 실사에 가까운 "AI 티" 나는
// 얼굴 대신 따뜻한 색감의 수채화/스토리북 느낌. 주제(씬 프롬프트) 뒤에
// 붙여야만 정상 동작합니다 — 이 문구를 주제보다 앞에 놓으면 이미지 생성 모델이
// 주제 자체를 무시하고 엉뚱한 그림을 그리는 게 실측으로 확인됐습니다.
const _IMAGE_STYLE_SUFFIX =
  ", warm watercolor illustration, soft muted color palette, gentle golden hour lighting, " +
  "visible paper texture, loose expressive brushstrokes, soft bleeding edges between colors, " +
  "warm beige and amber undertones, cozy nostalgic atmosphere, hand-painted storybook feel, " +
  "soft focus background, no harsh outlines, emotionally warm and comforting mood, " +
  "traditional watercolor paper grain, delicate color washes";

// 사람이 나오는 씬에서만 얼굴/손 관련 보정 문구를 추가합니다 — 이 문구를
// 사람이 없는 씬(사과, 바나나, 풍경 등)에도 똑같이 넣었더니 주제를 무시하고
// 매번 여자 얼굴 클로즈업을 그려버리는 문제가 실측으로 확인돼서, 씬 프롬프트에
// 사람을 가리키는 단어가 있을 때만 조건부로 붙입니다(2026-08-04).
const _PERSON_KEYWORDS =
  /\b(woman|women|man|men|person|people|elderly|grandmother|grandma|grandfather|grandpa|doctor|patient|nurse|child|children|boy|girl|family|couple|human|face|portrait|lady|gentleman|senior|adult)\b/i;

const _FACE_QUALITY_BOOST =
  ", detailed symmetrical face, sharp clear eyes, correct facial anatomy, close-up portrait, " +
  "headshot framing, hands not visible, wearing modest crew-neck clothing, fully clothed, covered shoulders";

// NVIDIA FLUX.1-dev는 width/height가 임의값이 아니라 정해진 값만 허용합니다
// (실측 확인: 768/832/896/960/1024/1088/1152/1216/1280/1344). 쇼츠(1080x1920,
// 9:16)에 가장 가까운 조합으로 골랐습니다.
const _NVIDIA_IMAGE_WIDTH = 768;
const _NVIDIA_IMAGE_HEIGHT = 1344;
const _NVIDIA_IMAGE_MODEL = "black-forest-labs/flux.1-dev";

// NVIDIA는 Pollinations와 달리 동시요청을 잘 버팁니다(실측: 8개 동시 요청 중
// 7개 성공). 그래도 완전 무제한은 아니라서(가끔 429), 적당한 동시 개수로
// 배치 처리하고 429/일시 오류는 짧게 재시도합니다(2026-08-19).
const _IMAGE_CONCURRENCY = 4;
const _IMAGE_MAX_RETRIES = 3;
const _IMAGE_RETRY_BASE_MS = 5000; // 5s, 10s, 20s

async function _fetchNvidiaImage(env, prompt, seed) {
  for (let attempt = 0; attempt <= _IMAGE_MAX_RETRIES; attempt++) {
    const resp = await fetch(`https://ai.api.nvidia.com/v1/genai/${_NVIDIA_IMAGE_MODEL}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.NVIDIA_API_KEY}`,
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        prompt,
        width: _NVIDIA_IMAGE_WIDTH,
        height: _NVIDIA_IMAGE_HEIGHT,
        steps: 25,
        seed,
      }),
    });
    if (resp.ok) return resp;
    if (attempt === _IMAGE_MAX_RETRIES) return resp;
    const wait = _IMAGE_RETRY_BASE_MS * Math.pow(2, attempt);
    console.log(`[image-gen] ${resp.status} 응답 — ${Math.round(wait / 1000)}초 뒤 재시도 (${attempt + 1}/${_IMAGE_MAX_RETRIES})`);
    await new Promise((r) => setTimeout(r, wait));
  }
}

async function _generateOneScene(env, runId, scene) {
  try {
    const faceBoost = _PERSON_KEYWORDS.test(scene.prompt) ? _FACE_QUALITY_BOOST : "";
    const prompt = `${scene.prompt}${faceBoost}${_IMAGE_STYLE_SUFFIX}`;
    const seed = Math.floor(Math.random() * 1e9);
    const resp = await _fetchNvidiaImage(env, prompt, seed);
    if (!resp.ok) {
      console.log(`[image-gen] scene ${scene.index} 최종 실패 (${resp.status})`);
      return;
    }
    const body = await resp.json();
    const b64 = body.artifacts && body.artifacts[0] && body.artifacts[0].base64;
    if (!b64) {
      console.log(`[image-gen] scene ${scene.index} 실패 — 응답에 이미지 없음`);
      return;
    }
    const buffer = decodeBase64Bytes(b64);
    const path = `assets/pending_images/${runId}/scene_${String(scene.index).padStart(4, "0")}.jpg`;
    await ghPutBinaryFile(env, path, buffer, `AI image scene ${scene.index} (${runId})`);
    console.log(`[image-gen] scene ${scene.index} 완료`);
  } catch (e) {
    console.log(`[image-gen] scene ${scene.index} 에러: ${e.message}`);
  }
}

/**
 * run_id에 속한 씬들을 NVIDIA(FLUX.1-dev)로 그려서
 * assets/pending_images/{run_id}/scene_{index:04d}.jpg 로 커밋합니다.
 * ctx.waitUntil()로 호출되므로 GitHub Actions 잡 시간과는 무관하게 돕니다.
 * _IMAGE_CONCURRENCY개씩 배치로 병렬 처리합니다(순차 처리했던 Pollinations
 * 시절과 달리 NVIDIA는 동시요청을 잘 버팀 — 2026-08-19).
 */
async function generateSceneImages(env, runId, scenes) {
  for (let i = 0; i < scenes.length; i += _IMAGE_CONCURRENCY) {
    const batch = scenes.slice(i, i + _IMAGE_CONCURRENCY);
    await Promise.all(batch.map((scene) => _generateOneScene(env, runId, scene)));
  }

  // 대본 하나(run_id 하나)의 이미지가 다 끝날 때마다 곧바로 조립 워크플로우를
  // 깨워봅니다 — 채널당 하루 3편이라 이 함수가 여러 번 불리는데, idle-guard
  // (findActiveRun) 덕분에 이미 조립이 돌고 있으면 그냥 스킵되고, 마지막
  // run_id가 끝났을 때 비로소 실제로 새로 트리거됩니다(2026-08-04).
  try {
    await triggerAssembleIfIdle(env);
  } catch (e) {
    console.log(`[image-gen] assemble 트리거 실패(무시, 안전망 cron이 나중에 처리함): ${e.message}`);
  }
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
      "혈관혈압", "당뇨관리", "관절근육", "장건강", "수면건강",
      "치매인지", "눈건강", "뼈건강", "면역력", "체중대사", "심장건강", "약물주의", "계절건강", "스트레스",
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
      { name: "시작", description: "전 채널 정기 생성(daily.yml)을 지금 바로 시작합니다", type: 1, options: [] },
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

// ─────────────────────────────────────────
// Oracle Cloud Always Free Ampere A1 재시도 봇
// ─────────────────────────────────────────
// scripts/oci_a1_retry.py(로컬 Windows 스케줄러용)를 Cloudflare Worker로
// 이식한 버전. 노트북을 안 켜놔도 되고, GitHub Actions처럼 분 단위로
// 과금되지 않습니다(Cloudflare Workers Cron Trigger는 무료 티어에 포함).
//
// OCI REST API는 Signature Version 1(RSA-SHA256 기반 커스텀 서명)을 씁니다.
// Python oci SDK가 내부적으로 하는 걸 Web Crypto API로 직접 구현했습니다 —
// 2026-07-28에 로컬 Node.js로 실제 API(ListInstances/LaunchInstance) 호출까지
// 검증 완료(GET 200, POST가 "Out of host capacity"로 정상 실패 = 서명 정확).
//
// 필요한 환경변수(Cloudflare 대시보드 → Worker → Settings → Variables):
//   OCI_USER_OCID, OCI_FINGERPRINT, OCI_TENANCY_OCID, OCI_PRIVATE_KEY_PEM,
//   OCI_SSH_PUBLIC_KEY
// Cron Trigger "*/10 * * * *" (10분마다)도 등록해야 합니다. GitHub Actions와
// 달리 Cloudflare Cron Trigger는 호출 빈도로 과금되지 않으므로(무료 플랜
// 하루 10만 요청 한도 안에서 자유), 자리 잡을 확률을 위해 짧은 간격으로 둡니다.

const OCI_REGION = "ap-osaka-1";
const OCI_HOST = `iaas.${OCI_REGION}.oraclecloud.com`;
const OCI_AVAILABILITY_DOMAIN = "lYdr:AP-OSAKA-1-AD-1";
const OCI_SUBNET_ID = "ocid1.subnet.oc1.ap-osaka-1.aaaaaaaagpflof3hvkkrzhsnozrbz3fbr3ffy3qnaen3ggidluxxrux3ovoa";
const OCI_IMAGE_ID = "ocid1.image.oc1.ap-osaka-1.aaaaaaaacxblapqsiodvwbiyhnuzv4edk5uw23boofijdlit2xtgz3f3h6eq";
const OCI_INSTANCE_NAME = "auto-video-pipeline-a1";
const OCI_OCPUS = 2.0;
const OCI_MEMORY_GBS = 12.0;

function _pemToArrayBuffer(pem) {
  const b64 = pem
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\s+/g, "");
  const raw = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  return raw.buffer;
}

async function _importOciPrivateKey(pem) {
  return crypto.subtle.importKey(
    "pkcs8",
    _pemToArrayBuffer(pem),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );
}

function _base64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

async function _sha256Base64(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return _base64(digest);
}

/** OCI Signature v1 헤더를 만듭니다. method는 "GET" 또는 "POST". */
async function _signOciRequest(method, path, bodyText, privateKey, env) {
  const date = new Date().toUTCString();
  const requestTarget = `${method.toLowerCase()} ${path}`;
  const keyId = `${env.OCI_TENANCY_OCID}/${env.OCI_USER_OCID}/${env.OCI_FINGERPRINT}`;

  let headersToSign, signingLines, extraHeaders;
  if (method === "GET") {
    headersToSign = "date (request-target) host";
    signingLines = [`date: ${date}`, `(request-target): ${requestTarget}`, `host: ${OCI_HOST}`];
    extraHeaders = {};
  } else {
    const contentSha256 = await _sha256Base64(bodyText);
    const contentLength = String(new TextEncoder().encode(bodyText).length);
    headersToSign = "date (request-target) host content-length content-type x-content-sha256";
    signingLines = [
      `date: ${date}`,
      `(request-target): ${requestTarget}`,
      `host: ${OCI_HOST}`,
      `content-length: ${contentLength}`,
      `content-type: application/json`,
      `x-content-sha256: ${contentSha256}`,
    ];
    extraHeaders = {
      "content-length": contentLength,
      "content-type": "application/json",
      "x-content-sha256": contentSha256,
    };
  }

  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    privateKey,
    new TextEncoder().encode(signingLines.join("\n"))
  );

  return {
    Date: date,
    Authorization:
      `Signature version="1",headers="${headersToSign}",keyId="${keyId}",` +
      `algorithm="rsa-sha256",signature="${_base64(signature)}"`,
    ...extraHeaders,
  };
}

async function _notifyOracleDiscord(env, message) {
  try {
    await fetch(`https://discord.com/api/v10/channels/${DISCORD_CHANNEL_ID}/messages`, {
      method: "POST",
      headers: { Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`, "Content-Type": "application/json", "User-Agent": DISCORD_BOT_UA },
      body: JSON.stringify({ content: message }),
    });
  } catch {
    // 알림 실패는 무시 — 재시도 로직 자체는 계속 돼야 함
  }
}

/** OCI 응답 본문을 안전하게 파싱합니다. JSON이 아니거나 비어있어도 절대 던지지 않고,
 * 항상 { parsed, raw } 형태로 돌려줍니다 — 알림 문구에 "undefined — undefined"처럼
 * 의미 없는 텍스트가 찍히는 걸 막기 위해, 실패하면 원문(raw)을 그대로 보존합니다. */
async function _safeReadJson(resp) {
  const raw = await resp.text().catch(() => "");
  try {
    return { parsed: raw ? JSON.parse(raw) : {}, raw };
  } catch {
    return { parsed: {}, raw };
  }
}

/** 인스턴스 생성을 한 번 시도합니다. 10분마다 Cron Trigger가 이걸 부릅니다.
 * OCI 응답 형태가 예상과 다르거나 서명/키 문제로 예외가 나도 조용히 사라지지
 * 않도록 전체를 try/catch로 감싸고, 실패하면 디스코드로 알립니다 — 이 크론은
 * ctx.waitUntil() 안에서 도는 거라 이 알림이 유일한 관찰 창구입니다. */
async function retryOracleA1(env) {
  try {
    await _retryOracleA1Inner(env);
  } catch (e) {
    await _notifyOracleDiscord(env, `⚠️ Ampere A1 재시도 중 예외 발생: ${e && e.message ? e.message : String(e)}`);
  }
}

async function _retryOracleA1Inner(env) {
  const privateKey = await _importOciPrivateKey(env.OCI_PRIVATE_KEY_PEM);
  const compartmentId = env.OCI_TENANCY_OCID;

  // 1) 이미 있는지 확인 (멱등 — 있으면 아무것도 안 하고 끝)
  const listPath = `/20160918/instances?compartmentId=${encodeURIComponent(compartmentId)}&displayName=${OCI_INSTANCE_NAME}`;
  const listHeaders = await _signOciRequest("GET", listPath, "", privateKey, env);
  const listResp = await fetch(`https://${OCI_HOST}${listPath}`, { headers: listHeaders });
  if (listResp.ok) {
    const { parsed: instances } = await _safeReadJson(listResp);
    if (Array.isArray(instances)) {
      const active = instances.find((i) => !["TERMINATED", "TERMINATING"].includes(i.lifecycleState));
      if (active) return; // 이미 확보됨 — 재시도 불필요
    }
    // 응답이 배열이 아니면(예상과 다른 형태) 존재 확인을 건너뛰고 그냥 생성을
    // 시도합니다 — 어차피 이미 인스턴스가 있으면 아래 POST가 자연스럽게
    // 실패(진짜 오류)로 알림이 가서 눈에 띕니다.
  }

  // 2) 생성 시도
  const body = JSON.stringify({
    compartmentId,
    availabilityDomain: OCI_AVAILABILITY_DOMAIN,
    shape: "VM.Standard.A1.Flex",
    shapeConfig: { ocpus: OCI_OCPUS, memoryInGBs: OCI_MEMORY_GBS },
    displayName: OCI_INSTANCE_NAME,
    createVnicDetails: { subnetId: OCI_SUBNET_ID, assignPublicIp: true },
    sourceDetails: { sourceType: "image", imageId: OCI_IMAGE_ID },
    metadata: { ssh_authorized_keys: env.OCI_SSH_PUBLIC_KEY },
  });
  const launchPath = "/20160918/instances";
  const launchHeaders = await _signOciRequest("POST", launchPath, body, privateKey, env);
  const launchResp = await fetch(`https://${OCI_HOST}${launchPath}`, { method: "POST", headers: launchHeaders, body });

  if (launchResp.ok) {
    const { parsed: data, raw } = await _safeReadJson(launchResp);
    const instanceId = data && data.id ? data.id : `(응답 파싱 실패, 원문: ${raw.slice(0, 200)})`;
    await _notifyOracleDiscord(env, `🎉 잡았다! Ampere A1 서버 확보 성공! instance_id=${instanceId}`);
    return;
  }

  const { parsed: errBody, raw: errRaw } = await _safeReadJson(launchResp);
  const capacityErrors = ["LimitExceeded", "InternalError", "OutOfCapacity", "TooManyRequests"];
  if (capacityErrors.includes(errBody.code) || /capacity/i.test(errBody.message || "")) {
    return; // 자리 없음 — 다음 스케줄에 재시도, 알림 안 보냄(스팸 방지)
  }
  // 용량 문제가 아닌 진짜 오류만 알림 — code/message가 없으면(형태가 다른 응답)
  // 원문(errRaw)을 그대로 보여줘서 "undefined — undefined" 같은 무의미한 알림을 막는다.
  const errText = errBody.code || errBody.message
    ? `${errBody.code} — ${errBody.message}`
    : `HTTP ${launchResp.status}, 원문: ${errRaw.slice(0, 300)}`;
  await _notifyOracleDiscord(env, `⚠️ Ampere A1 재시도 중 예상 못한 오류: ${errText}`);
}
