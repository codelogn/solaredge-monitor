// Where each number on the dashboard comes from. Three sources, one colour
// each, used on every card, column and chart so nothing is ambiguous:
//   inv  — read straight from the inverter over Modbus on the LAN (real time)
//   se   — downloaded from SolarEdge's servers (as the inverter uploaded it;
//          can be hours late when the inverter's uploads fall behind)
//   calc — calculated by our own Python code, from one of the above
const SOURCES = {
  inv: {
    label: "Inverter",
    title: "Read directly from the inverter over Modbus on your home network — real time, no internet involved",
  },
  se: {
    label: "SolarEdge",
    title: "Downloaded from SolarEdge's servers, as the inverter uploaded it — the same data the SolarEdge app shows, and it can be hours late",
  },
  calc: {
    label: "Computed",
    title: "Calculated by our Python code (src/analysis.py, webapp/app.py) — not a number SolarEdge or the inverter reports",
  },
};

// Pill for a card label or heading. `from` names the raw data a computed
// figure was derived from, e.g. src("calc", "inv") -> "Computed · Inverter data".
function src(kind, from) {
  const s = SOURCES[kind];
  const suffix = from ? ` · ${SOURCES[from].label} data` : "";
  const title = from ? `${s.title}, from ${SOURCES[from].label} data` : s.title;
  return `<span class="src ${kind}" title="${title}">${s.label}${suffix}</span>`;
}

function srcLegend() {
  return `<div class="src-legend">
    <b>Where each number comes from:</b>
    ${src("inv")} read directly from the inverter on your LAN, in real time
    ${src("se")} from SolarEdge's servers — same as the SolarEdge app, can lag hours
    ${src("calc")} calculated by our Python code
    <span class="muted">· table columns are underlined in the same colours · hover any tag for details</span>
    <br><span class="acc" style="vertical-align:baseline;font-size:12px">*</span> <span class="muted">Figures marked with an asterisk may not be exact — hover it, or see the accuracy notes at the bottom of the page.</span>
  </div>`;
}

