const FADE_OUT_SECONDS = 0.005;
const FADE_IN_SECONDS = 0.008;
const routes = new WeakMap();
let context;

async function audioRoute(media) {
  const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!media?.currentSrc || !AudioContextClass) return null;
  if (new URL(media.currentSrc).origin !== location.origin) return null;
  context ||= new AudioContextClass();
  if (context.state !== 'running') await context.resume();
  if (context.state !== 'running') return null;
  let route = routes.get(media);
  if (!route) {
    const gain = context.createGain();
    context.createMediaElementSource(media).connect(gain).connect(context.destination);
    route = gain.gain;
    routes.set(media, route);
  }
  return route;
}

export function createPreviewSeek(media, {signal}) {
  let timer;
  let target = null;
  let waiting = false;

  function prepare() {
    if (signal.aborted || (routes.has(media) && context.state === 'running')) return;
    // A fresh user gesture can unlock a resume requested before autoplay was allowed.
    audioRoute(media).catch(() => null);
  }

  function reset(fadeIn = false) {
    clearTimeout(timer);
    target = null;
    waiting = false;
    const gain = routes.get(media);
    if (!gain) return;
    const now = context.currentTime;
    gain.cancelScheduledValues(now);
    if (fadeIn) {
      gain.setValueAtTime(0, now);
      gain.linearRampToValueAtTime(1, now + FADE_IN_SECONDS);
    } else {
      gain.setValueAtTime(1, now);
    }
  }

  function write(delayedTarget) {
    waiting = true;
    target = null;
    media.currentTime = delayedTarget;
    if (!media.seeking) reset(true);
    else timer = setTimeout(() => reset(true), 1500);
  }

  function seek(seconds) {
    if (!media) return;
    const gain = routes.get(media);
    if (media.paused || media.muted || media.volume === 0 || !gain || context.state !== 'running') {
      if (target !== null || waiting) reset();
      media.currentTime = seconds;
      return;
    }
    if (target !== null) {
      target = seconds;
      return;
    }
    if (waiting) {
      clearTimeout(timer);
      write(seconds);
      return;
    }
    if (!media.seeking && Math.abs(media.currentTime - seconds) < 0.000001) return;

    target = seconds;
    const now = context.currentTime;
    const fadeEnd = now + FADE_OUT_SECONDS;
    if (typeof gain.cancelAndHoldAtTime === 'function') gain.cancelAndHoldAtTime(now);
    else {
      const level = gain.value;
      gain.cancelScheduledValues(now);
      gain.setValueAtTime(level, now);
    }
    gain.linearRampToValueAtTime(0, fadeEnd);
    const finishFade = () => {
      if (target === null) return;
      const remaining = fadeEnd - context.currentTime;
      if (context.state === 'running' && remaining > 0.0001) {
        timer = setTimeout(finishFade, Math.ceil(remaining * 1000));
      } else {
        write(target);
      }
    };
    timer = setTimeout(finishFade, FADE_OUT_SECONDS * 1000);
  }

  const options = {signal};
  media?.addEventListener('play', prepare, options);
  media?.addEventListener('seeked', () => {
    if (waiting && !media.seeking) reset(true);
  }, options);
  media?.addEventListener('pause', () => {
    if (target !== null) media.currentTime = target;
    reset();
  }, options);
  for (const event of ['emptied', 'error']) media?.addEventListener(event, () => reset(), options);
  signal.addEventListener('abort', () => reset(), {once:true});
  return {prepare, seek, get pending() {return target !== null || waiting;}};
}
