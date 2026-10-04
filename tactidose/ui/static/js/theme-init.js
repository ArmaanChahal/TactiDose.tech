/*
 * Applies the saved colour theme and text size before first paint. Loaded as a classic
 * (non-module) script in <head> so the page never flashes the wrong theme.
 * Default: light; existing saved preferences are preserved. Other themes: "light" (black on white) and
 * "yellow" (yellow on black). Text size: "normal", "large", "xlarge".
 */
(function applySavedDisplay() {
  try {
    var root = document.documentElement;
    var theme = window.localStorage.getItem('tactidose.theme');
    root.setAttribute('data-theme', ['light', 'dark', 'yellow'].includes(theme) ? theme : 'light');
    var size = window.localStorage.getItem('tactidose.textSize');
    if (size === 'large' || size === 'xlarge') {
      root.setAttribute('data-text-size', size);
    }
  } catch (err) {
    /* storage unavailable: keep the defaults */
  }
})();