// Figures that may not be 100% accurate, and why. Each is marked in the page
// with acc(n) — an asterisk and number, hover for the short reason — and
// explained in full by accKey() at the bottom of the page. Numbers are fixed
// across pages so "*6" always means the same caveat.
const ACCURACY = {
  1: {
    title: "Instant snapshot",
    short: "one instant, not an average — output swings a lot between readings",
    long: `SolarEdge's per-optimizer voltage, current and power are a single instant, taken every
      3–6 minutes on each optimizer's own schedule. Under moving cloud, two panels read a minute
      apart can see different light, and readings near 0 A are common. Checked against the
      inverter's own DC power, those near-zero readings are real — averaged over time, the
      optimizers' readings match what the inverter measures — so they're kept, but no single
      reading says much about a panel. Judge it on averages over time.`,
  },
  2: {
    title: "Delivery lag",
    short: "only as current as the inverter's last successful upload to SolarEdge",
    long: `Everything from SolarEdge is only as current as the inverter's last successful upload.
      When the inverter's network link is weak or drops packets, the uploads can
      fall hours behind — see <b>Cloud Delay</b> — and per-panel data for that stretch may arrive
      late or never. The SolarEdge app has the same lag.`,
  },
  3: {
    title: "Daylight avg / median / max / near 0",
    short: "from the individual readings (*1) in the scored daylight windows",
    long: `Computed from every individual reading (*1) in the scored daylight windows. The average is
      the figure checked against the inverter. The maximum depends on which moments happened to be
      sampled. Median replaces the minimum, which was ~0 for nearly every panel and told them apart
      not at all. "Near 0" is the share of those readings below 0.2 A — how much of the good-light
      time the panel spent producing almost nothing. It leans higher on weaker panels, but only
      loosely; use it as supporting evidence, not on its own.`,
  },
  4: {
    title: "Verdict / vs Peers",
    short: "relative to its peers (whole array unless PANEL_GROUPS is set); can't tell shade from a fault on its own",
    long: `Each optimizer is compared with the median of its peers in the same 15-minute window — the
      whole array by default, or its group if PANEL_GROUPS defines groups (e.g. by roof face). Against
      the whole array, panels on a poorly facing roof read low for that reason alone; 100% means "like
      its peers", not "healthy". Low output can
      be shade, dirt, orientation or a fault — the data alone can't separate them (the hourly
      profile helps; a site check settles it). When afternoon uploads stall (*2) afternoons are
      under-represented. Always read the Confidence column.`,
  },
  5: {
    title: "Per-report energy — shown, not used",
    short: "not real watt-hours, and doesn't track the inverter well",
    long: `SolarEdge's per-report energy figure covers overlapping windows, so it isn't an amount of
      energy, and checked against the inverter it scattered about three times as much as the power
      readings do. It's shown for completeness; nothing on this dashboard is computed from it.`,
  },
  6: {
    title: "Sampled peak",
    short: "highest 30-second reading we captured; true peak may be higher",
    long: `The highest of the inverter readings we actually captured, every 30 seconds. Reads fail
      when the inverter's link drops packets (see <b>Modbus Reads</b>), and output can spike
      between samples under broken cloud, so the true peak may have been somewhat higher.`,
  },
  7: {
    title: "Inverter's built-in meter",
    short: "not a revenue-grade meter; power can be minutes old when reads fail",
    long: `Power and energy come from the inverter's own metering, which is not revenue-grade — expect
      it to differ from your utility's meter by a few percent. "DC Power now" is the last read that
      succeeded; its age is shown under it and can be several minutes when reads are failing.
      Readings where the inverter answered with an old snapshot (its lifetime counter went
      backwards) are discarded.`,
  },
  8: {
    title: "SolarEdge layout count",
    short: "SolarEdge's layout count — can include replaced optimizers",
    long: `This is the count in SolarEdge's site layout, which keeps listing optimizers after they
      are physically replaced. List replaced serials in <code>DECOMMISSIONED_SERIALS</code> to
      exclude them from scoring; they still appear in this count.`,
  },
  9: {
    title: "Chart gaps",
    short: "lines are drawn across short gaps; failed reads can hide dips",
    long: `The power line is drawn straight across gaps of up to 5 minutes, and failed Modbus reads
      leave gaps, so short dips or spikes may not appear. Longer gaps show as breaks.`,
  },
  10: {
    title: "Gaps and data age",
    short: "can be SolarEdge delivery stalling, not this optimizer",
    long: `A gap or old "fresh data" can mean the inverter's uploads to SolarEdge stalled (*2), which
      hits every optimizer at the same time — not that this optimizer stopped. Compare with other
      optimizers and the Cloud Delay chart on the dashboard before blaming the unit.`,
  },
  11: {
    title: "Hour-by-hour profile",
    short: "few samples per hour; thin afternoons",
    long: `Each hour's figure is the median of only a handful of 15-minute windows per day of data,
      so hours with few samples swing a lot. Afternoons are thin when uploads stall (*2). Look for
      patterns repeated over several days rather than single hours.`,
  },
  12: {
    title: "Sampled average",
    short: "time-weighted average of the readings we captured while producing",
    long: `Average DC power while the inverter was producing (at least 50 W), weighted by time
      between the 30-second readings we captured. Stretches of more than 10 minutes without a
      successful read are left out rather than guessed, so on a day with many failed reads the
      average reflects the parts of the day we actually saw.`,
  },
  13: {
    title: "Partial day",
    short: "Modbus didn't see the whole production day — energy and averages undercount",
    long: `Marked when inverter readings didn't cover the start or the end of that day's production —
      typically the day Modbus was first set up, or a long outage of the inverter's network link.
      That day's energy, average and hours only cover the part that was seen. Days are summarised
      from the raw readings, which are kept for <code>INVERTER_RETENTION_DAYS</code> (365 by
      default); the per-day summaries themselves are kept indefinitely.`,
  },
  14: {
    title: "Per-panel daily energy",
    short: "measured = delivered readings only; estimated = inverter's energy shared out",
    long: `Per-panel data only reaches us through SolarEdge, so two figures are kept for each panel and
      day. <b>Measured</b> adds up the panel's own power readings over the time they cover — exact for
      what arrived, but short on days when stretches never did. <b>Estimated</b> takes the inverter's
      measured energy for the day and shares it out by each panel's measured portion, so the panels add
      up to what the inverter really produced; it assumes each panel's share during the missing hours
      matched the delivered ones, which isn't true for a panel shaded only then. No estimate is made on
      days Modbus didn't see in full. <b>Coverage</b> says how much of the producing day had this
      panel's data — below ~90%, compare panels on that day with care.`,
  },
  15: {
    title: "Hourly ranking",
    short: "only hours with every panel's data are ranked; close finishes are near-ties",
    long: `Panels are ranked by the energy their own readings add up to within each clock hour. An hour
      is ranked only if every panel has data in all of its 15-minute windows — otherwise the panel with
      missing data would lose unfairly — so on days SolarEdge's uploads stall, fewer hours count.
      Within one hour, readings from different panels are minutes apart and the best panels often finish
      within 1–2% of each other, so who "wins" a close hour is close to a coin toss; top-3 finishes and
      average rank are steadier. Whole-array ranks mostly reflect roof placement; ranks within
      roof-face groups (set in PANEL_GROUPS) cancel that out and are the better pointer to shade or a fault.`,
  },
};

