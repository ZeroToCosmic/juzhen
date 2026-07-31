function randomInt(min, max, random = Math.random) {
  const value = Math.floor(random() * (max - min + 1)) + min;
  return Math.min(max, Math.max(min, value));
}

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function randomInRange([min, max], random = Math.random) {
  return randomInt(min, max, random);
}

async function humanClick(page, x, y, options = {}) {
  const random = options.random || Math.random;
  const sleep = options.sleep || defaultSleep;
  const offsetX = randomInt(-5, 5, random);
  const offsetY = randomInt(-5, 5, random);
  const waitMs = randomInt(50, 150, random);

  await page.mouse.move(x + offsetX, y + offsetY);
  await sleep(waitMs);
  await page.mouse.down();
  await page.mouse.up();
}

async function humanType(page, text, options = {}) {
  const random = options.random || Math.random;
  const sleep = options.sleep || defaultSleep;

  for (const char of String(text)) {
    await page.keyboard.type(char);
    await sleep(randomInt(50, 250, random));
  }
}

async function humanScroll(page, options = {}) {
  const random = options.random || Math.random;
  const scrollPercent = randomInt(80, 100, random);

  await page.evaluate((percent) => {
    window.scrollBy(0, window.innerHeight * (percent / 100));
  }, scrollPercent);
}

function generateMouseStrategies() {
  return [
    {
      id: "steady_reader",
      label: "Steady reader",
      mouseMoves: 3,
      scrolls: 2,
      moveSteps: [12, 22],
      pauseMs: [300, 900],
      scrollDelta: [260, 520],
    },
    {
      id: "curious_scanner",
      label: "Curious scanner",
      mouseMoves: 5,
      scrolls: 3,
      moveSteps: [8, 18],
      pauseMs: [120, 420],
      scrollDelta: [180, 420],
    },
    {
      id: "slow_reviewer",
      label: "Slow reviewer",
      mouseMoves: 2,
      scrolls: 2,
      moveSteps: [18, 30],
      pauseMs: [700, 1400],
      scrollDelta: [120, 300],
    },
  ];
}

async function applyMouseStrategy(page, strategy, options = {}) {
  const random = options.random || Math.random;
  const sleep = options.sleep || defaultSleep;
  const viewport = typeof page.viewportSize === "function"
    ? page.viewportSize()
    : null;
  const width = viewport?.width || 1280;
  const height = viewport?.height || 720;

  for (let index = 0; index < strategy.mouseMoves; index += 1) {
    const x = randomInt(Math.floor(width * 0.1), Math.floor(width * 0.9), random);
    const y = randomInt(Math.floor(height * 0.25), Math.floor(height * 0.75), random);
    const steps = randomInRange(strategy.moveSteps, random);
    await page.mouse.move(x, y, { steps });
    await sleep(randomInRange(strategy.pauseMs, random));
  }

  for (let index = 0; index < strategy.scrolls; index += 1) {
    const delta = randomInRange(strategy.scrollDelta, random);
    await page.mouse.wheel(0, delta);
    await sleep(randomInRange(strategy.pauseMs, random));
  }
}

module.exports = {
  applyMouseStrategy,
  generateMouseStrategies,
  humanClick,
  humanScroll,
  humanType,
};
