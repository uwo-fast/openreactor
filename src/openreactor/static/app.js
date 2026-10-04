// openreactor's own behaviour, on top of the vendored htmx and Chart.js.
"use strict";

// Swap error responses too, so the operator sees why an action failed:
// the server answers them with a notice, not a bare status.
document.addEventListener("DOMContentLoaded", () => {
  htmx.config.responseHandling = [
    { code: "204", swap: false },
    { code: "...", swap: true },
  ];
  startCharts();
});

// One small chart per channel on the dashboard, from the readings seen
// since the page opened. Nothing is kept once the page is closed; the
// recorded run is in the database.
const POINTS = 300;

function startCharts() {
  const box = document.getElementById("charts");
  if (!box || typeof Chart === "undefined") return;
  const charts = new Map();

  async function poll() {
    let channels;
    try {
      const response = await fetch(box.dataset.source, { headers: { Accept: "application/json" } });
      if (response.status === 401) {
        // The session ended; the page's htmx requests take it to sign in.
        return false;
      }
      if (!response.ok) return true;
      channels = await response.json();
    } catch {
      return true;
    }
    const now = new Date().toLocaleTimeString();
    for (const c of channels) {
      let chart = charts.get(c.name);
      if (!chart && c.value === null) continue;
      if (!chart) {
        const cell = document.createElement("div");
        cell.className = "col-md-6";
        const canvas = document.createElement("canvas");
        canvas.setAttribute("role", "img");
        canvas.setAttribute("aria-label", `${c.name} over time`);
        cell.append(canvas);
        box.append(cell);
        chart = new Chart(canvas, {
          type: "line",
          data: { labels: [], datasets: [{ label: `${c.name} (${c.unit})`, data: [], pointRadius: 0, borderWidth: 1.5 }] },
          options: { animation: false, maintainAspectRatio: false, scales: { x: { ticks: { maxTicksLimit: 4 } } } },
        });
        charts.set(c.name, chart);
      }
      // A failed read is a gap in the line, not a straight join across it.
      chart.data.labels.push(now);
      chart.data.datasets[0].data.push(c.outcome === "ok" ? c.value : null);
      if (chart.data.labels.length > POINTS) {
        chart.data.labels.shift();
        chart.data.datasets[0].data.shift();
      }
      chart.update();
    }
    return true;
  }

  // The next poll is scheduled after this one finishes, so a slow answer
  // never stacks requests.
  async function loop() {
    if (await poll()) setTimeout(loop, 2000);
  }
  loop();
}
