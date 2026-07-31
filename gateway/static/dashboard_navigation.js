(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DashboardNavigation = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function panelFromSearch(search, allowedPanels, fallback = "settings") {
    const requested = new URLSearchParams(search || "").get("panel");
    return requested && allowedPanels.has(requested) ? requested : fallback;
  }

  function createDashboardNavigation({window, panels, links, title, fallback = "settings"}) {
    const allowedPanels = new Set(panels.map((panel) => panel.id.replace(/^panel-/, "")));
    const usableFallback = allowedPanels.has(fallback) ? fallback : allowedPanels.values().next().value;

    function normalizePanel(name) {
      return allowedPanels.has(name) ? name : usableFallback;
    }

    function showPanel(name, {writeHistory = false} = {}) {
      const selected = normalizePanel(name);
      panels.forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${selected}`));
      links.forEach((link) => {
        const active = link.dataset.panel === selected;
        link.classList.toggle("active", active);
        if (active) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
      });
      const activeLink = links.find((link) => link.dataset.panel === selected);
      if (title && activeLink) title.textContent = activeLink.textContent.trim();
      if (writeHistory) {
        const url = new URL(window.location.href);
        url.searchParams.set("panel", selected);
        window.history.pushState({panel: selected}, "", url.toString());
      }
      return selected;
    }

    function start() {
      links.forEach((link) => {
        if (!allowedPanels.has(link.dataset.panel)) return;
        link.addEventListener("click", (event) => {
          event.preventDefault();
          showPanel(link.dataset.panel, {writeHistory: true});
        });
      });
      window.addEventListener("popstate", () => {
        showPanel(panelFromSearch(window.location.search, allowedPanels, usableFallback));
      });
      showPanel(panelFromSearch(window.location.search, allowedPanels, usableFallback));
    }

    return {showPanel, start};
  }

  return {createDashboardNavigation, panelFromSearch};
});
