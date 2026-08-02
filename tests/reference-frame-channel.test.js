const assert = require("node:assert/strict");
const test = require("node:test");
const { applyReferenceTime, createHarness, deferred, submit } = require("./reference-frame-ui-harness.js");

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
}

function response(channels, defaultChannelId = null) {
  return {
    ok: true,
    json: async () => ({ channels, default_channel_id: defaultChannelId }),
  };
}

function channel(channelId, name = `Channel ${channelId}`, online = true) {
  return { channel_id: channelId, name, alias: name, online };
}

test("page initialization visibly selects online channel 1", async () => {
  const harness = createHarness(async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }));

  await settle();

  assert.equal(harness.channel.tagName, "select");
  assert.equal(harness.channel.value, "1");
  assert.equal(harness.channel.disabled, false);
  assert.deepEqual(harness.channel.children.map((option) => option.value), ["1"]);
});

test("page initialization chooses the smallest usable channel from an unsorted response", async () => {
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    response([channel(7), channel(3)], 3),
  );

  await settle();

  assert.equal(harness.channel.value, "3");
  assert.deepEqual(harness.channel.children.map((option) => option.value), ["7", "3"]);
});

test("offline and invalid channels are excluded from the visible control", async () => {
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    response([channel(0, "Invalid"), channel(-1, "Negative"), channel(2, "Offline", false), channel(4)]),
  );

  await settle();

  assert.equal(harness.channel.value, "4");
  assert.deepEqual(harness.channel.children.map((option) => option.value), ["4"]);
});

test("empty channel discovery leaves no fabricated selection", async () => {
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    response([], null),
  );

  await settle();

  assert.equal(harness.channel.value, "");
  assert.equal(harness.channel.disabled, true);
  assert.equal(harness.channel.children.length, 0);
  assert.equal(harness.channelStatus.textContent, "사용 가능한 온라인 채널이 없습니다.");
});

test("channel discovery failure is safe and does not expose response details", async () => {
  const marker = "rtsp://user:password@nvr.example/private";
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    { ok: false, json: async () => ({ error: marker }) },
  );

  await settle();

  assert.equal(harness.channel.value, "");
  assert.equal(harness.channel.disabled, true);
  assert.equal(harness.channelStatus.textContent, "채널 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요.");
  assert.doesNotMatch(harness.channelStatus.textContent, /rtsp|password|nvr\.example/);
});

test("explicit channel selection survives a later refresh while still available", async () => {
  const refreshResponse = deferred();
  let callCount = 0;
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    () => {
      callCount += 1;
      return callCount === 1
        ? Promise.resolve(response([channel(1), channel(7)], 1))
        : refreshResponse.promise;
    },
  );

  await settle();
  const refresh = harness.window.vigiVisionReferenceFrameChannels.refresh();
  harness.channel.value = "7";
  harness.channel.listeners.change();
  refreshResponse.resolve(response([channel(3), channel(7), channel(1)], 1));
  await refresh;

  assert.equal(harness.channel.value, "7");
});

test("a selected channel that disappears falls back to the API default", async () => {
  const refreshResponse = deferred();
  let callCount = 0;
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    () => {
      callCount += 1;
      return callCount === 1
        ? Promise.resolve(response([channel(1), channel(7)], 1))
        : refreshResponse.promise;
    },
  );

  await settle();
  harness.channel.value = "7";
  harness.channel.listeners.change();
  const refresh = harness.window.vigiVisionReferenceFrameChannels.refresh();
  refreshResponse.resolve(response([channel(3), channel(9)], 3));
  await refresh;

  assert.equal(harness.channel.value, "3");
});

test("selected channel is used by the reference-frame candidate request", async () => {
  let requestBody;
  const harness = createHarness(
    async (_url, options) => {
      requestBody = JSON.parse(options.body);
      return { ok: true, json: async () => ({ candidates: [], summary: {} }) };
    },
    response([channel(4, "Dining")], 4),
  );

  await settle();
  applyReferenceTime(harness);
  await submit(harness);

  assert.equal(requestBody.channel_id, 4);
});

test("channel labels decode encoded metadata into the requested format", async () => {
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    response([{ channel_id: 1, name: "VIGI%20C240", alias: "%EC%B9%B4%EC%9A%B4%ED%84%B0", online: true }], 1),
  );

  await settle();

  assert.equal(harness.channel.children[0].textContent, "채널 1 - VIGI C240 - 카운터");
  assert.equal(harness.channel.children[0].value, "1");
});

test("missing and duplicate channel metadata do not create empty separators", async () => {
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    response([
      { channel_id: 1, name: "VIGI%20C240", alias: "", online: true },
      { channel_id: 2, name: "", alias: "%EC%9E%85%EA%B5%AC", online: true },
      { channel_id: 3, name: "VIGI%20C240", alias: "VIGI C240", online: true },
    ], 1),
  );

  await settle();

  assert.deepEqual(
    harness.channel.children.map((option) => option.textContent),
    ["채널 1 - VIGI C240", "채널 2 - 입구", "채널 3 - VIGI C240"],
  );
});

test("malformed encoded metadata remains safe during channel initialization", async () => {
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    response([{ channel_id: 1, name: "VIGI%ZZ", alias: null, online: true }], 1),
  );

  await settle();

  assert.equal(harness.channel.children[0].textContent, "채널 1 - VIGI%ZZ");
  assert.equal(harness.channel.value, "1");
});

test("channel options stay separate from the timezone control", async () => {
  const harness = createHarness(
    async () => ({ ok: true, json: async () => ({ candidates: [], summary: {} }) }),
    response([{ channel_id: 1, name: "Counter", alias: "카운터", online: true }], 1),
  );

  await settle();

  assert.notEqual(harness.channel, harness.timezone);
  assert.deepEqual(harness.timezone.children.map((option) => option.value), ["Asia/Seoul", "UTC"]);
  assert.deepEqual(harness.channel.children.map((option) => option.value), ["1"]);
  assert.doesNotMatch(harness.channel.children[0].textContent, /Asia\/Seoul|UTC/);
});
