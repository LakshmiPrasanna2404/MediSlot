// Applies a saved light/dark choice before first paint (loaded in <head>).
// With no saved choice the page follows the operating-system setting.
(function () {
  try {
    var saved = localStorage.getItem('medislot-theme');
    if (saved === 'light' || saved === 'dark') {
      document.documentElement.setAttribute('data-theme', saved);
    }
  } catch (e) { /* storage blocked: follow the system setting */ }
})();
