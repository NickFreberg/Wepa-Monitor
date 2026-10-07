// Rounds map editor (administrator only). Built for a phone: tap a tool, tap the map.
//   Pins: Home Base, Door (optionally accessible), Printer, Parking space, Van location (one per team),
//         Fuel station (one), Supply closet.
//   Lines: Walk path (both ways), Van route (both ways), One-way van route (the direction you draw it).
//   Eraser: tap any pin or line to remove it; Undo erase brings it back; Clear… removes a whole kind at once.
// The line tool snaps each tap to a nearby pin or line end; tapping a pin after the first point finishes the
// line there. Saves go to /_rounds/map.json with the page's CSRF token.
(function () {
  "use strict";
  var boot = JSON.parse(document.getElementById("rm-boot").textContent);
  var TEAMS = boot.teams || ["ResNet", "IT Service Center"];
  var SNAP_PX = 30;
  var KINDS = {
    home: { label: "Home Base", color: "#1a7f37", team: true },
    door: { label: "Door", color: "#8a1c24", building: true },
    printer: { label: "Printer", color: "#5b3fa0", building: true },
    parking: { label: "Parking space", color: "#c25e00", building: true, team: true },
    van: { label: "Van location", color: "#b4470b", team: true, oneTeam: true },
    fuel: { label: "Fuel station", color: "#444", single: true },
    closet: { label: "Supply closet", color: "#0e6f74", building: true },
    walk: { label: "Walk path", color: "#1f5fae", line: true },
    van_route: { label: "Van route", color: "#e07a10", line: true, saveAs: "van" },
    van_oneway: { label: "One-way van route", color: "#c2410c", line: true }
  };
  var ADA = "#1565c0";
  var ICONS = {
    select: '<path d="M5 3l14 8-6 2 4 7-3 1-4-7-5 4z"/>',
    erase: '<path d="M20 20H9L4 15a2 2 0 0 1 0-3l9-9a2 2 0 0 1 3 0l5 5a2 2 0 0 1 0 3l-8 9"/><path d="M9 8l7 7"/>',
    home: '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    door: '<path d="M5 21h14M7 21V3h10v18M14 12h.01"/>',
    ada: '<circle cx="12" cy="4" r="2" fill="currentColor"/><path d="M12 7v6h5l2 5"/><path d="M9.5 10.5a5 5 0 1 0 6.3 7.3"/>',
    printer: '<path d="M7 9V3h10v6"/><rect x="3" y="9" width="18" height="8" rx="2"/><path d="M7 14h10v7H7z"/>',
    parking: '<rect x="4" y="3" width="16" height="18" rx="3"/><path d="M10 17V7h3.5a3 3 0 0 1 0 6H10"/>',
    van: '<path d="M2 16V8a2 2 0 0 1 2-2h11l5 5v5h-2M2 16h2m4 0h6"/><circle cx="6" cy="16.5" r="2"/><circle cx="16" cy="16.5" r="2"/><path d="M15 6v5h5"/>',
    fuel: '<path d="M4 21V5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v16M3 21h13M4 10h11"/><path d="M15 8l3 3v6a1.5 1.5 0 0 0 3 0V9l-3-3"/>',
    closet: '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
    walk: '<circle cx="13" cy="4" r="2"/><path d="M9 21l2-7 3 3v5M7 12l3-4 4 1 3 3M11 14l-1-5"/>',
    van_route: '<path d="M3 17l5-9 4 5 3-3 6 7"/><circle cx="3" cy="17" r="1.5"/><circle cx="21" cy="17" r="1.5"/>',
    van_oneway: '<path d="M3 12h15M13 6l6 6-6 6"/>'
  };
  function svg(name, color, size) {
    return '<svg viewBox="0 0 24 24" width="' + (size || 22) + '" height="' + (size || 22) + '" fill="none" stroke="' +
      (color || "currentColor") + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + ICONS[name] + "</svg>";
  }
  document.querySelectorAll(".rm-tools button").forEach(function (b) {
    b.querySelector(".rm-ico").outerHTML = svg(b.dataset.tool);
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
  var tool = "select", dirty = false, line = null, seq = Date.now() % 100000, erased = [];
  var statusEl = document.getElementById("rm-status"), saveBtn = document.getElementById("rm-save");
  var hint = document.getElementById("rm-hint"), sheet = document.getElementById("rm-sheet");
  var lineBar = document.getElementById("rm-line"), eraseBar = document.getElementById("rm-erasebar");
  var unerase = document.getElementById("rm-unerase");

  function newId(prefix) { seq += 1; return prefix + seq.toString(36); }
  function setDirty(v) { dirty = v; saveBtn.disabled = !v; if (v) { say("Unsaved changes"); } }
  function say(t) { statusEl.textContent = t; }
  function esc(t) { return String(t || "").replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";"; }); }
  function lineKind(q) { return q.kind === "van" ? "van_route" : q.kind; }

  function nearestBuilding(ll) {
    var best = null, bd = Infinity;
    boot.buildings.forEach(function (b) {
      var d = map.distance(ll, [b.lat, b.lon]);
      if (d < bd) { bd = d; best = b.name; }
    });
    return best || "";
  }
  function pinLabel(p) {
    var k = KINDS[p.kind];
    var who = p.team ? " (" + p.team + ")" : "";
    return (p.kind === "door" && p.accessible ? "Accessible door" : k.label) + who +
      (p.name ? ": " + p.name : p.building ? ": " + p.building : "");
  }

  // --- drawing ---------------------------------------------------------------------------------------------
  function pointIcon(p) {
    var ada = p.kind === "door" && p.accessible;
    var color = ada ? "#ffffff" : KINDS[p.kind].color;
    return L.divIcon({ className: "rm-pin rm-pin--" + p.kind + (ada ? " rm-pin--ada" : ""), iconSize: [40, 40],
      iconAnchor: [20, 20],
      html: '<span style="border-color:' + (ada ? ADA : color) + (ada ? ";background:" + ADA : "") + '">' +
        svg(ada ? "ada" : p.kind, color, 22) + "</span>" });
  }
  function drawPoint(p) {
    if (layers[p.id]) { map.removeLayer(layers[p.id]); }
    var m = L.marker([p.lat, p.lon], { icon: pointIcon(p), draggable: tool === "select", keyboard: true,
      title: pinLabel(p) });
    m.on("click", function (e) { L.DomEvent.stopPropagation(e); onPointTap(p); });
    m.on("dragend", function () {
      var ll = m.getLatLng();
      p.lat = +ll.lat.toFixed(6); p.lon = +ll.lng.toFixed(6);
      setDirty(true);
    });
    m.addTo(map);
    layers[p.id] = m;
  }
  function arrows(coords, color) {
    // One-way routes: a small arrow at the middle of each segment, pointing the way the van may go.
    var out = [];
    for (var i = 1; i < coords.length; i++) {
      var a = map.latLngToLayerPoint(coords[i - 1]), b = map.latLngToLayerPoint(coords[i]);
      var mid = L.latLng((coords[i - 1][0] + coords[i][0]) / 2, (coords[i - 1][1] + coords[i][1]) / 2);
      var deg = Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI;
      out.push(L.marker(mid, { interactive: false, keyboard: false, icon: L.divIcon({ className: "rm-arrow",
        iconSize: [18, 18], iconAnchor: [9, 9],
        html: '<span style="transform:rotate(' + deg + 'deg);color:' + color + '">➤</span>' }) }));
    }
    return out;
  }
  function drawPath(q) {
    if (layers[q.id]) { map.removeLayer(layers[q.id]); }
    var k = KINDS[lineKind(q)];
    var pl = L.polyline(q.coords, { color: k.color, weight: 6, opacity: .85,
      dashArray: q.kind === "walk" ? "2 10" : null, lineCap: "round" });
    var group = L.featureGroup([pl].concat(q.kind === "van_oneway" ? arrows(q.coords, k.color) : []));
    pl.on("click", function (e) {
      if (tool === "select") { L.DomEvent.stopPropagation(e); select(q, "path"); }
      else if (tool === "erase") { L.DomEvent.stopPropagation(e); erase(q, "path"); }
    });
    group.addTo(map);
    layers[q.id] = group;
  }
  function redraw() {
    Object.keys(layers).forEach(function (id) { map.removeLayer(layers[id]); });
    layers = {};
    data.paths.forEach(drawPath);
    data.points.forEach(drawPoint);
  }
  map.on("zoomend", function () {            // arrows are placed in screen space: redraw them at the new zoom
    data.paths.forEach(function (q) { if (q.kind === "van_oneway") { drawPath(q); } });
  });

  // --- tools -------------------------------------------------------------------------------------------------
  var HINTS = {
    select: "Tap a pin or line to edit it. Drag a pin to move it.",
    erase: "Tap any pin or line to remove it. Undo erase brings it back; Clear… removes a whole kind.",
    home: "Tap a Home Base: an RSR station, the IT Service Center or the ResNet office. Rounds can start and end there.",
    door: "Tap a building entrance (Aerial view helps). Mark it accessible in the panel if it is.",
    printer: "Tap where a printer is in its building.",
    parking: "Tap a parking space for a transit van. Say whose van, or leave it shared.",
    van: "Tap where a team's van is now. Placing it again moves it.",
    fuel: "Tap the fuel station. There's only one; placing it again moves it.",
    closet: "Tap a supply closet, where consumables are kept.",
    walk: "Tap to start a walking path (usable both ways), tap along the way, then Finish.",
    van_route: "Tap to start a van route (both ways), tap along the road, then Finish.",
    van_oneway: "Tap to start a one-way van route IN THE DIRECTION OF TRAVEL, tap along the road, then Finish."
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
    eraseBar.hidden = t !== "erase";
    Object.keys(layers).forEach(function (id) {
      var l = layers[id];
      if (l.dragging) { if (t === "select") { l.dragging.enable(); } else { l.dragging.disable(); } }
    });
    document.body.classList.toggle("rm-drawing", !!(KINDS[t] && KINDS[t].line));
    document.body.classList.toggle("rm-erasing", t === "erase");
  }
  document.querySelectorAll(".rm-tools button").forEach(function (b) {
    b.addEventListener("click", function () { setTool(b.dataset.tool); });
  });

  map.on("click", function (e) {
    if (tool === "select" || tool === "erase") { closeSheet(); return; }
    var k = KINDS[tool];
    if (k.line) { addVertex(e.latlng, null); return; }
    var p = { id: newId("p"), kind: tool, lat: +e.latlng.lat.toFixed(6), lon: +e.latlng.lng.toFixed(6), name: "",
      building: k.building ? nearestBuilding(e.latlng) : "" };
    if (tool === "door") { p.accessible = false; }
    if (k.team) { p.team = tool === "van" ? TEAMS[0] : ""; }
    if (k.single) { removeWhere(function (o) { return o.kind === tool; }); }
    if (k.oneTeam) { removeWhere(function (o) { return o.kind === tool && o.team === p.team; }); }
    data.points.push(p);
    drawPoint(p);
    setDirty(true);
    select(p, "point");
  });
  function removeWhere(test) {
    data.points.filter(test).forEach(function (o) {
      data.points.splice(data.points.indexOf(o), 1);
      if (layers[o.id]) { map.removeLayer(layers[o.id]); delete layers[o.id]; }
    });
  }

  function onPointTap(p) {
    if (KINDS[tool] && KINDS[tool].line) { addVertex(L.latLng(p.lat, p.lon), p); return; }
    if (tool === "erase") { erase(p, "point"); return; }
    if (tool === "select") { select(p, "point"); }
  }

  // --- eraser ------------------------------------------------------------------------------------------------
  function erase(item, type) {
    var list = type === "point" ? data.points : data.paths;
    var at = list.indexOf(item);
    if (at < 0) { return; }
    list.splice(at, 1);
    if (layers[item.id]) { map.removeLayer(layers[item.id]); delete layers[item.id]; }
    erased.push({ item: item, type: type, at: at });
    unerase.disabled = false;
    setDirty(true);
    hint.textContent = (type === "point" ? pinLabel(item) : KINDS[lineKind(item)].label) + " removed.";
  }
  unerase.addEventListener("click", function () {
    var last = erased.pop();
    if (!last) { return; }
    var list = last.type === "point" ? data.points : data.paths;
    list.splice(Math.min(last.at, list.length), 0, last.item);
    if (last.type === "point") { drawPoint(last.item); } else { drawPath(last.item); }
    unerase.disabled = !erased.length;
    setDirty(true);
    hint.textContent = "Brought back.";
  });
  var CLEARS = [
    ["walk", "All walk paths", function (q) { return q.kind === "walk"; }, "path"],
    ["vanroutes", "All van routes (both kinds)", function (q) { return q.kind === "van" || q.kind === "van_oneway"; }, "path"],
    ["pins", "All pins except doors", function (p) { return p.kind !== "door"; }, "point"],
    ["all", "Everything except doors", null, "both"]
  ];
  document.getElementById("rm-clear").addEventListener("click", function () {
    sheet.innerHTML = '<div class="rm-sheet__head"><b>Clear</b> · removes them from this map; Save to keep it, ' +
      "or leave without saving to undo.</div>" +
      CLEARS.map(function (c) {
        return '<button type="button" class="rm-btn rm-btn--danger rm-btn--block" data-clear="' + c[0] + '">' + c[1] + "</button>";
      }).join("") + '<button type="button" class="rm-btn rm-btn--block" id="rm-clear-cancel">Cancel</button>';
    sheet.hidden = false;
    sheet.querySelectorAll("[data-clear]").forEach(function (b) {
      b.addEventListener("click", function () {
        var c = CLEARS.filter(function (x) { return x[0] === b.dataset.clear; })[0];
        var n = 0;
        if (c[3] !== "point") {
          data.paths.filter(c[2] || function () { return true; }).forEach(function (q) { erase(q, "path"); n++; });
        }
        if (c[3] !== "path") {
          data.points.filter(c[2] || function (p) { return p.kind !== "door"; }).forEach(function (p) { erase(p, "point"); n++; });
        }
        closeSheet();
        hint.textContent = n ? n + " removed. Undo erase brings them back one at a time." : "Nothing to clear.";
      });
    });
    document.getElementById("rm-clear-cancel").addEventListener("click", closeSheet);
  });

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
    var k = KINDS[tool];
    if (!line) {
      line = { kind: tool, coords: [], marks: [] };
      line.layer = L.polyline([], { color: k.color, weight: 6, opacity: .9,
        dashArray: tool === "walk" ? "2 10" : null }).addTo(map);
      lineBar.hidden = false;
    }
    line.coords.push([+at.lat.toFixed(6), +at.lng.toFixed(6)]);
    line.layer.setLatLngs(line.coords);
    line.marks.push(L.circleMarker(at, { radius: 7, color: k.color, weight: 3, fillColor: "#fff",
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
    var kind = KINDS[line.kind].saveAs || line.kind;
    var q = { id: newId("l"), kind: kind, coords: line.coords.slice(), name: "" };
    clearLine();
    data.paths.push(q);
    drawPath(q);
    setDirty(true);
    hint.textContent = KINDS[lineKind(q)].label + " added. Tap to start another, or pick a tool.";
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

  // --- the sheet: name, building, team, accessible, direction, delete --------------------------------------------
  function closeSheet() { sheet.hidden = true; sheet.innerHTML = ""; }
  function select(item, type) {
    var k = type === "path" ? KINDS[lineKind(item)] : KINDS[item.kind];
    var opts = boot.buildings.map(function (b) {
      return '<option value="' + esc(b.name) + '"' + (b.name === item.building ? " selected" : "") + ">" + esc(b.name) + "</option>";
    }).join("");
    var teams = (item.kind === "van" ? [] : [["", "Shared / either team"]]).concat(TEAMS.map(function (t) { return [t, t]; }))
      .map(function (t) { return '<option value="' + esc(t[0]) + '"' + ((item.team || "") === t[0] ? " selected" : "") + ">" + esc(t[1]) + "</option>"; }).join("");
    var html = '<div class="rm-sheet__head"><b>' + esc(k.label) + "</b>" +
      (type === "path" ? " · " + item.coords.length + " points" : "") + "</div>" +
      '<label class="rm-field">Name (optional)<input id="rm-name" maxlength="80" value="' + esc(item.name) +
      '" placeholder="' + (item.kind === "home" ? "e.g. ResNet office, ECC" : item.kind === "closet" ? "e.g. Maxwell supply closet" : "") + '"></label>';
    if (type === "point" && k.building) {
      html += '<label class="rm-field">Building<select id="rm-building"><option value="">None</option>' + opts + "</select></label>";
    }
    if (type === "point" && k.team) {
      html += '<label class="rm-field">' + (item.kind === "van" ? "Whose van" : "Team") + '<select id="rm-team">' + teams + "</select></label>";
    }
    if (item.kind === "door") {
      html += '<label class="rm-check"><input type="checkbox" id="rm-ada"' + (item.accessible ? " checked" : "") +
        "> Accessible entrance (ramp or level, automatic door)</label>";
    }
    if (item.kind === "van_oneway") {
      html += '<button type="button" class="rm-btn rm-btn--block" id="rm-flip">Reverse the direction</button>';
    }
    html += '<div class="rm-sheet__actions"><button type="button" class="rm-btn rm-btn--danger" id="rm-del">Remove</button>' +
      '<button type="button" class="rm-btn rm-btn--primary" id="rm-done">Done</button></div>';
    sheet.innerHTML = html;
    sheet.hidden = false;
    var name = document.getElementById("rm-name"), bsel = document.getElementById("rm-building");
    var tsel = document.getElementById("rm-team"), ada = document.getElementById("rm-ada");
    var flip = document.getElementById("rm-flip");
    name.addEventListener("input", function () { item.name = name.value; setDirty(true); });
    if (bsel) { bsel.addEventListener("change", function () { item.building = bsel.value; setDirty(true); drawPoint(item); }); }
    if (tsel) {
      tsel.addEventListener("change", function () {
        if (k.oneTeam) { removeWhere(function (o) { return o !== item && o.kind === item.kind && o.team === tsel.value; }); }
        item.team = tsel.value; setDirty(true); drawPoint(item);
      });
    }
    if (ada) { ada.addEventListener("change", function () { item.accessible = ada.checked; setDirty(true); drawPoint(item); }); }
    if (flip) { flip.addEventListener("click", function () { item.coords.reverse(); setDirty(true); drawPath(item); }); }
    document.getElementById("rm-done").addEventListener("click", function () {
      if (type === "point") { drawPoint(item); }
      closeSheet();
    });
    document.getElementById("rm-del").addEventListener("click", function () { erase(item, type); closeSheet(); });
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
        erased = []; unerase.disabled = true;
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
