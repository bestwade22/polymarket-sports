/* NBA Polymarket Bot dashboard.
 *
 * The API base URL is read from a query parameter so the same static files
 * work against a local server or a deployed API Gateway:
 *
 *   index.html?api=http://127.0.0.1:8000/dashboard
 *
 * With no parameter the page tries, in order:
 *   1. ?api=
 *   2. window.API_BASE (set by an inline script or a config.js)
 *   3. "/dashboard"  (the API Gateway path)
 */

(function () {
  "use strict";

  var REFRESH_MS = 20000;

  function apiBase() {
    var params = new URLSearchParams(window.location.search);
    var fromQuery = params.get("api");
    if (fromQuery) return fromQuery;
    if (window.API_BASE) return window.API_BASE;
    return "/dashboard";
  }

  function etToday() {
    // The bot's dates are ET dates, so the default date must be the ET today.
    var now = new Date();
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "America/New_York",
      year: "numeric", month: "2-digit", day: "2-digit"
    }).formatToParts(now);
    var map = {};
    parts.forEach(function (p) { map[p.type] = p.value; });
    return map.year + "-" + map.month + "-" + map.day;
  }

  function fmtTime(iso) {
    if (!iso) return "-";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var et = new Intl.DateTimeFormat("en-GB", {
      timeZone: "America/New_York",
      hour: "2-digit", minute: "2-digit", hour12: false
    }).format(d);
    var hkt = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Hong_Kong",
      hour: "2-digit", minute: "2-digit", hour12: false
    }).format(d);
    return et + " ET / " + hkt + " HKT";
  }

  function fmtPrice(v) {
    if (v === null || v === undefined || v === "") return "-";
    var n = Number(v);
    if (isNaN(n)) return String(v);
    return n.toFixed(3);
  }

  function fmtPnl(v) {
    if (v === null || v === undefined) return "-";
    var n = Number(v);
    if (isNaN(n)) return String(v);
    return (n >= 0 ? "+" : "") + n.toFixed(3);
  }

  function signClass(v) {
    var n = Number(v);
    if (isNaN(n) || n === 0) return "";
    return n > 0 ? "pos" : "neg";
  }

  function pill(status) {
    var cls = String(status || "unknown").toLowerCase();
    if (["scheduled", "armed", "triggered", "traded", "simulated", "skipped", "filled"].indexOf(cls) === -1) {
      cls = "scheduled";
    }
    return '<span class="pill ' + cls + '">' + cls + "</span>";
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function el(id) { return document.getElementById(id); }

  function renderSummary(summary) {
    summary = summary || {};
    el("s-games").textContent = summary.game_count != null ? summary.game_count : "-";
    el("s-armed").textContent = summary.armed_count != null ? summary.armed_count : "-";
    el("s-triggered").textContent = summary.triggered_count != null ? summary.triggered_count : "-";
    el("s-traded").textContent = summary.traded_count != null ? summary.traded_count : "-";
    el("s-trades").textContent = summary.trade_count != null ? summary.trade_count : "-";

    var realised = el("s-realised");
    realised.textContent = fmtPnl(summary.realised_pnl);
    realised.className = "stat-value " + signClass(summary.realised_pnl);

    var mtm = el("s-mtm");
    mtm.textContent = fmtPnl(summary.mark_to_market);
    mtm.className = "stat-value " + signClass(summary.mark_to_market);

    var total = el("s-total");
    total.textContent = fmtPnl(summary.total_pnl);
    total.className = "stat-value " + signClass(summary.total_pnl);
  }

  function renderGames(games) {
    var tbody = document.querySelector("#games-table tbody");
    tbody.innerHTML = "";
    if (!games || !games.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No games for this date</td></tr>';
      return;
    }
    games.forEach(function (g) {
      var prices = g.pre_game_prices || {};
      var pre = Object.keys(prices).map(function (team) {
        var p = prices[team];
        return escapeHtml(team) + " " + fmtPrice(p.mid);
      }).join(" &middot; ");

      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + fmtTime(g.start_time) + "</td>" +
        "<td>" + escapeHtml(g.matchup) + "</td>" +
        '<td class="muted">' + escapeHtml(g.away_record || "") + " / " + escapeHtml(g.home_record || "") + "</td>" +
        "<td>" + (pre || "-") + "</td>" +
        "<td>" + escapeHtml(g.monitored_team || "-") + "</td>" +
        '<td class="num">' + fmtPrice(g.trigger_threshold) + "</td>" +
        '<td class="num">' + fmtPrice(g.trigger_price) + "</td>" +
        "<td>" + pill(g.status) + "</td>";
      tbody.appendChild(tr);
    });
  }

  function renderTrades(trades) {
    var tbody = document.querySelector("#trades-table tbody");
    tbody.innerHTML = "";
    if (!trades || !trades.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">No trades yet</td></tr>';
      return;
    }
    trades.forEach(function (t) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + fmtTime(t.ts ? new Date(t.ts * 1000).toISOString() : null) + "</td>" +
        "<td>" + escapeHtml(t.matchup || t.slug) + "</td>" +
        "<td>" + escapeHtml(t.team || "-") + "</td>" +
        '<td class="num">' + fmtPrice(t.buy_price) + "</td>" +
        '<td class="num">' + fmtPrice(t.sell_price) + "</td>" +
        '<td class="num">' + escapeHtml(t.filled_size || t.size || "-") + "</td>" +
        '<td class="num">' + fmtPrice(t.implied_edge) + "</td>" +
        '<td class="num ' + signClass(t.pnl) + '">' + fmtPnl(t.pnl) + "</td>" +
        "<td>" + pill(t.status) + "</td>";
      tbody.appendChild(tr);
    });
  }

  function renderLogs(logs) {
    var box = el("log");
    box.innerHTML = "";
    if (!logs || !logs.length) {
      box.innerHTML = '<div class="empty">No activity</div>';
      return;
    }
    logs.slice().reverse().forEach(function (l) {
      var row = document.createElement("div");
      row.className = "log-row";
      var time = l.ts ? new Date(l.ts * 1000).toISOString().substring(11, 19) : "--:--:--";
      row.innerHTML =
        '<span class="log-time">' + time + "</span>" +
        '<span class="log-slug">' + escapeHtml(l.slug || "") + "</span>" +
        "<span>" + escapeHtml(l.message || "") + "</span>";
      box.appendChild(row);
    });
  }

  function setStatus(ok, message) {
    var dot = el("status-dot");
    dot.className = "dot " + (ok ? "ok" : "err");
    el("footer-note").textContent = message;
  }

  function refresh() {
    var date = el("date-input").value || etToday();
    var url = apiBase() + "?date=" + encodeURIComponent(date);
    if (el("live-toggle").checked) url += "&live=1";

    fetch(url)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        renderSummary(data.summary);
        renderGames(data.games);
        renderTrades(data.trades);
        renderLogs(data.logs);
        var cfg = data.config || {};
        setStatus(true,
          "Updated " + new Date().toLocaleTimeString() +
          " | date " + data.date +
          " | drop ratio " + cfg.price_drop_ratio +
          " | " + (cfg.dry_run ? "DRY RUN" : "LIVE"));
      })
      .catch(function (err) {
        setStatus(false, "Failed to load: " + err.message);
      });
  }

  function init() {
    el("date-input").value = etToday();
    el("refresh-btn").addEventListener("click", refresh);
    el("live-toggle").addEventListener("change", refresh);
    el("date-input").addEventListener("change", refresh);
    refresh();
    setInterval(refresh, REFRESH_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
