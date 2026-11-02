(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorOperationQueue = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function createOperationQueue(options) {
    if (typeof options?.sendBatch !== "function") throw new Error("sendBatch is required");
    let debounceMs = Math.max(0, Number(options.debounceMs ?? 60));
    const maxBatchSize = Math.max(1, Number(options.maxBatchSize ?? 100));
    let maxRetries = Math.max(0, Number(options.maxRetries ?? 3));
    const requestTimeoutMs = Math.max(1, Number(options.requestTimeoutMs ?? 30000));
    const pending = [];
    const failed = [];
    let inFlight = null;
    let timer = null;
    let sequence = 0;
    let destroyed = false;
    let paused = false;

    function inertEntry(operation) {
      const entry = {operation, attempts:0};
      entry.promise = new Promise((resolve, reject) => {
        entry.resolve = resolve;
        entry.reject = reject;
      });
      // Restored entries have no caller awaiting their result.
      entry.promise.catch(() => {});
      return entry;
    }

    function sameOperation(left, right) {
      // Match server receipt identity; retention/base are not action content.
      const canonical = value => {
        if (Array.isArray(value)) return value.map(canonical);
        if (value && typeof value === "object") return Object.fromEntries(
          Object.keys(value).sort().map(key => [key, canonical(value[key])]));
        return value;
      };
      return left.type === right.type
        && JSON.stringify(canonical(left.payload || {})) === JSON.stringify(canonical(right.payload || {}));
    }

    (options.initialOperations || []).forEach(operation => {
      if (!operation?.operation_id) return;
      const existing = pending.find(entry => entry.operation.operation_id === operation.operation_id);
      if (existing) {
        if (!sameOperation(existing.operation, operation)) throw new Error("同一操作编号对应不同编辑，恢复记录已保留，请处理冲突");
        return;
      }
      pending.push(inertEntry(operation));
    });

    function snapshot() {
      return {
        pending:pending.length,
        in_flight:inFlight?.entries.length || 0,
        failed:failed.length,
        busy:!!inFlight || pending.length > 0,
        hasUnsavedChanges:!!inFlight || pending.length > 0 || failed.length > 0
      };
    }

    function observe(name, ...args) {
      try { options[name]?.(...args); }
      catch (error) {
        // Rendering must never interrupt persistence or turn a committed save
        // into a retry. Recovery-journal failures are reported by its owner.
        try { options.onObserverError?.(error, name); } catch (_) { /* Observer only. */ }
      }
    }

    function publish() {
      const journal = [
        ...failed,
        ...(inFlight?.entries || []),
        ...pending
      ].map(entry => entry.operation);
      observe("onJournal", journal);
      observe("onStatus", snapshot());
    }

    function schedule(delay = debounceMs) {
      if (destroyed || paused || inFlight || timer || failed.length || !pending.length) return;
      timer = setTimeout(() => {
        timer = null;
        flush();
      }, Math.max(0, delay));
    }

    function retryable(error) {
      if (typeof options.shouldRetry === "function") return !!options.shouldRetry(error);
      return !error?.status || error.status >= 500;
    }

    async function flush() {
      if (destroyed || paused || inFlight || failed.length || !pending.length) return null;
      let count = maxBatchSize;
      if (pending[0]?.retryBatchId) {
        count = 1;
        while (count < pending.length && count < maxBatchSize && pending[count].retryBatchId === pending[0].retryBatchId) count++;
      } else if (options.groupByUserAction && pending.length) {
        const action = entry => entry.operation.payload?.provenance?.metadata?.user_action_id || entry.operation.operation_id;
        const first = action(pending[0]);
        count = 1;
        while (count < pending.length && count < maxBatchSize && action(pending[count]) === first) count++;
      }
      const entries = pending.splice(0, count);
      const batch = {
        schema_version:"substar.editor-operation-batch.v1",
        batch_id:`batch_${Date.now().toString(36)}_${(++sequence).toString(36)}`,
        base:null,
        preserve_user_actions:!!options.preserveUserActions,
        operations:entries.map(entry => entry.operation)
      };
      inFlight = {entries, batch};
      publish();
      let retryDelay = 0;
      const controller = new AbortController();
      let requestTimer;
      try {
        batch.base = options.base();
        const result = await Promise.race([
          Promise.resolve(options.sendBatch(batch, {signal:controller.signal})),
          new Promise((_, reject) => {
            requestTimer = setTimeout(() => {
              controller.abort();
              reject(new Error("保存请求超时，操作已保留，将按设置重试"));
            }, requestTimeoutMs);
          })
        ]);
        entries.forEach(entry => entry.resolve(result));
        return result;
      } catch (error) {
        const attempts = Math.max(...entries.map(entry => entry.attempts), 0);
        if (retryable(error) && attempts < maxRetries) {
          entries.forEach(entry => { entry.attempts += 1; entry.retryBatchId = batch.batch_id; });
          try { await options.onRetry?.(error, batch); } catch (_) { /* next send reports it */ }
          pending.unshift(...entries);
          retryDelay = Math.min(4000, 150 * (2 ** attempts));
        } else {
          failed.push(...entries.map(entry => ({...entry, error, retryBatchId:batch.batch_id})));
          entries.forEach(entry => entry.reject(error));
          observe("onFailed", error, entries.map(entry => entry.operation));
        }
        return null;
      } finally {
        clearTimeout(requestTimer);
        inFlight = null;
        publish();
        schedule(retryDelay);
      }
    }

    function enqueue(operation) {
      if (destroyed) return Promise.reject(new Error("operation queue is destroyed"));
      if (!operation?.operation_id) return Promise.reject(new Error("编辑缺少操作编号，无法保存"));
      const existing = [...failed, ...(inFlight?.entries || []), ...pending]
        .find(entry => entry.operation.operation_id === operation.operation_id);
      if (existing) {
        if (!sameOperation(existing.operation, operation)) return Promise.reject(new Error("同一操作编号对应不同编辑，请处理冲突"));
        return existing.promise;
      }
      const entry = inertEntry(operation);
      pending.push(entry);
      publish();
      schedule();
      return entry.promise;
    }

    function retryFailed() {
      if (!failed.length) return;
      pending.unshift(...failed.splice(0).map(entry => ({...inertEntry(entry.operation), retryBatchId:entry.retryBatchId})));
      publish();
      schedule(0);
    }

    if (pending.length) {
      publish();
      schedule(0);
    }

    return Object.freeze({
      enqueue,
      pause() {
        paused = true;
        if (timer) clearTimeout(timer);
        timer = null;
      },
      resume() { paused = false; schedule(); },
      async waitForInFlight() {
        while (inFlight && !destroyed) await new Promise(resolve => setTimeout(resolve, 20));
      },
      configure({debounceMs:delay, maxRetries:retries}) {
        if (Number.isFinite(delay) && delay >= 0) debounceMs = delay;
        if (Number.isInteger(retries) && retries >= 0) maxRetries = retries;
        if (timer) clearTimeout(timer);
        timer = null;
        schedule();
      },
      flushNow() {
        if (timer) clearTimeout(timer);
        timer = null;
        return flush();
      },
      async flushAndWait() {
        if (timer) clearTimeout(timer);
        timer = null;
        await flush();
        while (inFlight || pending.length) {
          if (failed.length) throw new Error(failed[0].error?.message || "仍有编辑未保存，请先处理保存冲突或错误");
          if (destroyed) throw new Error("保存会话已结束");
          await new Promise(resolve => setTimeout(resolve, 20));
        }
        if (failed.length) throw new Error(failed[0].error?.message || "仍有编辑未保存，请先处理保存冲突或错误");
        if (destroyed) throw new Error("保存会话已结束");
        return options.base();
      },
      discardUnsaved() {
        if (inFlight) throw new Error("保存请求仍在进行");
        const entries = [...failed.splice(0), ...pending.splice(0)];
        entries.forEach(entry => entry.reject(new Error("编辑已保留为恢复副本")));
        publish();
        return entries.map(entry => entry.operation);
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
