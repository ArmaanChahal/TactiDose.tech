/**
 * WAI-ARIA tabs (automatic activation): Left/Right arrows, Home and End move between
 * tabs; the selected tab is mirrored in location.hash so a reload keeps it.
 *
 * Markup: a [role=tablist] containing [role=tab] buttons with data-tab="name" and
 * aria-controls pointing at their [role=tabpanel].
 */

export function initTabs(tablist, { onSelect = null } = {}) {
  const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
  const panels = tabs.map((tab) => document.getElementById(tab.getAttribute('aria-controls')));
  let current = null;

  function select(tab, { focus = false, updateHash = true } = {}) {
    if (!tab) return;
    tabs.forEach((t, i) => {
      const on = t === tab;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      if (panels[i]) panels[i].hidden = !on;
    });
    if (focus) tab.focus();
    if (updateHash && window.history?.replaceState) {
      window.history.replaceState(null, '', `#${tab.dataset.tab}`);
    }
    const previous = current;
    current = tab.dataset.tab;
    if (onSelect && previous !== current) onSelect(current, previous);
  }

  tablist.addEventListener('click', (e) => {
    const tab = e.target.closest('[role="tab"]');
    if (tab && tabs.includes(tab)) select(tab);
  });

  tablist.addEventListener('keydown', (e) => {
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    let next = null;
    if (e.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
    else if (e.key === 'ArrowLeft') next = tabs[(index - 1 + tabs.length) % tabs.length];
    else if (e.key === 'Home') next = tabs[0];
    else if (e.key === 'End') next = tabs[tabs.length - 1];
    if (next) {
      e.preventDefault();
      select(next, { focus: true });
    }
  });

  const fromHash = () => tabs.find((t) => `#${t.dataset.tab}` === window.location.hash);
  window.addEventListener('hashchange', () => {
    const tab = fromHash();
    if (tab) select(tab, { updateHash: false });
  });

  select(fromHash() || tabs.find((t) => t.getAttribute('aria-selected') === 'true') || tabs[0], { updateHash: false });

  return {
    /** Programmatic switch (e.g. "Enter manually" jumps to Medications). */
    select(name, { focus = true } = {}) {
      select(tabs.find((t) => t.dataset.tab === name), { focus });
    },
    get current() {
      return current;
    },
  };
}
