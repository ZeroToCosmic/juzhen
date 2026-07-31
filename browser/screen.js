async function captureScreen(page) {
  const buffer = await page.screenshot({ type: "jpeg", quality: 60 });
  return buffer.toString("base64");
}

module.exports = {
  captureScreen,
};
