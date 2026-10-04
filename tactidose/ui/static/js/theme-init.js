/*
 * Applies the saved colour theme before first paint. Loaded as a classic
 * (non-module) script in <head> so the page never flashes the wrong theme.
 * Default is the dark theme (black background).
 */
(function applySavedTheme() {
  try {
    var saved = window.localStorage.getItem('tactidose.theme');
    if (saved === 'light' || saved === 'dark') {
      document.documentElement.setAttribute('data-theme', saved);
    }
  } catch (err) {
    /* storage unavailable: keep the default dark theme */
  }
})();
