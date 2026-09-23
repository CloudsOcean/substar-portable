(function(root) {
  function dragMode(target) {
    if (target.closest('input,textarea,select,a,[contenteditable="true"],.token-selection-menu')) return null;
    if (target.closest('.source-token-line')) return 'tokens';
    const button=target.closest('button');
    if (button && !button.matches('[data-cue-select]')) return null;
    return 'cues';
  }
  function intersectingTokens(boxes, rect) {
    return [...boxes].filter(([,box]) => box.right>=rect.left && box.left<=rect.right &&
      box.bottom>=rect.top && box.top<=rect.bottom).map(([id]) => id);
  }
  function createSelection() {
    let ids = new Set(), anchor = null;
    return {
      get ids() { return new Set(ids); },
      clear() { ids.clear(); anchor = null; },
      reconcile(order) { const valid = new Set(order); ids = new Set([...ids].filter(x => valid.has(x))); if (!valid.has(anchor)) anchor = null; },
      select(id, order, {range=false, toggle=false}={}) {
        if (!order.includes(id)) return;
        if (range && order.includes(anchor)) {
          const a=order.indexOf(anchor), b=order.indexOf(id);
          const span=order.slice(Math.min(a,b), Math.max(a,b)+1);
          ids = new Set(toggle ? [...ids,...span] : span);
        } else {
          if (toggle) { if (ids.has(id)) ids.delete(id); else ids.add(id); }
          else ids = new Set([id]);
          anchor=id;
        }
      },
      drag(first, last, order, original=[]) {
        const a=order.indexOf(first), b=order.indexOf(last);
        if (a<0 || b<0) return;
        ids = new Set([...original,...order.slice(Math.min(a,b),Math.max(a,b)+1)]);
        anchor=first;
      }
    };
  }
  if (typeof module !== 'undefined') module.exports={createSelection,dragMode,intersectingTokens};
  else root.EditorCueSelection={createSelection,dragMode,intersectingTokens};
})(typeof window !== 'undefined' ? window : globalThis);
