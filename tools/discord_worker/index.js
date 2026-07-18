/**
 * Cloudflare Worker — Discord 슬래시 커맨드(/영상) 수신 엔드포인트.
 *
 * 디스코드의 "Interactions Endpoint URL"로 등록해서 씁니다. 슬래시 커맨드가
 * 호출되면 서명을 검증하고, GitHub Actions의 on_demand.yml 워크플로우를
 * workflow_dispatch로 트리거합니다 (channel, topic을 입력값으로 전달).
 *
 * 필요한 환경변수 (Cloudflare 대시보드 → Worker → Settings → Variables):
 *   DISCORD_PUBLIC_KEY   디스코드 개발자 포털 → General Information → Public Key
 *   GITHUB_TOKEN         repo(Actions: write) 권한의 GitHub 개인 액세스 토큰
 *   GITHUB_REPO          "kimchi-pancake/auto-video-pipeline" 형태
 *
 * 배포: Cloudflare 대시보드에서 Worker 만들고 이 파일 내용을 그대로 붙여넣기.
 */

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

      const ghResp = await fetch(
        `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/on_demand.yml/dispatches`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${env.GITHUB_TOKEN}`,
            Accept: "application/vnd.github+json",
            "User-Agent": "auto-video-pipeline-discord-bot",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ ref: "master", inputs: { channel, topic } }),
        }
      );

      if (!ghResp.ok) {
        const errText = await ghResp.text();
        return json({
          type: 4,
          data: { content: `깃허브 액션 트리거 실패 (${ghResp.status}): ${errText.slice(0, 300)}` },
        });
      }

      return json({
        type: 4,
        data: {
          content: `🎬 [${channel}] "${topic}" 주제로 영상 생성 시작! 완료되면 디스코드로 알림 올 거임 (몇 분~몇십 분 걸림).`,
        },
      });
    }

    return new Response("unknown interaction type", { status: 400 });
  },
};

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
