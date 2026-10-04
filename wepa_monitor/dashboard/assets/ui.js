// Small behaviours Dash doesn't provide: keyboard and outside-click dismissal of panels, the
// print button, the live log scrolling, and chart tooltips kept inside the screen on phones.
(function () {
  // A session that ended (idle timeout, sign-out elsewhere, password change) answers Dash's requests with
  // 401: go to the sign-in page, then come back here.
  if (window.fetch && !window.__wepaFetch) {
    window.__wepaFetch = window.fetch;
    window.fetch = function (input, init) {
      return window.__wepaFetch(input, init).then(function (resp) {
        var url = (typeof input === "string" ? input : (input && input.url)) || "";
        if (resp.status === 401 && (url.indexOf("_dash") >= 0 || url.indexOf("_session") >= 0)) {
          window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname + window.location.search);
        }
        return resp;
      });
    };
  }

  function openPopovers() { return Array.prototype.slice.call(document.querySelectorAll("details.popover[open]")); }

  // Escape closes whichever panel is open: a popover, notifications, or "What this means".
  document.addEventListener("keydown", function (e) {
    // Ctrl/Cmd+I opens or closes the assistant.
    if ((e.ctrlKey || e.metaKey) && (e.key === "i" || e.key === "I")) {
      var a = document.getElementById("assist");
      var b = document.getElementById(a && a.classList.contains("is-open") ? "assist-close" : "assist-open");
      if (b) { e.preventDefault(); b.click(); }
      return;
    }
    if (e.key !== "Escape") { return; }
    var pops = openPopovers();
    if (pops.length) { pops.forEach(function (d) { d.open = false; d.querySelector("summary").focus(); }); return; }
    var assist = document.getElementById("assist");
    if (assist && assist.classList.contains("is-open")) {
      document.getElementById("assist-close").click(); document.getElementById("assist-open").focus(); return;
    }
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

  // Easter eggs. Tap the BSU theme 27 times in a row and Dark
  // becomes Cosmic; then tap Cosmic 10 times in a row and Light becomes Cup. "In a row" means no other
  // theme in between and no pause longer than 4 seconds. Unlocks live in the "eggs" store (this browser
  // only); Appearance > "Bring back the original themes" undoes them.
  var EGGS = [
    { slot: "crimson", taps: 27, unlock: "cosmic", show: "dark",
      say: "Cosmic unlocked! Dark is now Cosmic. You can bring Dark back under Appearance." },
    { slot: "dark", taps: 10, needs: "cosmic", unlock: "cup", show: "light",
      say: "Cup unlocked! Light is now Cup, straight out of 1994. You can bring Light back under Appearance." }
  ];
  var streak = { slot: null, n: 0, at: 0 };
  function readEggs() {
    try { return JSON.parse(window.localStorage.getItem("eggs")) || {}; } catch (err) { return {}; }
  }
  function toast(text) {
    var el = document.getElementById("egg-toast");
    if (!el) { return; }
    el.textContent = text;
    el.classList.add("is-shown");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { el.classList.remove("is-shown"); }, 7000);
  }
  document.addEventListener("click", function (e) {
    var input = e.target;
    if (!input.matches || !input.matches(".themes input[type=radio]")) { return; }
    var now = Date.now();
    if (streak.slot === input.value && now - streak.at < 4000) { streak.n += 1; } else { streak.slot = input.value; streak.n = 1; }
    streak.at = now;
    var eggs = readEggs();
    EGGS.forEach(function (egg) {
      if (egg.slot !== streak.slot || streak.n !== egg.taps || eggs[egg.unlock]) { return; }
      if (egg.needs && !eggs[egg.needs]) { return; }
      if (!(window.dash_clientside && window.dash_clientside.set_props)) { return; }
      eggs[egg.unlock] = true;
      window.dash_clientside.set_props("eggs", { data: eggs });
      streak = { slot: null, n: 0, at: 0 };
      // Select the slot that changed, the way a person would, so the choice is remembered.
      setTimeout(function () {
        var target = document.querySelector('.themes input[value="' + egg.show + '"]');
        if (target) { target.click(); }
        streak = { slot: null, n: 0, at: 0 };
      }, 50);
      toast(egg.say);
    });
  });

  var stick = new WeakMap();
  // Live logs (System page) and the assistant's conversation stay scrolled to the newest line,
  // unless the reader has scrolled up to look at something.
  function followLog() { ["sy-log", "assist-log"].forEach(follow); }
  function follow(id) {
    var t = document.getElementById(id);
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
