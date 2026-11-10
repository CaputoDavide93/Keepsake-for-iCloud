/* Keepsake: apply the saved theme before the first paint, so a dark choice never
   flashes light. Loaded from <head> as its own file because the content security
   policy forbids inline scripts. No stored choice means Auto: the page follows the
   system setting through prefers-color-scheme in style.css. */
(function () {
  try {
    var t = localStorage.getItem("keepsake-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: Auto */ }
})();
