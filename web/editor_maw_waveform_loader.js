// Own the complete waveform request lifecycle, independently of document saves.
export function createWaveformLoader({load, apply, onState, timeoutMs = 20000, retries = 3, retryDelayMs = 500}) {
  let disposed = false, running = null, controller = null;
  function attempt(signal) {
    const request = new AbortController();
    let timer, abort;
    return new Promise((resolve, reject) => {
      abort = () => { request.abort(); reject(new Error('波形读取已取消')); };
      signal.addEventListener('abort', abort, {once:true});
      timer = setTimeout(() => {
        request.abort();
        reject(new Error('波形读取超时'));
      }, timeoutMs);
      Promise.resolve().then(() => {
        if (signal.aborted) throw new Error('波形读取已取消');
        return load({signal:request.signal});
      }).then(resolve, reject);
    }).finally(() => { clearTimeout(timer); signal.removeEventListener('abort', abort); });
  }
  function delay(ms, signal) {
    return new Promise(resolve => {
      const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); };
      const timer = setTimeout(done, ms);
      signal.addEventListener('abort', done, {once:true});
      if (signal.aborted) done();
    });
  }
  function start() {
    if (disposed) return Promise.resolve();
    if (running) return running;
    controller = new AbortController();
    const signal = controller.signal;
    running = (async () => {
      for (let index = 0; index <= retries && !disposed; index++) {
        onState({state:'loading', attempt:index + 1});
        try {
          const value = await attempt(signal);
          if (disposed) return;
          if (apply(value) === false) throw new Error('波形数据格式无效');
          onState({state:'ready'});
          return;
        } catch (error) {
          if (disposed) return;
          if (index === retries) { onState({state:'error', error}); return; }
          onState({state:'retrying', attempt:index + 1, error});
          await delay(retryDelayMs * 2 ** index, signal);
        }
      }
    })().finally(() => { running = null; });
    return running;
  }
  return {start, dispose() { disposed = true; controller?.abort(); }};
}
