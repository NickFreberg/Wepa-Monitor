// Escape closes whichever side panel is open (notifications or "What this means").
document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape") { return; }
  ["xdrawer-close", "drawer-close"].forEach(function (id) {
    var panel = document.getElementById(id === "xdrawer-close" ? "xdrawer" : "drawer");
    var btn = document.getElementById(id);
    if (panel && btn && panel.classList.contains("is-open")) { btn.click(); }
  });
});