function acc(n) {
  const a = ACCURACY[n];
  return `<span class="acc" title="*${n} ${a.title}: ${a.short} — see note ${n} at the bottom of the page">*${n}</span>`;
}

function accKey(ids) {
  return `<section class="acc-key" id="accKey">
    <h2><span><span class="acc-star">*</span> Accuracy notes — figures marked with an asterisk</span></h2>
    <dl>${ids.map(n => `<dt>*${n} ${ACCURACY[n].title}</dt><dd>${ACCURACY[n].long}</dd>`).join("")}</dl>
  </section>`;
}

// Replaces static placeholders once at startup:
//   <span data-src="se" data-from="..."></span>  -> source tag
//   <span data-acc="4"></span>                   -> accuracy marker
function fillMarkers() {
  document.querySelectorAll("[data-src]").forEach(el => {
    el.outerHTML = src(el.dataset.src, el.dataset.from);
  });
  document.querySelectorAll("[data-acc]").forEach(el => {
    el.outerHTML = acc(Number(el.dataset.acc));
  });
}

// ---------------------------------------------------------------------------
// Live data health: is each source trustworthy *right now*? Fed by
// /api/inverter. The two sources fail in different ways, so they are judged
// separately:
//   Modbus (LAN)      — reads fail outright; totals stay correct because the
//                       inverter's lifetime counter catches up after a gap.
//   SolarEdge (cloud) — data arrives late or a stretch never arrives; values
//                       that do arrive are correct and carry their own
//                       measurement time.

const SE_LAG_WARN_MIN = 15;    // normal delivery is ~2 min
const SE_LAG_BAD_MIN = 60;

function fmtAge(minutes) {
  if (minutes == null) return "—";
  return minutes < 60 ? `${Math.round(minutes)} min` : `${(minutes / 60).toFixed(1)} h`;
}

// Clock time in the site's timezone (not the viewer's), matching the charts.
function localTime(iso, tz) {
  return iso ? new Date(iso).toLocaleTimeString([], { timeZone: tz, hour: "2-digit", minute: "2-digit" }) : "—";
}

function assessHealth(d) {
  // Modbus
  let modbus;
  const reads = d.reads_last_hour || 0, fails = d.failures_last_hour || 0;
  const okPct = reads + fails ? Math.round(100 * reads / (reads + fails)) : null;
  if (!d.modbus_configured) {
    modbus = { state: "NOT CONFIGURED", cls: "muted",
      text: "set INVERTER_MODBUS_HOST to read the inverter directly" };
  } else if (!d.latest || d.latest_age_seconds > 600) {
    modbus = { state: "UNAVAILABLE", cls: "bad",
      text: `no successful read for ${d.latest ? fmtAge(d.latest_age_seconds / 60) : "a while"} — inverter unreachable on the LAN` };
  } else if (okPct !== null && okPct < 50) {
    modbus = { state: "UNRELIABLE", cls: "bad",
      text: `only ${okPct}% of reads succeeded in the last hour — "now" figures can be minutes old; energy totals are still correct` };
  } else if (okPct !== null && okPct < 90) {
    modbus = { state: "DEGRADED", cls: "warn",
      text: `${okPct}% of reads succeeded in the last hour — some gaps; totals still correct` };
  } else {
    modbus = { state: "LIVE", cls: "ok", text: "reading the inverter directly, in real time" };
  }

  // SolarEdge
  let se;
  const age = d.solaredge_age_minutes;
  if (age == null) {
    se = { state: "NO DATA", cls: "muted", text: "nothing received from SolarEdge yet today" };
  } else if (d.inverter_producing === false) {
    // Not producing now: judge by whether SolarEdge's data reaches the time
    // the inverter stopped producing today, not by its age.
    const missing = d.solaredge_behind_production_minutes;
    if (missing != null && missing > SE_LAG_WARN_MIN) {
      se = { state: `MISSING ${fmtAge(missing)}`, cls: missing > SE_LAG_BAD_MIN ? "bad" : "warn",
        text: `the inverter produced until ${localTime(d.last_producing_at, d.timezone)}, but per-panel data
          stops at ${localTime(d.newest_solaredge_measurement, d.timezone)} — ${fmtAge(missing)} of today hasn't
          arrived from SolarEdge. It may still upload overnight, or may never arrive` };
    } else {
      se = { state: "IDLE", cls: "muted",
        text: "inverter isn't producing, so optimizers aren't reporting — normal after dark; today's data is complete" };
    }
  } else if (age > SE_LAG_BAD_MIN) {
    se = { state: `BEHIND ${fmtAge(age)}`, cls: "bad",
      text: `newest per-panel data is ${fmtAge(age)} old — the inverter's uploads are falling behind. What arrives is still correct; recent hours are missing until it catches up, and some may never arrive` };
  } else if (age > SE_LAG_WARN_MIN) {
    se = { state: `LAGGING ${fmtAge(age)}`, cls: "warn",
      text: `newest per-panel data is ${fmtAge(age)} old (normal is ~2 min)` };
  } else {
    se = { state: "CURRENT", cls: "ok", text: `newest per-panel data is ${fmtAge(age)} old` };
  }
  return { modbus, se };
}

