/**
 * Lazy loading for care-portal tab panels: a panel loads when it is first shown, and
 * reloads (debounced) when live events mark it stale while visible — otherwise on the
 * next show.
 */

import { debounce } from '../dom.js';

export function lazyPanel(load, { delay = 350 } = {}) {
  let visible = false;
  let stale = true;
  const soon = debounce(() => {
    stale = false;
    load();
  }, delay);
  return {
    show() {
      visible = true;
      if (stale) {
        stale = false;
        load();
      }
    },
    hide() {
      visible = false;
    },
    markStale() {
      stale = true;
      if (visible) soon();
    },
    reset() {
      stale = true;
      soon.cancel();
    },
    reload() {
      stale = false;
      load();
    },
    get visible() {
      return visible;
    },
  };
}
