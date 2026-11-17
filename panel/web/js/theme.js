// Runs before the page paints (a classic script in <head>), so a light theme doesn't flash dark.
(function () {
  var pref = "dark";
  try { pref = localStorage.getItem("hearth.theme") || "dark"; } catch (e) { /* storage blocked */ }
  var light = pref === "light" || (pref === "system" && window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches);
  document.documentElement.dataset.theme = light ? "light" : "dark";
})();
