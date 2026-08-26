(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorOperationQueue = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function createOperationQueue(options) {
    if (typeof options?.sendBatch !== "function") throw new Error("sendBatch is required");
    const debounceMs = Math.max(0, Number(options.debounceMs ?? 60));
    const maxBatchSize = Math.max(1, Number(options.maxBatchSize ?? 100));
    const maxRetries = Math.max(0, Number(options.maxRetries ?? 3));
    const pending = [];
    const failed = [];
    let inFlight = null;
    let timer = null;
    let sequence = 0;
    let destroyed = false;

    function inertEntry(operation) {
      return {operation, resolve() {}, reject() {}, attempts:0};
    }

    (options.initialOperations || []).forEach(operation => {
      if (operation?.operation_id) pending.push(inertEntry(operation));
    });

    function snapshot() {
      return {
        pending:pending.length,
        in_flight:inFlight?.entries.length || 0,
        failed:failed.length,
        busy:!!inFlight || pending.length > 0
      };
    }

    function publish() {
      const journal = [
        ...(inFlight?.entries || []),
        ...pending,
        ...failed
      ].map(entry => entry.operation);
      options.onStatus?.(snapshot());
      options.onJournal?.(journal);
    }

    function schedule(delay = debounceMs) {
      if (destroyed || inFlight || timer || !pending.length) return;
      timer = setTimeout(() => {
        timer = null;
        flush();
      }, Math.max(0, delay));
    }

    function retryable(error) {
      if (typeof options.shouldRetry === "function") return !!options.shouldRetry(error);
      return !error?.status || error.status === 409 || error.status >= 500;
    }

    async function flush() {
      if (destroyed || inFlight || !pending.length) return null;
      const entries = pending.splice(0, maxBatchSize);
      const batch = {
        schema_version:"substar.editor-operation-batch.v1",
        batch_id:`batch_${Date.now().toString(36)}_${(++sequence).toString(36)}`,
        base:options.base(),
        operations:entries.map(entry => entry.operation)
      };
      inFlight = {entries, batch};
      publish();
      try {
        const result = await options.sendBatch(batch);
        entries.forEach(entry => entry.resolve(result));
        return result;
      } catch (error) {
        const attempts = Math.max(...entries.map(entry => entry.attempts), 0);
        if (retryable(error) && attempts < maxRetries) {
          entries.forEach(entry => { entry.attempts += 1; });
          try { await options.onRetry?.(error, batch); } catch (_) { /* next send reports it */ }
          pending.unshift(...entries);
          schedule(Math.min(4000, 150 * (2 ** attempts)));
        } else {
          failed.push(...entries.map(entry => ({...entry, error})));
          entries.forEach(entry => entry.reject(error));
          options.onFailed?.(error, entries.map(entry => entry.operation));
        }
        return null;
      } finally {
        inFlight = null;
        publish();
        schedule(0);
      }
    }

    function enqueue(operation) {
      if (destroyed) return Promise.reject(new Error("operation queue is destroyed"));
      const promise = new Promise((resolve, reject) => {
        pending.push({operation, resolve, reject, attempts:0});
      });
      publish();
      schedule();
      return promise;
    }

    function retryFailed() {
      if (!failed.length) return;
      pending.push(...failed.splice(0).map(entry => ({...entry, error:null})));
      publish();
      schedule(0);
    }

    if (pending.length) {
      publish();
      schedule(0);
    }

    return Object.freeze({
      enqueue,
      flushNow() {
        if (timer) clearTimeout(timer);
        timer = null;
        return flush();
      },
      retryFailed,
      getState:snapshot,
      destroy() {
        destroyed = true;
        if (timer) clearTimeout(timer);
        timer = null;
      }
    });
  }

  return Object.freeze({createOperationQueue});
});
