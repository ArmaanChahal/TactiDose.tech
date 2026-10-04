/*
 * Applies the saved colour theme and text size before first paint. Loaded as a classic
 * (non-module) script in <head> so the page never flashes the wrong theme.
 * Default: white on black ("dark"). Other themes: "light" (black on white) and
 * "yellow" (yellow on black). Text size: "normal", "large", "xlarge".
 */
(function applySavedDisplay() {
  try {
    var root = document.documentElement;
    var theme = window.localStorage.getItem('tactidose.theme');
    if (theme === 'light' || theme === 'dark' || theme === 'yellow') {
      root.setAttribute('data-theme', theme);
    }
    var size = window.localStorage.getItem('tactidose.textSize');
    if (size === 'large' || size === 'xlarge') {
      root.setAttribute('data-text-size', size);
    }
  } catch (err) {
    /* storage unavailable: keep the defaults */
  }
})();
