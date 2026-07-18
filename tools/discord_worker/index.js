/**
 * Cloudflare Worker — Discord 슬래시 커맨드(/영상) 수신 엔드포인트.
 *
 * 디스코드의 "Interactions Endpoint URL"로 등록해서 씁니다. 슬래시 커맨드가
 * 호출되면 서명을 검증하고, config/topic_queue.json에 {channel: topic}을
 * 기록합니다(GitHub Contents API로 직접 커밋). 평소엔 daily.yml이 완전
 * 자동(주제 랜덤 선택)으로 돌지만, 다음 정기 실행 때 이 큐에 해당 채널의
 * 예약이 있으면 그 주제를 대신 씁니다 — 매번 즉석 생성이 아니라 "다음 영상은
 * 이 주제로" 예약하는 방식입니다.
 *
 * 필요한 환경변수 (Cloudflare 대시보드 → Worker → Settings → Variables):
 *   DISCORD_PUBLIC_KEY   디스코드 개발자 포털 → General Information → Public Key
 *   GITHUB_TOKEN         repo(Contents: write) 권한의 GitHub 개인 액세스 토큰
 *   GITHUB_REPO          "kimchi-pancake/auto-video-pipeline" 형태
 *
 * 배포: Cloudflare 대시보드에서 Worker 만들고 이 파일 내용을 그대로 붙여넣기.
 */

const QUEUE_PATH = "config/topic_queue.json";
const BRANCH = "master";

export default {
  async fetch(request, env) {
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

    // APPLICATION_COMMAND
    if (interaction.type === 2) {
      const options = interaction.data.options || [];
      const channel = options.find((o) => o.name === "channel")?.value;
      const topic = options.find((o) => o.name === "topic")?.value;

      if (!channel || !topic) {
        return json({ type: 4, data: { content: "채널과 주제를 둘 다 입력해줘." } });
      }

      try {
        await queueTopic(env, channel, topic);
      } catch (e) {
        return json({
          type: 4,
          data: { content: `주제 예약 실패: ${String(e).slice(0, 300)}` },
        });
      }

      return json({
        type: 4,
        data: {
          content: `📌 [${channel}] 다음 정기 생성(매일 20:10 KST) 때 "${topic}" 주제로 만들게. 그때 알림 올 거임.`,
        },
      });
    }

    return new Response("unknown interaction type", { status: 400 });
  },
};

async function queueTopic(env, channel, topic) {
  const apiUrl = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${QUEUE_PATH}`;
  const headers = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "auto-video-pipeline-discord-bot",
  };

  let queue = {};
  let sha;
  const getResp = await fetch(`${apiUrl}?ref=${BRANCH}`, { headers });
  if (getResp.status === 200) {
    const data = await getResp.json();
    sha = data.sha;
    queue = JSON.parse(decodeBase64(data.content));
  } else if (getResp.status !== 404) {
    throw new Error(`파일 조회 실패 (${getResp.status})`);
  }

  queue[channel] = topic;

  const putResp = await fetch(apiUrl, {
    method: "PUT",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({
      message: `queue topic for ${channel} via discord bot [skip ci]`,
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
