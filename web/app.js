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
 *
 * All times shown are Hong Kong time (HKT). The bot's dates are ET dates,
 * but the reader is in Hong Kong, so every timestamp is rendered in HKT
 * and the date input selects an ET date.
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
    return inZone("America/New_York", new Date(), "date");
  }

  function inZone(timeZone, date, kind) {
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: timeZone,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false
    }).formatToParts(date);
    var map = {};
    parts.forEach(function (p) { map[p.type] = p.value; });
    if (kind === "date") {
      return map.year + "-" + map.month + "-" + map.day;
    }
    if (kind === "month") {
      return map.year + "-" + map.month;
    }
    if (kind === "hour") {
      // "24" means midnight in some locales; normalise it to "00".
      return map.hour === "24" ? "00" : map.hour;
    }
    return map.minute;
  }

  /* --- date helpers ----------------------------------------------------- */

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function parseIsoDate(text) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text || ""));
    if (!m) return null;
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  }

  function isoDate(date) {
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
  }

  function shiftDays(isoText, delta) {
    var d = parseIsoDate(isoText);
    if (!d) return isoText;
    d.setDate(d.getDate() + delta);
    return isoDate(d);
  }

  /* --- formatting ------------------------------------------------------- */

  function fmtHkt(iso) {
    if (!iso) return "-";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var day = inZone("Asia/Hong_Kong", d, "date");
    var hour = inZone("Asia/Hong_Kong", d, "hour");
    var minute = inZone("Asia/Hong_Kong", d, "minute");
    return day + " " + hour + ":" + minute;
  }

  function fmtHktFromEpoch(seconds) {
    if (seconds === null || seconds === undefined || seconds === "") return "-";
    var n = Number(seconds);
    if (isNaN(n) || n === 0) return "-";
    return fmtHkt(new Date(n * 1000));
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
    if (isNaN(n)) return "-";
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

  /* --- rendering -------------------------------------------------------- */

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

  /* --- listener health --------------------------------------------------- */

  var LISTENER_STATES = {
    running: { cls: "ok", label: "Running" },
    stale: { cls: "warn", label: "Stale" },
    stopped: { cls: "off", label: "Stopped" },
    unknown: { cls: "off", label: "Unknown" }
  };

  function setStat(id, value, note, cls) {
    var node = el(id);
    node.textContent = value;
    node.className = "stat-value" + (cls ? " " + cls : "");
    if (note !== undefined) {
      var noteNode = el(id + "-note");
      if (noteNode) noteNode.textContent = note;
    }
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "";
    var n = Number(seconds);
    if (isNaN(n)) return "";
    if (n < 60) return n + "s ago";
    if (n < 3600) return Math.floor(n / 60) + "m ago";
    return Math.floor(n / 3600) + "h " + Math.floor((n % 3600) / 60) + "m ago";
  }

  function renderListener(listener) {
    listener = listener || {};
    var state = String(listener.state || "unknown").toLowerCase();
    var meta = LISTENER_STATES[state] || LISTENER_STATES.unknown;

    var note = "";
    if (listener.last_message) note = listener.last_message;
    setStat("l-state", meta.label, note, meta.cls);

    // Scale up / down. The recorded time is the actual transition; when
    // the listener never ran, the planned window time is shown instead.
    var up = listener.scale_up_at || listener.started_ts;
    setStat(
      "l-scale-up",
      up ? fmtHktFromEpoch(up) : "-",
      up
        ? (listener.scale_up_at ? "actual scale-up" : "listener started")
        : (listener.window_start_ts ? "planned window start" : "")
    );

    var down = listener.scale_down_at;
    setStat(
      "l-scale-down",
      down ? fmtHktFromEpoch(down) : "-",
      down ? "actual scale-down" : (listener.window_end_ts ? "planned window end" : "")
    );

    var hb = listener.last_heartbeat_ts;
    setStat(
      "l-heartbeat",
      hb ? fmtHktFromEpoch(hb) : "-",
      hb ? fmtDuration(listener.heartbeat_age_seconds) : "no heartbeat yet"
    );

    var windowText = "-";
    var windowNote = "";
    if (listener.window_start_ts && listener.window_end_ts) {
      windowText = fmtHktFromEpoch(listener.window_start_ts);
      windowNote = "to " + fmtHktFromEpoch(listener.window_end_ts);
    }
    setStat("l-window", windowText, windowNote);

    var armed = listener.armed_count;
    var tokens = listener.token_count;
    setStat(
      "l-armed",
      (armed === null || armed === undefined ? "-" : armed) + " / " +
        (tokens === null || tokens === undefined ? "-" : tokens),
      listener.pid ? "pid " + listener.pid : ""
    );

    // The listener's own log lines, newest last.
    var box = el("listener-log");
    box.innerHTML = "";
    var messages = listener.messages || [];
    if (!messages.length) {
      box.innerHTML = '<div class="empty">No listener messages recorded</div>';
      return;
    }
    messages.forEach(function (m) {
      var row = document.createElement("div");
      row.className = "log-row";
      row.innerHTML =
        '<span class="log-time">' + escapeHtml(fmtHktFromEpoch(m.ts)) + "</span>" +
        "<span>" + escapeHtml(m.message || "") + "</span>";
      box.appendChild(row);
    });
  }

  function matchupCell(g) {    var text = escapeHtml(g.matchup || "");
    var url = g.polymarket_url;
    if (!url) return text;
    return '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener noreferrer"' +
      ' title="Open on Polymarket">' + text + " &#8599;</a>";
  }

  function renderGames(games) {
    var tbody = document.querySelector("#games-table tbody");
    tbody.innerHTML = "";
    if (!games || !games.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">No games for this date</td></tr>';
      return;
    }
    games.forEach(function (g) {
      var prices = g.pre_game_prices || {};
      var pre = Object.keys(prices).map(function (team) {
        var p = prices[team];
        return escapeHtml(team) + " " + fmtPrice(p.mid);
      }).join(" &middot; ");

      // The lowest bid the listener saw, and how far it fell from the
      // pre-game mid - the second is what makes the number meaningful.
      var lowest = g.lowest_bid;
      var drop = "";
      if (lowest !== null && lowest !== undefined && g.pre_game_mid) {
        var pct = (1 - Number(lowest) / Number(g.pre_game_mid)) * 100;
        drop = ' <span class="muted">(' + (pct >= 0 ? "-" : "+") + Math.abs(pct).toFixed(1) + "%)</span>";
      }

      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + fmtHkt(g.start_time) + "</td>" +
        "<td>" + matchupCell(g) + "</td>" +
        '<td class="muted">' + escapeHtml(g.away_record || "") + " / " + escapeHtml(g.home_record || "") + "</td>" +
        "<td>" + (pre || "-") + "</td>" +
        "<td>" + fmtHktFromEpoch(g.pre_game_snapshot_ts) + "</td>" +
        "<td>" + escapeHtml(g.monitored_team || "-") + "</td>" +
        '<td class="num">' + fmtPrice(lowest) + drop + "</td>" +
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
        "<td>" + fmtHktFromEpoch(t.ts) + "</td>" +
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
      var time = l.ts ? fmtHktFromEpoch(l.ts) : "-";
      row.innerHTML =
        '<span class="log-time">' + escapeHtml(time) + "</span>" +
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
        renderListener(data.listener);
        renderGames(data.games);
        renderTrades(data.trades);
        renderLogs(data.logs);
        var cfg = data.config || {};
        setStatus(true,
          "Updated " + new Date().toLocaleTimeString() +
          " | ET date " + data.date +
          " | drop ratio " + cfg.price_drop_ratio +
          " | " + (cfg.dry_run ? "DRY RUN" : "LIVE"));
      })
      .catch(function (err) {
        setStatus(false, "Failed to load: " + err.message);
      });
  }

  /* --- calendar --------------------------------------------------------- */

  var calMonth = null;   // "YYYY-MM" of the month on screen
  var calAnchor = null;  // the ET date the calendar opens on

  function monthLabel(ym) {
    var parts = ym.split("-");
    var d = new Date(Number(parts[0]), Number(parts[1]) - 1, 1);
    return d.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  }

  function renderCalendar() {
    if (!calMonth) return;
    el("cal-title").textContent = monthLabel(calMonth);

    var parts = calMonth.split("-");
    var year = Number(parts[0]);
    var month = Number(parts[1]) - 1;
    var first = new Date(year, month, 1);
    var daysInMonth = new Date(year, month + 1, 0).getDate();
    // Monday-first grid.
    var lead = (first.getDay() + 6) % 7;

    var grid = el("cal-grid");
    grid.innerHTML = "";
    ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].forEach(function (d) {
      var cell = document.createElement("div");
      cell.className = "cal-dow";
      cell.textContent = d;
      grid.appendChild(cell);
    });

    var selected = el("date-input").value;
    for (var i = 0; i < lead; i++) {
      grid.appendChild(document.createElement("div"));
    }
    for (var day = 1; day <= daysInMonth; day++) {
      (function (day) {
        var cell = document.createElement("button");
        cell.type = "button";
        cell.className = "cal-day";
        cell.textContent = String(day);
        var iso = year + "-" + pad(month + 1) + "-" + pad(day);
        if (iso === selected) cell.classList.add("selected");
        cell.addEventListener("click", function () {
          el("date-input").value = iso;
          closeCalendar();
          refresh();
        });
        grid.appendChild(cell);
      })(day);
    }
  }

  function openCalendar() {
    calAnchor = el("date-input").value || etToday();
    calMonth = calAnchor.slice(0, 7);
    el("calendar").hidden = false;
    renderCalendar();
  }

  function closeCalendar() {
    el("calendar").hidden = true;
  }

  function shiftMonth(delta) {
    var parts = calMonth.split("-");
    var d = new Date(Number(parts[0]), Number(parts[1]) - 1 + delta, 1);
    calMonth = d.getFullYear() + "-" + pad(d.getMonth() + 1);
    renderCalendar();
  }

  /* --- init ------------------------------------------------------------- */

  function init() {
    el("date-input").value = etToday();

    el("refresh-btn").addEventListener("click", refresh);
    el("live-toggle").addEventListener("change", refresh);
    el("date-input").addEventListener("change", function () {
      closeCalendar();
      refresh();
    });

    el("date-prev").addEventListener("click", function () {
      el("date-input").value = shiftDays(el("date-input").value, -1);
      refresh();
    });
    el("date-next").addEventListener("click", function () {
      el("date-input").value = shiftDays(el("date-input").value, 1);
      refresh();
    });
    el("date-today").addEventListener("click", function () {
      el("date-input").value = etToday();
      refresh();
    });

    // Clicking the date field opens the calendar; the native picker stays
    // available through the calendar button next to it.
    el("date-input").addEventListener("focus", openCalendar);
    el("cal-prev").addEventListener("click", function () { shiftMonth(-1); });
    el("cal-next").addEventListener("click", function () { shiftMonth(1); });

    document.addEventListener("click", function (ev) {
      var picker = document.querySelector(".date-picker");
      if (picker && !picker.contains(ev.target)) closeCalendar();
    });

    refresh();
    setInterval(refresh, REFRESH_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
