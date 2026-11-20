(function (root) {
  "use strict";

  function create(hooks) {
    let busy = false;
    let closed = false;

    async function poll() {
      if (closed || busy || document.hidden) return;
      const projectId = hooks.getProjectId();
      if (!projectId || !hooks.getRevisionId()) return;

      busy = true;
      try {
        const response = await fetch(`/api/agent/projects/${encodeURIComponent(projectId)}/head`);
        if (!response.ok) return;
        const head = await response.json();
        if (closed || projectId !== hooks.getProjectId()) return;
        if (head.revision_id !== hooks.getRevisionId() && hooks.canSync()) {
          await hooks.syncLatest();
        }
      } catch (_error) {
        // A transient backend restart must not interrupt local edits.
      } finally {
        busy = false;
      }
    }

    const timer = setInterval(poll, 3000);
    window.addEventListener("focus", poll);
    window.addEventListener("pagehide", () => {
      closed = true;
      clearInterval(timer);
    }, { once: true });
    return { poll };
  }

  root.SubstarAgentStatus = { create };
})(globalThis);
