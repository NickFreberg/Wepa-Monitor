// Rounds map editor (administrator only): drop starts, doors, van parking spots and ends, and draw walking
// paths and van routes. Built for a phone: tap a tool, tap the map. The line tool snaps each tap to a nearby
// start, door, van spot, end or the end of another line; tapping one of those after the first point finishes
// the line there. Saves go to /_rounds/map.json with the page's CSRF token.
(function () {
  "use strict";
  var boot = JSON.parse(document.getElementById("rm-boot").textContent);
  var SNAP_PX = 30;
  var KINDS = {
    start: { label: "Start", color: "#1a7f37" },
    door: { label: "Door", color: "#8a1c24" },
    parking: { label: "Van spot", color: "#c25e00" },
    end: { label: "End", color: "#1d1d1f" },
    walk: { label: "Walk path", color: "#1f5fae" },
    van: { label: "Van route", color: "#e07a10" }
  };
  var ICONS = {
    select: '<path d="M5 3l14 8-6 2 4 7-3 1-4-7-5 4z"/>',
    start: '<path d="M6 21V4M6 4h11l-2 4 2 4H6"/>',
    door: '<path d="M5 21h14M7 21V3h10v18M14 12h.01"/>',
    parking: '<path d="M2 16V8a2 2 0 0 1 2-2h11l5 5v5h-2M2 16h2m4 0h6"/><circle cx="6" cy="16.5" r="2"/><circle cx="16" cy="16.5" r="2"/><path d="M15 6v5h5M5 9h7"/>',
    end: '<path d="M6 21V4h12v9H6M10 4v9M14 4v9M6 8.5h12"/>',
    walk: '<circle cx="13" cy="4" r="2"/><path d="M9 21l2-7 3 3v5M7 12l3-4 4 1 3 3M11 14l-1-5"/>',
    van: '<path d="M3 17l5-9 4 5 3-3 6 7"/><circle cx="3" cy="17" r="1.5"/><circle cx="21" cy="17" r="1.5"/>'
  };
  function svg(kind, color, size) {
    return '<svg viewBox="0 0 24 24" width="' + (size || 22) + '" height="' + (size || 22) + '" fill="none" stroke="' +
      (color || "currentColor") + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + ICONS[kind] + "</svg>";
  }
  document.querySelectorAll(".rm-tools button").forEach(function (b) {
    var k = b.dataset.tool;
    b.querySelector(".rm-ico").outerHTML = svg(k === "parking" ? "parking" : k);
  });

  // --- map ----------------------------------------------------------------------------------------------
  var map = L.map("rm-map", { zoomControl: true, tap: true }).setView([41.9876, -70.9695], 17);
  var streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 20, maxNativeZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(map);
  var aerial = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 20, maxNativeZoom: 19, attribution: "Imagery &copy; Esri" });
  L.control.layers({ "Streets": streets, "Aerial (find doors)": aerial }, null, { position: "topright" }).addTo(map);

  var data = { points: [], paths: [] };
  var layers = {};               // id -> Leaflet layer
  var tool = "select", dirty = false, selected = null, line = null, seq = Date.now() % 100000;
  var statusEl = document.getElementById("rm-status"), saveBtn = document.getElementById("rm-save");
  var hint = document.getElementById("rm-hint"), sheet = document.getElementById("rm-sheet");
  var lineBar = document.getElementById("rm-line");

  function newId(prefix) { seq += 1; return prefix + seq.toString(36); }
  function setDirty(v) { dirty = v; saveBtn.disabled = !v; if (v) { say("Unsaved changes"); } }
  function say(t) { statusEl.textContent = t; }
  function esc(t) { return String(t || "").replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";"; }); }

  function nearestBuilding(ll) {
    var best = null, bd = Infinity;
    boot.buildings.forEach(function (b) {
      var d = map.distance(ll, [b.lat, b.lon]);
      if (d < bd) { bd = d; best = b.name; }
    });
    return best || "";
  }

  // --- drawing ---------------------------------------------------------------------------------------------
  function pointIcon(p) {
    var k = KINDS[p.kind];
    return L.divIcon({ className: "rm-pin rm-pin--" + p.kind, iconSize: [40, 40], iconAnchor: [20, 20],
      html: '<span style="border-color:' + k.color + '">' + svg(p.kind, k.color, 22) + "</span>" });
  }
  function drawPoint(p) {
    if (layers[p.id]) { map.removeLayer(layers[p.id]); }
    var m = L.marker([p.lat, p.lon], { icon: pointIcon(p), draggable: tool === "select", keyboard: true,
      title: KINDS[p.kind].label + (p.name ? ": " + p.name : p.building ? ": " + p.building : "") });
    m.on("click", function (e) { L.DomEvent.stopPropagation(e); onPointTap(p, e); });
    m.on("dragend", function () {
      var ll = m.getLatLng();
      p.lat = +ll.lat.toFixed(6); p.lon = +ll.lng.toFixed(6);
      setDirty(true);
    });
    m.addTo(map);
    layers[p.id] = m;
  }
  function drawPath(q) {
    if (layers[q.id]) { map.removeLayer(layers[q.id]); }
    var k = KINDS[q.kind];
    var pl = L.polyline(q.coords, { color: k.color, weight: 6, opacity: .85, dashArray: q.kind === "walk" ? "2 10" : null,
      lineCap: "round" });
    pl.on("click", function (e) {
      if (tool !== "select") { return; }
      L.DomEvent.stopPropagation(e); select(q, "path");
    });
    pl.addTo(map);
    layers[q.id] = pl;
  }
  function redraw() {
    Object.keys(layers).forEach(function (id) { map.removeLayer(layers[id]); });
    layers = {};
    data.paths.forEach(drawPath);
    data.points.forEach(drawPoint);
  }

  // --- tools -------------------------------------------------------------------------------------------------
  var HINTS = {
    select: "Tap a pin or line to name or delete it. Drag a pin to move it.",
    start: "Tap where a round can begin, like an office or desk.",
    door: "Tap the entrance to use. Switch to Aerial to see doors. It's matched to the nearest building.",
    parking: "Tap where the van parks. It's matched to the nearest building.",
    end: "Tap where a one-way round can finish.",
    walk: "Tap to start a walking path (it snaps to pins and line ends), tap along the way, then Finish.",
    van: "Tap to start a van route (it snaps to pins and line ends), tap along the road, then Finish."
  };
  function setTool(t) {
    if (line) { cancelLine(); }
    tool = t;
    closeSheet();
    document.querySelectorAll(".rm-tools button").forEach(function (b) {
      b.classList.toggle("is-on", b.dataset.tool === t);
      b.setAttribute("aria-pressed", b.dataset.tool === t ? "true" : "false");
    });
    hint.textContent = HINTS[t];
    Object.keys(layers).forEach(function (id) {
      var l = layers[id];
      if (l.dragging) { if (t === "select") { l.dragging.enable(); } else { l.dragging.disable(); } }
    });
    document.body.classList.toggle("rm-drawing", t === "walk" || t === "van");
  }
  document.querySelectorAll(".rm-tools button").forEach(function (b) {
    b.addEventListener("click", function () { setTool(b.dataset.tool); });
  });

  map.on("click", function (e) {
    if (tool === "select") { closeSheet(); return; }
    if (KINDS[tool] && (tool === "walk" || tool === "van")) { addVertex(e.latlng, null); return; }
    var p = { id: newId("p"), kind: tool, lat: +e.latlng.lat.toFixed(6), lon: +e.latlng.lng.toFixed(6), name: "",
      building: (tool === "door" || tool === "parking") ? nearestBuilding(e.latlng) : "" };
    data.points.push(p);
    drawPoint(p);
    setDirty(true);
    select(p, "point");
  });

  function onPointTap(p, e) {
    if (tool === "walk" || tool === "van") { addVertex(L.latLng(p.lat, p.lon), p); return; }
    if (tool === "select") { select(p, "point"); }
  }

  // --- line tool -----------------------------------------------------------------------------------------------
  function snapTargets() {
    var out = data.points.map(function (p) { return { ll: L.latLng(p.lat, p.lon), pin: p }; });
    data.paths.forEach(function (q) {
      out.push({ ll: L.latLng(q.coords[0]), end: true });
      out.push({ ll: L.latLng(q.coords[q.coords.length - 1]), end: true });
    });
    return out;
  }
  function snap(ll) {
    var px = map.latLngToContainerPoint(ll), best = null, bd = SNAP_PX;
    snapTargets().forEach(function (t) {
      var d = px.distanceTo(map.latLngToContainerPoint(t.ll));
      if (d < bd) { bd = d; best = t; }
    });
    return best;
  }
  function addVertex(ll, pin) {
    var hit = pin ? { ll: L.latLng(pin.lat, pin.lon), pin: pin } : snap(ll);
    var at = hit ? hit.ll : ll;
    if (!line) {
      line = { kind: tool, coords: [], marks: [] };
      line.layer = L.polyline([], { color: KINDS[tool].color, weight: 6, opacity: .9,
        dashArray: tool === "walk" ? "2 10" : null }).addTo(map);
      lineBar.hidden = false;
    }
    line.coords.push([+at.lat.toFixed(6), +at.lng.toFixed(6)]);
    line.layer.setLatLngs(line.coords);
    line.marks.push(L.circleMarker(at, { radius: 7, color: KINDS[tool].color, weight: 3, fillColor: "#fff",
      fillOpacity: 1 }).addTo(map));
    var n = line.coords.length;
    hint.textContent = n === 1 ? (hit ? "Snapped. Now tap along the way." : "Started. Tap along the way.")
      : (hit ? "Snapped to " + (hit.pin ? KINDS[hit.pin.kind].label.toLowerCase() : "a line end") + "." : n + " points so far.");
    if (n > 1 && hit && hit.pin) { finishLine(); }
  }
  function clearLine() {
    if (!line) { return; }
    map.removeLayer(line.layer);
    line.marks.forEach(function (m) { map.removeLayer(m); });
    line = null;
    lineBar.hidden = true;
  }
  function cancelLine() { clearLine(); hint.textContent = HINTS[tool] || ""; }
  function finishLine() {
    if (!line) { return; }
    if (line.coords.length < 2) { hint.textContent = "A line needs at least two points."; return; }
    var q = { id: newId("l"), kind: line.kind, coords: line.coords.slice(), name: "" };
    clearLine();
    data.paths.push(q);
    drawPath(q);
    setDirty(true);
    hint.textContent = KINDS[q.kind].label + " added. Tap to start another, or pick a tool.";
  }
  document.getElementById("rm-undo").addEventListener("click", function () {
    if (!line || !line.coords.length) { return; }
    line.coords.pop();
    map.removeLayer(line.marks.pop());
    line.layer.setLatLngs(line.coords);
    if (!line.coords.length) { cancelLine(); }
  });
  document.getElementById("rm-cancel").addEventListener("click", cancelLine);
  document.getElementById("rm-finish").addEventListener("click", finishLine);

  // --- the sheet: name, building, delete -------------------------------------------------------------------------
  function closeSheet() { sheet.hidden = true; sheet.innerHTML = ""; selected = null; }
  function select(item, type) {
    selected = { item: item, type: type };
    var k = KINDS[item.kind];
    var opts = boot.buildings.map(function (b) {
      return '<option value="' + esc(b.name) + '"' + (b.name === item.building ? " selected" : "") + ">" + esc(b.name) + "</option>";
    }).join("");
    var needsBuilding = item.kind === "door" || item.kind === "parking";
    sheet.innerHTML =
      '<div class="rm-sheet__head"><b>' + esc(k.label) + "</b>" +
      (type === "path" ? " · " + item.coords.length + " points" : "") + "</div>" +
      '<label class="rm-field">Name' + (needsBuilding ? " (optional)" : "") +
      '<input id="rm-name" maxlength="80" value="' + esc(item.name) + '" placeholder="' +
      (item.kind === "start" ? "e.g. ResNet office" : item.kind === "end" ? "e.g. IT Service Center" : "") + '"></label>' +
      (type === "point" ? '<label class="rm-field">Building' + (needsBuilding ? "" : " (optional)") +
        '<select id="rm-building"><option value="">None</option>' + opts + "</select></label>" : "") +
      '<div class="rm-sheet__actions"><button type="button" class="rm-btn rm-btn--danger" id="rm-del">Delete</button>' +
      '<button type="button" class="rm-btn rm-btn--primary" id="rm-done">Done</button></div>';
    sheet.hidden = false;
    var name = document.getElementById("rm-name"), bsel = document.getElementById("rm-building");
    name.addEventListener("input", function () { item.name = name.value; setDirty(true); });
    if (bsel) { bsel.addEventListener("change", function () { item.building = bsel.value; setDirty(true); drawPoint(item); }); }
    document.getElementById("rm-done").addEventListener("click", function () {
      if (type === "point") { drawPoint(item); }
      closeSheet();
    });
    document.getElementById("rm-del").addEventListener("click", function () {
      var list = type === "point" ? data.points : data.paths;
      list.splice(list.indexOf(item), 1);
      map.removeLayer(layers[item.id]); delete layers[item.id];
      setDirty(true); closeSheet();
    });
  }

  // --- load and save -------------------------------------------------------------------------------------------------
  fetch("/_rounds/map.json", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
    data = { points: d.points || [], paths: d.paths || [] };
    redraw();
    var all = data.points.map(function (p) { return [p.lat, p.lon]; });
    data.paths.forEach(function (q) { all = all.concat(q.coords); });
    if (all.length) { map.fitBounds(all, { padding: [40, 40], maxZoom: 18 }); }
    say(d.updated ? "Saved " + new Date(d.updated * 1000).toLocaleString() + (d.updated_by ? " by " + d.updated_by : "") : "Nothing drawn yet");
    setTool("select");
  }).catch(function () { say("Couldn't load the map"); });

  saveBtn.addEventListener("click", function () {
    if (line) { finishLine(); }
    saveBtn.disabled = true; say("Saving…");
    fetch("/_rounds/map.json", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": boot.csrf },
      body: JSON.stringify(data) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { say(res.j.error || "Couldn't save"); saveBtn.disabled = false; return; }
        data = { points: res.j.points, paths: res.j.paths };
        redraw(); setTool(tool); dirty = false;
        say("Saved. Rounds uses it now.");
      })
      .catch(function () { say("Couldn't save: check the connection"); saveBtn.disabled = false; });
  });
  window.addEventListener("beforeunload", function (e) { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { if (line) { cancelLine(); } else { closeSheet(); } }
    if (e.key === "Enter" && line && document.activeElement === document.body) { finishLine(); }
  });
})();
