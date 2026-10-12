// Apply the theme before anything renders (no flash of the wrong mode).
// A file of its own rather than an inline script in index.html: the Content-Security-Policy in
// the Caddyfile allows scripts from 'self' only, and an inline script would need a hash that
// breaks with every edit. It is loaded as a classic, render-blocking script on purpose.
(function () {
  try {
    var saved = localStorage.getItem("theme");
    var dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.classList.add(dark ? "dark" : "light");
  } catch (e) {
    document.documentElement.classList.add("light");
  }
})();
