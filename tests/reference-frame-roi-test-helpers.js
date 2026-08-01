const {
  applyReferenceTime,
  candidate,
  candidateSet,
  createHarness,
  findElement,
  submit,
} = require("./reference-frame-ui-harness.js");

async function activeRoiHarness(offset = 0) {
  const harness = createHarness(async () => ({
    ok: true,
    json: async () => candidateSet([candidate(offset)]),
  }));
  applyReferenceTime(harness);
  await submit(harness);
  const card = harness.results.children[0];
  const thumbnail = card.children[0].children[0];
  const radio = findElement(card, "input");
  thumbnail.listeners.load();
  radio.checked = true;
  radio.listeners.change();
  harness.previewImage.rect = { left: 10, top: 20, width: 200, height: 160 };
  harness.previewImage.complete = true;
  harness.previewImage.listeners.load();
  return harness;
}

function pointerEvent(pointerId, pointerType, clientX, clientY, button = 0) {
  return {
    pointerId,
    pointerType,
    button,
    clientX,
    clientY,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
  };
}

function emit(harness, name, event) {
  harness.roiStage.listeners[name](event);
  return event;
}

function drag(harness, start, end, pointerType = "mouse", pointerId = 1) {
  emit(harness, "pointerdown", pointerEvent(pointerId, pointerType, start[0], start[1]));
  emit(harness, "pointermove", pointerEvent(pointerId, pointerType, end[0], end[1]));
  emit(harness, "pointerup", pointerEvent(pointerId, pointerType, end[0], end[1]));
}

module.exports = { activeRoiHarness, drag, emit, pointerEvent };
