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

  // Sandman (wepa_monitor/sandman.py): when the egg fires, play the server's copy of the song if it has one,
  // and a second or two in, type "Exit light. Enter night." into a banner that leaves after about 10 seconds.
  // Music that plays on its own needs a way to stop it (WCAG 1.4.2): the "Stop the music" button stays on
  // screen while it plays, and Escape stops it too. Reduced motion shows the line without the typing.
  window.spoSandman = (function () {
    var LINE = "Exit light. Enter night.";
    var started = 0, timers = [];
    function el(id) { return document.getElementById(id); }
    function later(fn, ms) { timers.push(setTimeout(fn, ms)); }
    function calm() {
      var d = document.documentElement.dataset;
      return d.motion === "reduce" || (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    }
    function stopButton(show) { var b = el("sandman-stop"); if (b) { b.hidden = !show; } }
    function stop() {
      var a = el("sandman-audio");
      if (a) { a.pause(); try { a.currentTime = 0; } catch (err) { /* not loaded yet */ } }
      stopButton(false);
    }
    function banner() {
      var box = el("sandman-banner");
      if (!box) { return; }
      var text = box.querySelector(".sandman-banner__text"), said = box.querySelector(".sr-only");
      box.hidden = false; box.classList.remove("is-leaving");
      text.textContent = ""; said.textContent = LINE;
      if (calm()) { text.textContent = LINE; }
      else {
        box.classList.add("is-typing");
        var at = 0;
        LINE.split("").forEach(function (ch, i) {
          at += (ch === " " && LINE[i - 1] === ".") ? 420 : 120;              // a beat after "Exit light."
          later(function () { text.textContent += ch; if (i === LINE.length - 1) { box.classList.remove("is-typing"); } }, at);
        });
      }
      later(function () { box.classList.add("is-leaving"); }, 10000);
      later(function () { box.hidden = true; box.classList.remove("is-leaving"); said.textContent = ""; }, 10800);
    }
    function start(withAudio) {
      timers.forEach(clearTimeout); timers = [];
      var a = el("sandman-audio"), delay = 600;
      if (withAudio && a) {
        a.src = "/_sandman/audio";
        var p = a.play();
        if (p && p.then) { p.then(function () { stopButton(true); }).catch(function () { stopButton(false); }); }
        delay = 1800;
      }
      later(banner, delay);
    }
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") { stop(); } });
    document.addEventListener("click", function (e) {
      if (e.target.closest && e.target.closest("#sandman-stop")) { stop(); }
    });
    document.addEventListener("ended", function (e) { if (e.target.id === "sandman-audio") { stopButton(false); } }, true);
    return {
      maybeStart: function (eggs) {
        var at = eggs && eggs.sandman, played = null;
        try { played = window.sessionStorage.getItem("spo-sandman-played"); } catch (err) { /* storage off */ }
        // Fresh unlocks only: never again after a reload or for an unlock this tab has already played.
        if (!at || at === started || String(at) === played || Date.now() - at > 20000) { return; }
        started = at;
        try { window.sessionStorage.setItem("spo-sandman-played", String(at)); } catch (err) { /* storage off */ }
        start(!!eggs.sandman_audio);
      },
      stop: function () {
        stop(); timers.forEach(clearTimeout); timers = [];
        var box = el("sandman-banner"); if (box) { box.hidden = true; }
      }
    };
  })();

  // 6 7. Whenever the number 67 shows up anywhere (text, chart labels, tooltips, what someone types), two hands
  // bob "6... 7..." in a pink panel, the page shakes a little, and a ring wobbles around the 67. Only 67 as a
  // number of its own: not 167, 670, 0.67, 67.5 or 1,067. Each show lasts under 5 seconds (WCAG 2.2.2), nothing
  // flashes (2.3.1), Escape dismisses it, and reduced motion gets a still version. It's decorative: aria-hidden.
  var SIXSEVEN = /(^|[^\d.,#])67(?![\d]|[.,]\d)/;
  var seen67 = new WeakMap(), showing67 = null, scan67Timer = null;
  function calm67() {
    return document.documentElement.dataset.motion === "reduce" ||
      (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }
  function hide67() {
    if (!showing67) { return; }
    clearTimeout(showing67.timer);
    showing67.els.forEach(function (el) { el.remove(); });
    showing67 = null;
  }
  function show67(rect) {
    if (showing67) { return; }
    var still = calm67(), els = [];
    var panel = document.createElement("div");
    panel.className = "sixseven" + (still ? " sixseven--still" : "");
    panel.setAttribute("aria-hidden", "true");
    panel.innerHTML = '<div class="sixseven__hand sixseven__hand--six"><img src="/assets/six-seven-hand.svg" alt=""><b>6</b></div>' +
      '<div class="sixseven__hand sixseven__hand--seven"><img src="/assets/six-seven-hand.svg" alt=""><b>7</b></div>';
    document.body.appendChild(panel); els.push(panel);
    if (rect && rect.width) {
      var ring = document.createElement("div");
      ring.className = "sixseven-ring" + (still ? " sixseven--still" : "");
      ring.setAttribute("aria-hidden", "true");
      ring.style.left = (rect.left - 8) + "px"; ring.style.top = (rect.top - 6) + "px";
      ring.style.width = (rect.width + 16) + "px"; ring.style.height = (rect.height + 12) + "px";
      document.body.appendChild(ring); els.push(ring);
    }
    var shell = document.querySelector(".shell");
    if (shell && !still) {
      shell.classList.remove("is-sixseven"); void shell.offsetWidth; shell.classList.add("is-sixseven");
      setTimeout(function () { shell.classList.remove("is-sixseven"); }, 600);
    }
    showing67 = { els: els, timer: setTimeout(hide67, 4600) };
  }
  function rangeRect(node, index) {
    try {
      var r = document.createRange();
      r.setStart(node, index); r.setEnd(node, index + 2);
      return r.getBoundingClientRect();
    } catch (err) { return null; }
  }
  function scan67() {
    scan67Timer = null;
    if (!document.body) { return; }
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        var p = n.parentElement;
        if (!p || !n.nodeValue || n.nodeValue.indexOf("67") < 0) { return NodeFilter.FILTER_REJECT; }
        if (p.closest("script, style, noscript, .sixseven, [hidden]")) { return NodeFilter.FILTER_REJECT; }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var first = null, n;
    while ((n = walker.nextNode())) {
      if (seen67.get(n) === n.nodeValue) { continue; }
      seen67.set(n, n.nodeValue);
      var m = SIXSEVEN.exec(n.nodeValue);
      if (!m || !n.parentElement.getClientRects().length) { continue; }
      if (!first) { first = rangeRect(n, m.index + m[1].length); }
    }
    if (first) { show67(first); }
  }
  function queue67() { if (!scan67Timer) { scan67Timer = setTimeout(scan67, 350); } }
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t && typeof t.value === "string" && SIXSEVEN.test(t.value)) { show67(t.getBoundingClientRect()); }
  }, true);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { hide67(); } });

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
    requestAnimationFrame(function () { pending = false; followLog(); clampHover(); queue67(); });
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
