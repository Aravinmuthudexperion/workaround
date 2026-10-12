// ARC Tower UI behaviour: copy buttons, HTMX CSRF header, Chart.js rendering.
(function () {
  "use strict";

  // --- CSRF for HTMX requests ------------------------------------------------
  document.addEventListener("htmx:configRequest", function (evt) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) evt.detail.headers["X-CSRF-Token"] = meta.content;
  });

  // --- copy-to-clipboard -------------------------------------------------------
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest("[data-copy]");
    if (!btn) return;
    var source = document.getElementById(btn.getAttribute("data-copy"));
    if (!source) return;
    var text = source.innerText.trim();
    var done = function () {
      var old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1400);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done);
    } else {
      var area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      done();
    }
  });

  // --- charts ---------------------------------------------------------------------
  var PALETTE = ["#4c6ef5", "#12b886", "#fab005", "#fa5252", "#7950f2", "#15aabf", "#e64980", "#82c91e"];

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function formatValue(value, unit) {
    if (value === null || value === undefined) return "–";
    if (unit === "USD") return "$" + value.toFixed(value < 1 ? 3 : 2);
    if (unit === "ms") return value >= 1000 ? (value / 1000).toFixed(2) + "s" : Math.round(value) + "ms";
    if (unit === "%") return value.toFixed(1) + "%";
    if (Math.abs(value) >= 1e6) return (value / 1e6).toFixed(1) + "M";
    if (Math.abs(value) >= 1e3) return (value / 1e3).toFixed(1) + "k";
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }

  function renderPie(canvas, data) {
    var text = css("--text-2");
    if (!data.values.length) {
      canvas.parentElement.insertAdjacentHTML("beforeend",
        '<p class="faint" style="position:absolute;inset:0;display:grid;place-items:center">No data in this range yet</p>');
      return;
    }
    return new Chart(canvas, {
      type: "doughnut",
      data: { labels: data.labels, datasets: [{ data: data.values, borderWidth: 1,
              backgroundColor: data.labels.map(function (_, i) { return PALETTE[i % PALETTE.length]; }) }] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false, cutout: "55%",
        plugins: {
          legend: { position: "right", labels: { color: text, boxWidth: 10, boxHeight: 10 } },
          tooltip: { callbacks: { label: function (ctx) {
            var total = ctx.dataset.data.reduce(function (a, b) { return a + b; }, 0);
            return ctx.label + ": " + formatValue(ctx.parsed, data.unit) + " (" + Math.round(100 * ctx.parsed / total) + "%)";
          } } }
        }
      }
    });
  }

  function renderChart(canvas, data, type) {
    if (!window.Chart) return;
    if (data.kind === "pie") return renderPie(canvas, data);
    var grid = css("--border"), text = css("--text-2");
    var stacked = type === "bar";
    var chart = new Chart(canvas, {
      type: type || "line",
      data: {
        labels: data.labels.map(function (l) { return new Date(l); }),
        datasets: data.datasets.map(function (d, i) {
          var color = PALETTE[i % PALETTE.length];
          return { label: d.label, data: d.data, borderColor: color, backgroundColor: color + (stacked ? "cc" : "22"),
                   borderWidth: 2, pointRadius: 0, tension: 0.25, fill: !stacked, spanGaps: true };
        })
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom", labels: { color: text, boxWidth: 10, boxHeight: 10 } },
          tooltip: { callbacks: { label: function (ctx) {
            return ctx.dataset.label + ": " + formatValue(ctx.parsed.y, data.unit);
          } } }
        },
        scales: {
          x: { stacked: stacked, ticks: { color: text, maxTicksLimit: 8, callback: function (v) {
                 var d = new Date(this.getLabelForValue(v));
                 return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
               } }, grid: { color: grid } },
          y: { stacked: stacked, beginAtZero: true, ticks: { color: text, callback: function (v) {
                 return formatValue(v, data.unit); } }, grid: { color: grid } }
        }
      }
    });
    if (!data.datasets.length) {
      canvas.parentElement.insertAdjacentHTML("beforeend",
        '<p class="faint" style="position:absolute;inset:0;display:grid;place-items:center">No data in this range yet</p>');
    }
    return chart;
  }

  function loadCharts(root) {
    (root || document).querySelectorAll("[data-charts-src]").forEach(function (group) {
      fetch(group.getAttribute("data-charts-src"), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (all) {
          group.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
            var key = canvas.getAttribute("data-chart");
            if (all[key]) renderChart(canvas, all[key], canvas.getAttribute("data-type"));
          });
        });
    });
  }

  document.addEventListener("DOMContentLoaded", function () { loadCharts(document); });
})();
