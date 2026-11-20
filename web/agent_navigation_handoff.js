(() => {
  "use strict";

  const { project, navigationId } = document.querySelector("main").dataset;
  const status = document.getElementById("navigationStatus");
  const editor = document.getElementById("openEditor");

  function openEditor() {
    location.replace(editor.href);
  }

  if (typeof BroadcastChannel !== "function") {
    openEditor();
    return;
  }

  const channel = new BroadcastChannel("substar:cue-navigation:" + project);
  let editorFound = false;
  let editorFailed = false;
  const probe = () => channel.postMessage({ type: "probe", navigationId });
  const interval = setInterval(probe, 350);
  const discoveryTimeout = setTimeout(() => {
    if (!editorFound) {
      stop();
      openEditor();
      return;
    }
    status.textContent = "已发送定位，正在等待已打开的编辑器确认。";
  }, 2200);
  const acknowledgementTimeout = setTimeout(() => {
    stop();
    status.textContent = editorFailed
      ? "已打开的编辑器未能定位这条字幕。"
      : "已打开的编辑器尚未确认定位。";
  }, 15000);

  function stop() {
    clearInterval(interval);
    clearTimeout(discoveryTimeout);
    clearTimeout(acknowledgementTimeout);
    channel.close();
  }

  channel.onmessage = ({ data }) => {
    if (data?.type !== "editor" || data.navigationId !== navigationId) return;
    editorFound = true;
    if (data.status === "failed") editorFailed = true;
    if (data.status !== "located") return;

    stop();
    status.textContent = "已在原编辑器定位，可以关闭此标签页。";
    window.close();
  };
  window.addEventListener("pagehide", stop, { once: true });
  probe();
})();
