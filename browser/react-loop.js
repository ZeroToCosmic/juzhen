const { humanClick, humanScroll, humanType } = require("./actions");
const { askAgentBrain: defaultAskAgentBrain } = require("./agent-brain");
const { captureScreen: defaultCaptureScreen } = require("./screen");

const defaultActions = {
  humanClick,
  humanScroll,
  humanType,
};

async function runReActLoop({
  page,
  taskGoal,
  maxSteps = 10,
  captureScreen = defaultCaptureScreen,
  askAgentBrain = defaultAskAgentBrain,
  actions = defaultActions,
  logger = console,
}) {
  let lastDecision;

  for (let step = 1; step <= maxSteps; step += 1) {
    const base64Image = await captureScreen(page);
    const decision = await askAgentBrain(base64Image, taskGoal);
    lastDecision = decision;

    if (typeof logger.log === "function") {
      logger.log(`Step ${step}: ${decision.action}`);
    }

    if (decision.action === "success" || decision.action === "failed") {
      return { status: decision.action, steps: step, decision };
    }

    switch (decision.action) {
      case "click":
        await actions.humanClick(page, decision.x, decision.y);
        break;
      case "type":
        await actions.humanType(page, decision.text);
        break;
      case "scroll":
        await actions.humanScroll(page);
        break;
      default:
        throw new Error(`Unsupported action: ${decision.action}`);
    }
  }

  return { status: "max_steps", steps: maxSteps, decision: lastDecision };
}

module.exports = {
  runReActLoop,
};
