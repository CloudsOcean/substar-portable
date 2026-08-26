(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.EditorWaveformCache = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function createWaveformCache({limit = 16} = {}) {
    const values = new Map();
    const inFlight = new Map();
    const latestByGroup = new Map();
    const stats = {hits:0, misses:0, deduped:0, cancelled:0};

    function keyFor(request) {
      return [
        request.mediaId,
        Number(request.start).toFixed(3),
        Number(request.end).toFixed(3),
        Number(request.points)
      ].join(":");
    }

    function remember(key, value) {
      values.delete(key);
      values.set(key, value);
      while (values.size > Math.max(1, Number(limit))) {
        values.delete(values.keys().next().value);
      }
    }

    function request(input) {
      const key = keyFor(input);
      if (values.has(key)) {
        const value = values.get(key);
        remember(key, value);
        stats.hits += 1;
        return Promise.resolve(value);
      }
      if (inFlight.has(key)) {
        stats.deduped += 1;
        return inFlight.get(key).promise;
      }
      stats.misses += 1;
      const group = String(input.latestGroup || input.mediaId || "default");
      const previousKey = latestByGroup.get(group);
      if (previousKey && previousKey !== key) {
        const previous = inFlight.get(previousKey);
        if (previous) {
          previous.controller.abort();
          stats.cancelled += 1;
        }
      }
      const controller = new AbortController();
      latestByGroup.set(group, key);
      const promise = Promise.resolve(input.load({signal:controller.signal}))
        .then(value => {
          remember(key, value);
          return value;
        })
        .finally(() => {
          inFlight.delete(key);
          if (latestByGroup.get(group) === key) latestByGroup.delete(group);
        });
      inFlight.set(key, {promise, controller, group});
      return promise;
    }

    return Object.freeze({
      request,
      clear() {
        inFlight.forEach(entry => entry.controller.abort());
        inFlight.clear();
        latestByGroup.clear();
        values.clear();
      },
      getStats() {
        return {...stats, entries:values.size, in_flight:inFlight.size};
      }
    });
  }

  return Object.freeze({createWaveformCache});
});
