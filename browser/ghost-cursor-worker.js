const readline = require("node:readline");
const { path } = require("ghost-cursor");

function finitePoint(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  if (!Number.isFinite(value.x)) {
    throw new Error(`${label}.x must be a finite number`);
  }
  if (!Number.isFinite(value.y)) {
    throw new Error(`${label}.y must be a finite number`);
  }
  return { x: value.x, y: value.y };
}

function finiteTarget(value) {
  const target = finitePoint(value, "target");
  if (!Number.isFinite(value.width) || value.width <= 0) {
    throw new Error("target.width must be a positive finite number");
  }
  if (!Number.isFinite(value.height) || value.height <= 0) {
    throw new Error("target.height must be a positive finite number");
  }
  return { ...target, width: value.width, height: value.height };
}

function generatePath(request, pathFunction = path) {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new Error("request must be an object");
  }
  if (typeof request.id !== "string" || request.id.trim() === "") {
    throw new Error("id must be a non-empty string");
  }

  const start = finitePoint(request.start, "start");
  const end = finitePoint(request.end, "end");
  let destination = end;
  if (request.target !== undefined) {
    const target = finiteTarget(request.target);
    destination = {
      x: end.x,
      y: end.y,
      width: target.width,
      height: target.height,
    };
  }
  const points = pathFunction(start, destination);

  if (!Array.isArray(points) || points.length < 2) {
    throw new Error("generated path must contain at least two points");
  }

  return {
    id: request.id,
    points: points.map((point, index) => finitePoint(point, `point ${index + 1}`)),
  };
}

function responseId(request) {
  return request && typeof request.id === "string" ? request.id : null;
}

function startWorker(input = process.stdin, output = process.stdout) {
  const lines = readline.createInterface({ input, crlfDelay: Infinity });
  lines.on("line", (line) => {
    let request;
    try {
      request = JSON.parse(line);
      output.write(`${JSON.stringify(generatePath(request))}\n`);
    } catch {
      output.write(`${JSON.stringify({ id: responseId(request), error: "Invalid path request" })}\n`);
    }
  });
  return lines;
}

if (require.main === module) {
  startWorker();
}

module.exports = { finitePoint, generatePath, startWorker };