function healthStrip(d, link = "#dataSources") {
  const h = assessHealth(d);
  return `<div class="health-strip">
    <div class="health-item">
      <div class="health-head">Inverter data ${src("inv")} <span class="muted">over your LAN (Modbus)</span></div>
      <span class="badge ${h.modbus.cls}">${h.modbus.state}</span>
      <span class="health-text">${h.modbus.text}</span>
    </div>
    <div class="health-item">
      <div class="health-head">Per-panel data ${src("se")} <span class="muted">over the internet</span></div>
      <span class="badge ${h.se.cls}">${h.se.state}</span>
      <span class="health-text">${h.se.text}</span>
    </div>
    <a class="health-link" href="${link}">Which source to trust, and when ↓</a>
  </div>`;
}

// Warning line for sections built on SolarEdge data; empty when it's current.
function seLagBanner(d) {
  const { se } = assessHealth(d);
  if (se.cls !== "bad" && se.cls !== "warn") return "";
  const behind = d.inverter_producing === false
    ? `SolarEdge per-panel data stops at ${localTime(d.newest_solaredge_measurement, d.timezone)}; the inverter produced until ${localTime(d.last_producing_at, d.timezone)}.`
    : `SolarEdge data is ${fmtAge(d.solaredge_age_minutes)} behind.`;
  return `<div class="lag-banner ${se.cls}">⚠ <b>${behind}</b>
    Figures here show the panels as they were then, not now. Verdicts are still valid — every
    reading is analysed at the time it was measured — but the missing hours aren't counted
    until they arrive.</div>`;
}

// ---------------------------------------------------------------------------
// Site menu: one definition, inserted at the top of every page that loads
// this script, with the current page highlighted. The optimizer detail page
// counts as part of "Panel history".
const NAV = [
  { href: "/",            label: "Dashboard",        icon: "▦", pages: ["", "index.html"] },
  { href: "inverter.html", label: "Inverter history", icon: "⚡", pages: ["inverter.html"] },
  { href: "panels.html",   label: "Panel history",    icon: "▤", pages: ["panels.html", "optimizer.html"] },
  { href: "hourly.html",   label: "Hourly ranking",   icon: "🏆", pages: ["hourly.html"] },
];

function renderNav() {
  const page = location.pathname.split("/").pop();
  const links = NAV.map(n => {
    const active = n.pages.includes(page);
    return `<a href="${n.href}" class="nav-link${active ? " active" : ""}"${active ? ' aria-current="page"' : ""}>
      <span class="nav-icon" aria-hidden="true">${n.icon}</span>${n.label}</a>`;
  }).join("");
  document.body.insertAdjacentHTML("afterbegin", `
    <nav class="site-nav" aria-label="Main">
      <a href="/" class="nav-brand"><span aria-hidden="true">☀</span> SolarMonitor</a>
      <div class="nav-links">${links}</div>
    </nav>`);
}
document.addEventListener("DOMContentLoaded", renderNav);
