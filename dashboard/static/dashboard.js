// MediSlot operations dashboard front end.
// All text goes into the DOM with textContent: alarm reasons, log lines and
// table names are untrusted data. Nothing here builds markup from strings.
(function () {
  'use strict';

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var REFRESH_MS = 60000;
  var CHART_HEIGHT = 232;
  var MARGIN = { l: 46, r: 62, t: 12, b: 26 };

  var state = { range: '1h', data: null, cards: [], views: {}, timer: null, lastWidth: 0 };

  // ---- DOM helpers ------------------------------------------------------
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function svg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) node.setAttribute(key, attrs[key]);
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function $(id) { return document.getElementById(id); }

  // ---- formatting -------------------------------------------------------
  function fmtCount(v) {
    if (v === null || v === undefined) return '–';
    var abs = Math.abs(v);
    if (abs >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (abs >= 1e4) return (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return Math.round(v).toLocaleString();
  }

  function fmtMs(v) {
    if (v === null || v === undefined) return '–';
    if (v >= 10000) return (v / 1000).toFixed(1) + ' s';
    return Math.round(v).toLocaleString() + ' ms';
  }

  function fmtPct(v) {
    if (v === null || v === undefined) return '–';
    return v.toFixed(1) + '%';
  }

  function fmtGb(v) {
    if (v === null || v === undefined) return '–';
    return v.toFixed(1) + ' GB';
  }

  function formatter(unit) {
    return { count: fmtCount, ms: fmtMs, percent: fmtPct }[unit] || fmtCount;
  }

  function periodLabel(seconds) {
    if (seconds === 60) return 'minute';
    return Math.round(seconds / 60) + ' minutes';
  }

  var timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
  var dayTimeFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
  var fullFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });

  function axisTime(epoch) {
    var d = new Date(epoch * 1000);
    return state.range === '24h' ? dayTimeFmt.format(d) : timeFmt.format(d);
  }

  function relTime(iso) {
    if (!iso) return '';
    var seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (seconds < 90) return 'just now';
    if (seconds < 5400) return Math.round(seconds / 60) + ' min ago';
    if (seconds < 129600) return Math.round(seconds / 3600) + ' h ago';
    return Math.round(seconds / 86400) + ' d ago';
  }

  // ---- scale ------------------------------------------------------------
  function niceScale(max, isCount) {
    if (!(max > 0)) max = isCount ? 4 : 1;
    var raw = max / 4;
    var pow = Math.pow(10, Math.floor(Math.log10(raw)));
    var frac = raw / pow;
    // Counts are whole numbers, so their ticks must be whole numbers too (no 2.5 step).
    var steps = isCount ? [1, 2, 5, 10] : [1, 2, 2.5, 5, 10];
    var step = 10 * pow;
    for (var s = 0; s < steps.length; s++) {
      if (frac <= steps[s]) { step = steps[s] * pow; break; }
    }
    if (isCount && step < 1) step = 1;
    var top = Math.ceil(max / step - 1e-9) * step;
    var ticks = [];
    for (var v = 0; v <= top + step / 1000; v += step) ticks.push(v);
    return { ticks: ticks, top: top };
  }

  // ---- stat tiles -------------------------------------------------------
  var ICONS = { good: '✓', warning: '!', critical: '✕' };

  function tile(label, value, sub, status) {
    var node = el('div', 'tile');
    node.appendChild(el('div', 'tile-label', label));
    node.appendChild(el('div', 'tile-value', value));
    var line = el('div', 'tile-sub');
    if (status) {
      var icon = el('span', 'status-icon status-' + status, ICONS[status]);
      icon.setAttribute('aria-hidden', 'true');
      line.appendChild(icon);
    }
    line.appendChild(el('span', null, sub));
    node.appendChild(line);
    return node;
  }

  function sum(values) {
    var total = 0;
    for (var i = 0; i < values.length; i++) total += values[i] || 0;
    return total;
  }

  function renderKpis(d) {
    var host = $('kpis');
    clear(host);
    var api = d.api.totals;
    var appErrors = d.lambdas.auth.totals.errors + d.lambdas.app.totals.errors;

    host.appendChild(tile('API requests', fmtCount(api.requests),
      '4xx ' + fmtCount(api.errors_4xx) + ' · 5xx ' + fmtCount(api.errors_5xx)));

    host.appendChild(tile('Server errors (5xx)', fmtCount(api.errors_5xx),
      api.errors_5xx > 0 ? 'Errors in this range' : 'None in this range',
      api.errors_5xx > 0 ? 'critical' : 'good'));

    host.appendChild(tile('API latency p99', fmtMs(api.latency_p99),
      'p50 ' + fmtMs(api.latency_p50)));

    host.appendChild(tile('Lambda errors', fmtCount(appErrors),
      appErrors > 0 ? 'Auth and app: check recent errors' : 'Auth and app: none',
      appErrors > 0 ? 'warning' : 'good'));

    var latest = d.rds.latest;
    var cpu = latest.cpu;
    var cpuStatus = cpu === null ? null : cpu >= 70 ? 'critical' : cpu >= 50 ? 'warning' : 'good';
    var cpuWord = cpu === null ? 'No data' : cpu >= 70 ? 'High' : cpu >= 50 ? 'Elevated' : 'Normal';
    host.appendChild(tile('Database CPU', fmtPct(cpu),
      cpuWord + ' · ' + (latest.connections === null ? '–' : Math.round(latest.connections)) +
      ' connections · ' + fmtGb(latest.free_storage_gb) + ' free',
      cpuStatus));

    host.appendChild(alarmTile(d.alarms));
  }

  function alarmTile(alarms) {
    if (!alarms.available) {
      return tile('Alarms', '–', 'Could not read alarms', 'warning');
    }
    var firing = alarms.items.filter(function (a) { return a.state === 'ALARM'; }).length;
    var waiting = alarms.items.filter(function (a) { return a.state === 'INSUFFICIENT_DATA'; }).length;
    if (alarms.items.length === 0) return tile('Alarms', '0', 'No alarms found', 'warning');
    if (firing > 0) return tile('Alarms firing', String(firing), 'Of ' + alarms.items.length + ' alarms', 'critical');
    if (waiting > 0) return tile('Alarms firing', '0', waiting + ' waiting for data', 'warning');
    return tile('Alarms firing', '0', 'All ' + alarms.items.length + ' OK', 'good');
  }

  // ---- chart specs ------------------------------------------------------
  // Colour follows the entity: auth is always slot 1, authorizer slot 2, app slot 3.
  function lambdaSeries(d, metric, authorizerLabel) {
    return [
      { label: 'auth', short: 'auth', cls: 's1', values: d.lambdas.auth.series[metric] },
      { label: authorizerLabel || 'authorizer', short: 'authorizer', cls: 's2', values: d.lambdas.authorizer.series[metric] },
      { label: 'app', short: 'app', cls: 's3', values: d.lambdas.app.series[metric] }
    ];
  }

  function buildSpecs(d) {
    var per = periodLabel(d.period_seconds);
    var api = d.api.series;
    return [
      { title: 'API requests', sub: 'Requests per ' + per, unit: 'count',
        series: [{ label: 'Requests', cls: 's1', values: api.requests }] },
      { title: 'API errors', sub: 'Client (4xx) and server (5xx) responses per ' + per, unit: 'count',
        series: [
          { label: '4xx client errors', short: '4xx', cls: 's1', values: api.errors_4xx },
          { label: '5xx server errors', short: '5xx', cls: 's2', values: api.errors_5xx }] },
      { title: 'API latency', sub: 'Milliseconds, 50th and 99th percentile', unit: 'ms',
        series: [
          { label: 'p50', short: 'p50', cls: 's1', values: api.latency_p50 },
          { label: 'p99', short: 'p99', cls: 's2', values: api.latency_p99 }] },
      { title: 'Lambda invocations', sub: 'Per ' + per, unit: 'count', series: lambdaSeries(d, 'invocations') },
      { title: 'Lambda errors', sub: 'Authorizer errors are rejected tokens (expected)', unit: 'count',
        series: lambdaSeries(d, 'errors', 'authorizer (rejected tokens)') },
      { title: 'Lambda duration p99', sub: 'Milliseconds', unit: 'ms', series: lambdaSeries(d, 'duration_p99') },
      { title: 'DynamoDB conditional-check failures', sub: 'Lost races on slots and seats per ' + per, unit: 'count',
        series: [
          { label: 'slots table', short: 'slots', cls: 's1', values: d.dynamodb.slots.series.conditional_check_failures },
          { label: 'seats table', short: 'seats', cls: 's2', values: d.dynamodb.seats.series.conditional_check_failures }] },
      { title: 'Database CPU', sub: 'Percent, alarm threshold 70%', unit: 'percent', threshold: 70,
        series: [{ label: 'CPU', cls: 's1', values: d.rds.series.cpu }] },
      { title: 'Database connections', sub: 'Average open connections', unit: 'count',
        series: [{ label: 'Connections', cls: 's1', values: d.rds.series.connections }] }
    ];
  }

  // ---- chart card -------------------------------------------------------
  function hasData(spec) {
    return spec.series.some(function (s) {
      return s.values.some(function (v) { return v !== null && v !== undefined; });
    });
  }

  function makeCard(spec, timestamps) {
    var fmt = formatter(spec.unit);
    var n = timestamps.length;
    var card = el('article', 'card');

    var head = el('header', 'card-head');
    var titles = el('div');
    titles.appendChild(el('h2', null, spec.title));
    titles.appendChild(el('p', 'muted', spec.sub));
    head.appendChild(titles);
    var toggle = el('button', 'btn', 'Table view');
    toggle.type = 'button';
    head.appendChild(toggle);
    card.appendChild(head);

    var plot = el('div', 'plot');
    plot.tabIndex = 0;
    plot.setAttribute('role', 'group');
    plot.setAttribute('aria-label', spec.title + ' chart. Use the arrow keys to read values, or open the table view.');
    card.appendChild(plot);

    var tableHost = el('div', 'table-scroll');
    tableHost.hidden = true;
    card.appendChild(tableHost);

    if (spec.series.length > 1) {
      var legend = el('div', 'legend');
      spec.series.forEach(function (s) {
        var item = el('span', 'legend-item');
        item.appendChild(el('span', 'key ' + s.cls));
        item.appendChild(el('span', null, s.label));
        legend.appendChild(item);
      });
      card.appendChild(legend);
    }

    var cur = n - 1;
    var geom = null;
    var parts = null;

    function buildTable() {
      clear(tableHost);
      var table = el('table');
      table.setAttribute('aria-label', spec.title + ' values');
      var thead = el('thead');
      var hr = el('tr');
      hr.appendChild(el('th', null, 'Time'));
      spec.series.forEach(function (s) { hr.appendChild(el('th', 'num', s.label)); });
      thead.appendChild(hr);
      table.appendChild(thead);
      var tbody = el('tbody');
      for (var i = n - 1; i >= 0; i--) {
        var tr = el('tr');
        tr.appendChild(el('td', null, fullFmt.format(new Date(timestamps[i] * 1000))));
        for (var k = 0; k < spec.series.length; k++) {
          tr.appendChild(el('td', 'num', fmt(spec.series[k].values[i])));
        }
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      tableHost.appendChild(table);
    }

    function setView(mode) {
      state.views[spec.title] = mode;
      var table = mode === 'table';
      plot.hidden = table;
      tableHost.hidden = !table;
      toggle.textContent = table ? 'Chart view' : 'Table view';
      if (table) buildTable(); else draw();
    }

    toggle.addEventListener('click', function () {
      setView(state.views[spec.title] === 'table' ? 'chart' : 'table');
    });

    function hideTip() {
      if (!parts) return;
      parts.tip.style.display = 'none';
      parts.cross.setAttribute('visibility', 'hidden');
      parts.dots.forEach(function (d) { d.setAttribute('visibility', 'hidden'); });
    }

    function showTip(idx) {
      if (!parts || !geom) return;
      cur = Math.max(0, Math.min(n - 1, idx));
      var px = geom.x(cur);
      parts.cross.setAttribute('x1', px);
      parts.cross.setAttribute('x2', px);
      parts.cross.setAttribute('visibility', 'visible');

      clear(parts.tip);
      parts.tip.appendChild(el('div', 'tip-time', fullFmt.format(new Date(timestamps[cur] * 1000))));
      spec.series.forEach(function (s, k) {
        var v = s.values[cur];
        var dot = parts.dots[k];
        if (v === null || v === undefined) {
          dot.setAttribute('visibility', 'hidden');
        } else {
          dot.setAttribute('cx', px);
          dot.setAttribute('cy', geom.y(v));
          dot.setAttribute('visibility', 'visible');
        }
        var row = el('div', 'tip-row');
        row.appendChild(el('span', 'key ' + s.cls));
        row.appendChild(el('span', 'tip-val', fmt(v)));
        row.appendChild(el('span', 'tip-label', s.label));
        parts.tip.appendChild(row);
      });
      parts.tip.style.display = 'block';
      var tw = parts.tip.offsetWidth;
      var left = px + 14;
      if (left + tw > geom.W) left = px - tw - 14;
      parts.tip.style.left = Math.max(0, left) + 'px';
    }

    function draw() {
      if (plot.hidden) return;
      clear(plot);
      parts = null;
      var W = Math.max(280, Math.floor(plot.clientWidth));
      if (!hasData(spec)) {
        plot.appendChild(el('div', 'empty-msg', 'No data in this range.'));
        return;
      }
      var H = CHART_HEIGHT;
      var iw = W - MARGIN.l - MARGIN.r;
      var ih = H - MARGIN.t - MARGIN.b;
      var max = 0;
      spec.series.forEach(function (s) {
        s.values.forEach(function (v) { if (v !== null && v !== undefined && v > max) max = v; });
      });
      if (spec.threshold && spec.threshold > max) max = spec.threshold;
      var scale = niceScale(max, spec.unit === 'count');
      var x = function (i) { return MARGIN.l + (n <= 1 ? 0 : (i * iw) / (n - 1)); };
      var y = function (v) { return MARGIN.t + ih - (v / scale.top) * ih; };
      geom = { W: W, x: x, y: y };

      var root = svg('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'presentation' });

      scale.ticks.forEach(function (t, i) {
        var yy = y(t);
        root.appendChild(svg('line', { class: i === 0 ? 'baseline' : 'gridline', x1: MARGIN.l, x2: W - MARGIN.r, y1: yy, y2: yy }));
        var label = svg('text', { class: 'axis-label', x: MARGIN.l - 8, y: yy + 4, 'text-anchor': 'end' });
        label.textContent = spec.unit === 'percent' ? (Math.round(t * 10) / 10) + '%' : fmt(t).replace(/ ms$/, '');
        root.appendChild(label);
      });

      var tickIdx = n < 5 ? [0, n - 1] : [0, Math.round((n - 1) / 4), Math.round((n - 1) / 2), Math.round(3 * (n - 1) / 4), n - 1];
      tickIdx.forEach(function (i, k) {
        var anchor = k === 0 ? 'start' : k === tickIdx.length - 1 ? 'end' : 'middle';
        var label = svg('text', { class: 'axis-label', x: x(i), y: H - 6, 'text-anchor': anchor });
        label.textContent = axisTime(timestamps[i]);
        root.appendChild(label);
      });

      if (spec.threshold) {
        var ty = y(spec.threshold);
        root.appendChild(svg('line', { class: 'threshold', x1: MARGIN.l, x2: W - MARGIN.r, y1: ty, y2: ty }));
        var tl = svg('text', { class: 'threshold-label', x: W - MARGIN.r - 4, y: ty - 5, 'text-anchor': 'end' });
        tl.textContent = 'Alarm threshold ' + spec.threshold + '%';
        root.appendChild(tl);
      }

      spec.series.forEach(function (s) {
        var segments = [];
        var run = null;
        s.values.forEach(function (v, i) {
          if (v === null || v === undefined) { run = null; return; }
          if (!run) { run = []; segments.push(run); }
          run.push([x(i), y(v)]);
        });
        var d = '';
        var area = '';
        segments.forEach(function (seg) {
          seg.forEach(function (p, i) { d += (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); });
          if (seg.length === 1) d += 'l0 0.01';
          if (spec.series.length === 1 && seg.length > 1) {
            var base = (MARGIN.t + ih).toFixed(1);
            area += seg.map(function (p, i) { return (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join('') +
              'L' + seg[seg.length - 1][0].toFixed(1) + ' ' + base + 'L' + seg[0][0].toFixed(1) + ' ' + base + 'Z';
          }
        });
        if (area) root.appendChild(svg('path', { class: 'area ' + s.cls, d: area }));
        root.appendChild(svg('path', { class: 'line ' + s.cls, d: d }));
      });

      // End dots, and direct end-labels when they do not collide (the legend covers the rest).
      var ends = [];
      spec.series.forEach(function (s) {
        for (var i = n - 1; i >= 0; i--) {
          var v = s.values[i];
          if (v !== null && v !== undefined) { ends.push({ s: s, i: i, yy: y(v) }); break; }
        }
      });
      ends.forEach(function (e) {
        root.appendChild(svg('circle', { class: 'dot ' + e.s.cls, cx: x(e.i), cy: e.yy, r: 4 }));
      });
      if (spec.series.length > 1) {
        var placed = [];
        ends.slice().sort(function (a, b) { return a.yy - b.yy; }).forEach(function (e) {
          if (placed.some(function (p) { return Math.abs(p - e.yy) < 13; })) return;
          placed.push(e.yy);
          var t = svg('text', { class: 'end-label', x: x(e.i) + 9, y: e.yy + 4 });
          t.textContent = e.s.short || e.s.label;
          root.appendChild(t);
        });
      }

      var cross = svg('line', { class: 'crosshair', x1: 0, x2: 0, y1: MARGIN.t, y2: MARGIN.t + ih, visibility: 'hidden' });
      root.appendChild(cross);
      var dots = spec.series.map(function (s) {
        var c = svg('circle', { class: 'dot ' + s.cls, r: 5, cx: 0, cy: 0, visibility: 'hidden' });
        root.appendChild(c);
        return c;
      });
      // The hit area is the whole plot band, far larger than any mark.
      var hit = svg('rect', { class: 'hit', x: MARGIN.l, y: MARGIN.t, width: iw, height: ih + 6 });
      root.appendChild(hit);
      plot.appendChild(root);

      var tip = el('div', 'tip');
      tip.setAttribute('aria-hidden', 'true');
      plot.appendChild(tip);
      parts = { tip: tip, cross: cross, dots: dots };

      function indexAt(evt) {
        var box = root.getBoundingClientRect();
        var px = ((evt.clientX - box.left) / box.width) * W;
        return Math.round(((px - MARGIN.l) / iw) * (n - 1));
      }
      hit.addEventListener('pointermove', function (evt) { showTip(indexAt(evt)); });
      hit.addEventListener('pointerdown', function (evt) { showTip(indexAt(evt)); });
      hit.addEventListener('pointerleave', hideTip);
    }

    plot.addEventListener('focus', function () { showTip(cur); });
    plot.addEventListener('blur', hideTip);
    plot.addEventListener('keydown', function (evt) {
      var next = null;
      if (evt.key === 'ArrowLeft') next = cur - 1;
      else if (evt.key === 'ArrowRight') next = cur + 1;
      else if (evt.key === 'Home') next = 0;
      else if (evt.key === 'End') next = n - 1;
      else if (evt.key === 'Escape') { hideTip(); return; }
      if (next !== null) { evt.preventDefault(); showTip(next); }
    });

    return { node: card, draw: draw, setView: setView, title: spec.title };
  }

  // ---- lists and tables -------------------------------------------------
  var STATE_TEXT = { ALARM: 'In alarm', OK: 'OK', INSUFFICIENT_DATA: 'Insufficient data' };
  var STATE_STATUS = { ALARM: 'critical', OK: 'good', INSUFFICIENT_DATA: 'warning' };

  function note(text, status) {
    var box = el('div', 'empty-note');
    if (status) {
      var icon = el('span', 'status-icon status-' + status, ICONS[status]);
      icon.setAttribute('aria-hidden', 'true');
      box.appendChild(icon);
    }
    box.appendChild(el('span', null, text));
    return box;
  }

  function renderAlarms(alarms) {
    var host = $('alarms');
    clear(host);
    if (!alarms.available) { host.appendChild(note('Alarm data is unavailable right now.', 'warning')); return; }
    if (alarms.items.length === 0) {
      host.appendChild(note('No alarms found. Deploy the monitoring stack to create them.', 'warning'));
      return;
    }
    var list = el('ul', 'list');
    alarms.items.forEach(function (a) {
      var li = el('li');
      var row = el('div', 'row');
      var status = STATE_STATUS[a.state] || 'warning';
      var icon = el('span', 'status-icon status-' + status, ICONS[status]);
      icon.setAttribute('aria-hidden', 'true');
      row.appendChild(icon);
      row.appendChild(el('span', 'row-title', a.name));
      row.appendChild(el('span', 'tag', STATE_TEXT[a.state] || a.state));
      row.appendChild(el('span', 'row-meta', relTime(a.updated)));
      li.appendChild(row);
      if (a.state !== 'OK' && a.reason) li.appendChild(el('div', 'row-detail', a.reason));
      list.appendChild(li);
    });
    host.appendChild(list);
  }

  function renderErrors(errors) {
    var host = $('errors');
    clear(host);
    if (!errors.available) { host.appendChild(note('Log data is unavailable right now.', 'warning')); return; }
    if (errors.items.length === 0) { host.appendChild(note('No errors logged in this range.', 'good')); return; }
    var list = el('ul', 'list');
    errors.items.forEach(function (e) {
      var li = el('li');
      var row = el('div', 'row');
      row.appendChild(el('span', 'row-meta', fullFmt.format(new Date(e.time))));
      row.appendChild(el('span', 'tag', e.function));
      li.appendChild(row);
      li.appendChild(el('p', 'err-msg', e.message));
      list.appendChild(li);
    });
    host.appendChild(list);
  }

  function renderCustom(custom) {
    var host = $('custom');
    clear(host);
    if (!custom.available) { host.appendChild(note('Application metrics are unavailable right now.', 'warning')); return; }
    if (custom.items.length === 0) {
      host.appendChild(note('No application metrics yet. Booking, saga and waitlist counts appear here once the booking code emits them.'));
      return;
    }
    var table = el('table');
    var hr = el('tr');
    hr.appendChild(el('th', null, 'Metric'));
    hr.appendChild(el('th', 'num', 'Total in range'));
    var thead = el('thead');
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el('tbody');
    custom.items.forEach(function (m) {
      var tr = el('tr');
      tr.appendChild(el('td', null, m.name));
      tr.appendChild(el('td', 'num', fmtCount(m.total)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  function renderDynamoTable(d) {
    var host = $('ddb-table');
    clear(host);
    var table = el('table');
    var thead = el('thead');
    var hr = el('tr');
    ['Table', 'Throttled', 'Condition check failures', 'Read units', 'Write units'].forEach(function (h, i) {
      hr.appendChild(el('th', i === 0 ? null : 'num', h));
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el('tbody');
    ['slots', 'seats'].forEach(function (key) {
      var t = d.dynamodb[key];
      var tr = el('tr');
      tr.appendChild(el('td', null, t.name));
      tr.appendChild(el('td', 'num', fmtCount(t.totals.throttled)));
      tr.appendChild(el('td', 'num', fmtCount(t.totals.conditional_check_failures)));
      tr.appendChild(el('td', 'num', fmtCount(t.totals.read_units)));
      tr.appendChild(el('td', 'num', fmtCount(t.totals.write_units)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  // ---- render and fetch -------------------------------------------------
  function renderCharts(d) {
    var host = $('charts');
    clear(host);
    state.cards = buildSpecs(d).map(function (spec) { return makeCard(spec, d.timestamps); });
    state.cards.forEach(function (c) { host.appendChild(c.node); });
    state.cards.forEach(function (c) {
      if (state.views[c.title] === 'table') c.setView('table'); else c.draw();
    });
    state.lastWidth = host.clientWidth;
  }

  function render(d) {
    renderKpis(d);
    renderCharts(d);
    renderDynamoTable(d);
    renderAlarms(d.alarms);
    renderErrors(d.errors);
    renderCustom(d.custom);
    $('ddb-sub').textContent = 'Totals for the selected range';
  }

  function load() {
    var main = $('main');
    main.classList.add('loading');
    var requested = state.range;
    return fetch('/api/summary?range=' + encodeURIComponent(requested), {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    }).then(function (res) {
      if (res.status === 401) { window.location.href = '/login'; return null; }
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      if (!data || requested !== state.range) return;
      state.data = data;
      render(data);
      $('banner').textContent = '';
      $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString() + ' · times shown in your local time zone';
    }).catch(function () {
      var when = state.data ? ' Showing data from ' + new Date(state.data.generated_at).toLocaleTimeString() + '.' : '';
      $('banner').textContent = 'Could not refresh the dashboard.' + when;
      $('updated').textContent = 'Refresh failed';
    }).then(function () {
      main.classList.remove('loading');
    });
  }

  // ---- wiring -----------------------------------------------------------
  function setRange(range) {
    state.range = range;
    document.querySelectorAll('.seg-btn').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-range') === range ? 'true' : 'false');
    });
    load();
  }

  document.querySelectorAll('.seg-btn').forEach(function (b) {
    b.addEventListener('click', function () { setRange(b.getAttribute('data-range')); });
  });

  function effectiveTheme() {
    var set = document.documentElement.getAttribute('data-theme');
    if (set) return set;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  var themeBtn = $('theme-toggle');
  function syncThemeButton() {
    themeBtn.textContent = effectiveTheme() === 'dark' ? 'Light mode' : 'Dark mode';
  }
  themeBtn.addEventListener('click', function () {
    var next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('medislot-theme', next); } catch (e) { /* ignore */ }
    syncThemeButton();
  });
  syncThemeButton();

  var resizeTimer = null;
  function redrawOnResize() {
    var width = $('charts').clientWidth;
    if (width === state.lastWidth) return;
    state.lastWidth = width;
    state.cards.forEach(function (c) { c.draw(); });
  }
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(redrawOnResize, 120);
    }).observe($('charts'));
  } else {
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(redrawOnResize, 120);
    });
  }

  function schedule() {
    clearInterval(state.timer);
    state.timer = setInterval(function () { if (!document.hidden) load(); }, REFRESH_MS);
  }
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && state.data && Date.now() - new Date(state.data.generated_at).getTime() > REFRESH_MS) load();
  });

  load();
  schedule();
})();
