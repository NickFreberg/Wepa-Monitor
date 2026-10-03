// Small behaviours Dash doesn't provide: keyboard and outside-click dismissal of panels, the
// print button, the live log scrolling, and chart tooltips kept inside the screen on phones.
(function () {
  function openPopovers() { return Array.prototype.slice.call(document.querySelectorAll("details.popover[open]")); }

  // Escape closes whichever panel is open: a popover, notifications, or "What this means".
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") { return; }
    var pops = openPopovers();
    if (pops.length) { pops.forEach(function (d) { d.open = false; d.querySelector("summary").focus(); }); return; }
    ["xdrawer-close", "drawer-close"].forEach(function (id) {
      var panel = document.getElementById(id === "xdrawer-close" ? "xdrawer" : "drawer");
      var btn = document.getElementById(id);
      if (panel && btn && panel.classList.contains("is-open")) { btn.click(); }
    });
  });

  // A click outside an open popover closes it; opening one closes the others.
  document.addEventListener("click", function (e) {
    openPopovers().forEach(function (d) {
      if (!d.contains(e.target) && !(e.target.closest && e.target.closest(".dash-dropdown-content, .Select-menu-outer"))) {
        d.open = false;
      }
    });
    var btn = e.target.closest && e.target.closest("[data-print]");
    if (btn) { window.print(); }
  });
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (d.matches && d.matches("details.popover") && d.open) {
      openPopovers().forEach(function (o) { if (o !== d) { o.open = false; } });
    }
  }, true);

  // Keep the live log scrolled to the newest line unless the reader has scrolled up.
  var stick = new WeakMap();
  function followLog() {
    var t = document.getElementById("sy-log");
    if (!t) { return; }
    if (!stick.has(t)) {
      stick.set(t, true);
      t.addEventListener("scroll", function () { stick.set(t, t.scrollTop + t.clientHeight >= t.scrollHeight - 24); });
    }
    if (stick.get(t)) { t.scrollTop = t.scrollHeight; }
  }

  // Plotly draws hover labels where the pointer is; on a narrow screen they can spill off the
  // edge. Nudge any label that crosses the window edge back inside.
  function clampHover() {
    var w = window.innerWidth;
    document.querySelectorAll(".hoverlayer > g").forEach(function (g) {
      var r = g.getBoundingClientRect();
      if (!r.width) { return; }
      var shift = 0;
      if (r.right > w - 6) { shift = (w - 6) - r.right; }
      if (r.left + shift < 6) { shift = 6 - r.left; }
      if (!shift) { return; }
      var m = /translate\(\s*([-\d.]+)[ ,]+([-\d.]+)\s*\)/.exec(g.getAttribute("transform") || "");
      if (m) { g.setAttribute("transform", "translate(" + (parseFloat(m[1]) + shift) + "," + m[2] + ")"); }
    });
  }

  var pending = false;
  new MutationObserver(function () {
    if (pending) { return; }
    pending = true;
    requestAnimationFrame(function () { pending = false; followLog(); clampHover(); });
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
