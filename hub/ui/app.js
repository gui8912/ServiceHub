/* 服务台网页端交互 —— 原生 JS，无框架无 CDN，断网离网都能跑。
   原则：界面只负责显示与发指令，所有判断都在后台 Python 里，两边状态永远一致。 */
(function () {
  'use strict';

  var STATE_RANK = { '运行中': 0, '外部运行': 1, '启动中': 2, '工具': 3, '已停止': 4, '未安装': 5 };

  var state = {
    services: [],
    hub: {},
    keyword: '',
    group: '全部',
    view: 'card',
    sort: { key: 'group', dir: 'asc' },
    busy: {},            // id -> true，避免连点重复提交
    failures: 0,
    timer: null,
    autoRefresh: true,
    refreshMs: 2000,
    settings: {},
    browsers: [],
    theme: 'system',
    fetchedAt: Date.now()
  };

  var el = {};
  ['hubMeta', 'content', 'searchBox', 'groupChips', 'stats', 'viewSeg',
   'btnStartAll', 'btnStopAll', 'btnRefresh', 'btnExit', 'btnTheme', 'btnSettings',
   'toasts', 'modalMask', 'modalTitle', 'modalBody', 'modalFoot', 'modalClose'
  ].forEach(function (id) { el[id] = document.getElementById(id); });

  var lastPrint = '';

  // ------------------------------------------------------------------ 工具
  function esc(text) {
    if (text === null || text === undefined) { return ''; }
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function stateClass(text) {
    if (text === '运行中') { return 'run'; }
    if (text === '外部运行') { return 'external'; }
    if (text === '启动中') { return 'starting'; }
    if (text === '工具') { return 'tool'; }
    if (text === '未安装') { return 'missing'; }
    return '';
  }

  function humanUptime(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    if (!seconds) { return ''; }
    if (seconds < 60) { return seconds + ' 秒'; }
    if (seconds < 3600) { return Math.floor(seconds / 60) + ' 分'; }
    var hours = Math.floor(seconds / 3600);
    var mins = Math.floor((seconds % 3600) / 60);
    if (hours < 24) { return hours + ' 时 ' + mins + ' 分'; }
    return Math.floor(hours / 24) + ' 天 ' + (hours % 24) + ' 时';
  }

  function toast(message, kind) {
    var box = document.createElement('div');
    box.className = 'toast ' + (kind || '');
    box.textContent = message;
    el.toasts.appendChild(box);
    setTimeout(function () {
      box.style.opacity = '0';
      box.style.transition = 'opacity .25s';
      setTimeout(function () { if (box.parentNode) { box.parentNode.removeChild(box); } }, 260);
    }, 3600);
  }

  function request(url, options) {
    return fetch(url, options).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (payload) {
        if (!resp.ok) { throw new Error(payload.message || ('HTTP ' + resp.status)); }
        return payload;
      });
    });
  }

  function get(url) {
    return request(url, { cache: 'no-store' });
  }

  function post(url, body) {
    return request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-ServiceHub': '1' },
      cache: 'no-store',
      body: JSON.stringify(body || {})
    });
  }

  // ------------------------------------------------------------------ 主题 / 视图
  function resolveTheme(theme) {
    if (theme === 'system') {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    return theme === 'dark' ? 'dark' : 'light';
  }

  function applyTheme(theme, persist) {
    state.theme = theme;
    document.documentElement.setAttribute('data-theme', resolveTheme(theme));
    if (persist) {
      try { localStorage.setItem('hub.theme', theme); } catch (e) {}
    }
  }

  function setView(view, persist) {
    state.view = view === 'list' ? 'list' : 'card';
    Array.prototype.forEach.call(el.viewSeg.querySelectorAll('.segBtn'), function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-view') === state.view);
    });
    if (persist) {
      try { localStorage.setItem('hub.view', state.view); } catch (e) {}
    }
    lastPrint = '';
    render();
  }

  // ------------------------------------------------------------------ 渲染
  function renderChips() {
    var groups = ['全部'];
    state.services.forEach(function (svc) {
      if (groups.indexOf(svc.group) < 0) { groups.push(svc.group); }
    });
    if (groups.indexOf(state.group) < 0) { state.group = '全部'; }
    el.groupChips.innerHTML = '';
    groups.forEach(function (name) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip' + (name === state.group ? ' active' : '');
      chip.textContent = name;
      chip.addEventListener('click', function () {
        state.group = name;
        lastPrint = '';
        render();
      });
      el.groupChips.appendChild(chip);
    });
  }

  function renderStats() {
    var total = state.services.length;
    var running = state.services.filter(function (s) { return s.running; }).length;
    var tools = state.services.filter(function (s) { return s.kind === 'tool'; }).length;
    var cost = Math.round((state.hub || {}).cost_ms || 0);
    var parts = ['共 <b>' + total + '</b> 项', '运行 <b>' + running + '</b>'];
    if (tools) { parts.push('工具 <b>' + tools + '</b>'); }
    if (cost) { parts.push('扫描 <b>' + cost + '</b> ms'); }
    el.stats.innerHTML = parts.join(' · ');
  }

  function matches(svc) {
    var hitGroup = state.group === '全部' || svc.group === state.group;
    if (!hitGroup) { return false; }
    var key = state.keyword.trim().toLowerCase();
    if (!key) { return true; }
    var hay = [svc.name, svc.desc, svc.group, svc.portText, svc.state, svc.id,
               svc.version, svc.updated].join(' ').toLowerCase();
    return hay.indexOf(key) >= 0;
  }

  function uptimeSpan(svc) {
    if (!svc.running || !svc.uptime) { return ''; }
    return '<span>已运行 <b data-uptime="' + svc.uptime + '" data-uptime-at="' +
      state.fetchedAt + '">' + esc(humanUptime(svc.uptime)) + '</b></span>';
  }

  function buildCard(svc) {
    var card = document.createElement('div');
    card.className = 'card';

    var meta = '<span>端口 <b>' + esc(svc.portText) + '</b></span>';
    if (svc.kind === 'tool') {
      meta += '<span class="dot"></span><span>一次性脚本</span>';
    } else {
      var up = uptimeSpan(svc);
      if (up) { meta += '<span class="dot"></span>' + up; }
    }
    if (svc.pid) { meta += '<span class="dot"></span><span>PID <b>' + esc(svc.pid) + '</b></span>'; }
    if (svc.external) { meta += '<span class="dot"></span><span>非本工作台启动</span>'; }
    if (svc.autostart === false) { meta += '<span class="dot"></span><span>不参与全部启动</span>'; }

    var actions = '';
    if (svc.kind === 'tool') {
      actions += '<button type="button" class="btn primary" data-act="run" data-id="' + esc(svc.id) + '">运行</button>';
    } else {
      actions += svc.running
        ? '<button type="button" class="btn danger" data-act="stop" data-id="' + esc(svc.id) + '">停止</button>'
        : '<button type="button" class="btn primary" data-act="start" data-id="' + esc(svc.id) + '">启动</button>';
    }
    actions += '<button type="button" class="btn" data-act="open" data-id="' + esc(svc.id) + '"'
      + (svc.url ? '' : ' disabled') + '>打开网页</button>';
    actions += '<button type="button" class="linkBtn" data-act="log" data-id="' + esc(svc.id) + '">查看日志</button>';

    card.innerHTML =
      '<div class="cardHead">' +
        '<div class="cardName">' + esc(svc.name) +
          '<span class="cardId">' + esc(svc.id) + '</span>' +
        '</div>' +
        '<span class="pill ' + stateClass(svc.state) + '">' + esc(svc.state) + '</span>' +
      '</div>' +
      '<div class="cardDesc">' + esc(svc.desc) + '</div>' +
      '<div class="cardMeta">' + meta + '</div>' +
      '<div class="cardVer">' +
        '<span>版本 <code>' + esc(svc.version || '—') + '</code></span>' +
        '<span>更新于 ' + esc(svc.updated || '—') + '</span>' +
      '</div>' +
      '<div class="cardActions">' + actions + '</div>';

    if (state.busy[svc.id]) {
      Array.prototype.forEach.call(card.querySelectorAll('button'), function (btn) { btn.disabled = true; });
    }
    return card;
  }

  // ------------------------------------------------------------------ 列表视图
  var COLUMNS = [
    { key: 'name', label: '服务' },
    { key: 'group', label: '分组' },
    { key: 'version', label: '版本号' },
    { key: 'updated', label: '更新时间' },
    { key: 'state', label: '状态' },
    { key: 'port', label: '端口' },
    { key: 'actions', label: '操作' }
  ];

  function versionParts(value) {
    return String(value || '').split(/[^0-9]+/).filter(function (x) { return x !== ''; })
      .map(function (x) { return parseInt(x, 10) || 0; });
  }

  function compare(a, b, key) {
    if (key === 'version') {
      var pa = versionParts(a.version), pb = versionParts(b.version);
      for (var i = 0; i < Math.max(pa.length, pb.length); i++) {
        var va = pa[i] === undefined ? -1 : pa[i];
        var vb = pb[i] === undefined ? -1 : pb[i];
        if (va !== vb) { return va - vb; }
      }
      return 0;
    }
    if (key === 'state') {
      var ra = STATE_RANK[a.state] === undefined ? 9 : STATE_RANK[a.state];
      var rb = STATE_RANK[b.state] === undefined ? 9 : STATE_RANK[b.state];
      return ra - rb;
    }
    if (key === 'port') {
      var na = a.port === null || a.port === undefined ? 999999 : a.port;
      var nb = b.port === null || b.port === undefined ? 999999 : b.port;
      return na - nb;
    }
    if (key === 'updated') {
      return String(a.updated || '').localeCompare(String(b.updated || ''));
    }
    return String(a[key] || '').localeCompare(String(b[key] || ''), 'zh-Hans-CN', { numeric: true });
  }

  function sortedList(list) {
    var copy = list.slice();
    var key = state.sort.key;
    var dir = state.sort.dir === 'desc' ? -1 : 1;
    copy.sort(function (a, b) {
      var result = compare(a, b, key);
      if (result === 0) { result = compare(a, b, 'name'); }
      return result * dir;
    });
    return copy;
  }

  function renderList(list) {
    var rows = sortedList(list);
    var head = COLUMNS.map(function (col) {
      if (col.key === 'actions') {
        return '<th class="' + '">' + esc(col.label) + '</th>';
      }
      var active = state.sort.key === col.key;
      var arrow = active ? (state.sort.dir === 'desc' ? '▼' : '▲') : '▲';
      return '<th class="sortable' + (active ? ' sorted' : '') + '" data-sort="' + col.key +
        '" title="点击排序">' + esc(col.label) + '<span class="arrow">' + arrow + '</span></th>';
    }).join('');

    var body = rows.map(function (svc) {
      var actions = '';
      if (svc.kind === 'tool') {
        actions += '<button type="button" class="btn primary" data-act="run" data-id="' + esc(svc.id) + '">运行</button>';
      } else {
        actions += svc.running
          ? '<button type="button" class="btn danger" data-act="stop" data-id="' + esc(svc.id) + '">停止</button>'
          : '<button type="button" class="btn primary" data-act="start" data-id="' + esc(svc.id) + '">启动</button>';
      }
      actions += '<button type="button" class="btn" data-act="open" data-id="' + esc(svc.id) + '"'
        + (svc.url ? '' : ' disabled') + '>网页</button>';
      actions += '<button type="button" class="linkBtn" data-act="log" data-id="' + esc(svc.id) + '">日志</button>';

      return '<tr>' +
        '<td class="cName">' + esc(svc.name) + '<span class="cId">' + esc(svc.id) + '</span>' +
          '<div class="cDesc">' + esc(svc.desc) + '</div></td>' +
        '<td>' + esc(svc.group) + '</td>' +
        '<td class="cVer"><code>' + esc(svc.version || '—') + '</code></td>' +
        '<td class="cUpd">' + esc(svc.updated || '—') + '</td>' +
        '<td><span class="pill ' + stateClass(svc.state) + '">' + esc(svc.state) + '</span></td>' +
        '<td class="cPort">' + esc(svc.portText) + '</td>' +
        '<td class="cAct">' + actions + '</td>' +
      '</tr>';
    }).join('');

    var wrap = document.createElement('div');
    wrap.className = 'tableWrap glass';
    wrap.innerHTML = '<table class="svcTable"><thead><tr>' + head + '</tr></thead><tbody>' +
      body + '</tbody></table>';

    if (state.busy && Object.keys(state.busy).length) {
      rows.forEach(function (svc) {
        if (!state.busy[svc.id]) { return; }
        Array.prototype.forEach.call(
          wrap.querySelectorAll('[data-id="' + svc.id + '"]'), function (btn) { btn.disabled = true; });
      });
    }
    el.content.appendChild(wrap);
  }

  function renderContent(list) {
    el.content.innerHTML = '';
    if (!state.services.length) {
      el.content.innerHTML = '<div class="empty">还没有登记任何服务，去 services.json 里加一条就行。</div>';
      return;
    }
    if (!list.length) {
      el.content.innerHTML = '<div class="empty">没有匹配的服务。</div>';
      return;
    }
    if (state.view === 'list') {
      renderList(list);
      return;
    }
    var order = [];
    var byGroup = {};
    list.forEach(function (svc) {
      if (!byGroup[svc.group]) { byGroup[svc.group] = []; order.push(svc.group); }
      byGroup[svc.group].push(svc);
    });
    order.forEach(function (name) {
      var section = document.createElement('section');
      section.className = 'group';
      section.innerHTML = '<h2 class="groupTitle">' + esc(name) +
        '<span class="count">' + byGroup[name].length + ' 项</span></h2>';
      var grid = document.createElement('div');
      grid.className = 'grid';
      byGroup[name].forEach(function (svc) { grid.appendChild(buildCard(svc)); });
      section.appendChild(grid);
      el.content.appendChild(section);
    });
  }

  function fingerprint(list) {
    var rows = list.map(function (s) {
      return [s.id, s.state, s.portText, s.url, s.pid, s.version, s.updated, s.name,
              s.desc, s.group, s.kind, s.autostart, s.running].join('|');
    });
    return [state.view, state.keyword, state.group, state.sort.key, state.sort.dir,
            Object.keys(state.busy).join(','), rows.join('~')].join('#');
  }

  function updateUptimes() {
    var now = Date.now();
    Array.prototype.forEach.call(el.content.querySelectorAll('[data-uptime]'), function (node) {
      var base = parseInt(node.getAttribute('data-uptime'), 10) || 0;
      var at = parseInt(node.getAttribute('data-uptime-at'), 10) || now;
      var seconds = base + Math.max(0, (now - at) / 1000);
      node.textContent = humanUptime(seconds);
    });
  }

  function render() {
    renderChips();
    renderStats();
    var list = state.services.filter(matches);
    var fp = fingerprint(list);
    if (fp !== lastPrint) {
      lastPrint = fp;
      renderContent(list);
    } else {
      updateUptimes();
    }
  }

  function renderHub() {
    var hub = state.hub || {};
    if (!hub.port) { el.hubMeta.textContent = '正在连接…'; return; }
    var bits = [
      '127.0.0.1:' + hub.port,
      'v' + (hub.version || '?'),
      'Python ' + hub.python,
      '已运行 ' + (humanUptime(hub.uptime) || '刚刚')
    ];
    if (hub.tray) { bits.push('托盘常驻'); }
    if (!hub.psutil) { bits.push('psutil 缺失'); }
    el.hubMeta.textContent = bits.join(' · ');
    el.hubMeta.title = '安装目录：' + (hub.appdir || '') + '\n日志目录：' + (hub.logdir || '');
  }

  // ------------------------------------------------------------------ 轮询
  function refresh(quiet) {
    return get('/api/status').then(function (payload) {
      state.failures = 0;
      state.fetchedAt = Date.now();
      state.services = payload.services || [];
      state.hub = payload.hub || {};
      renderHub();
      render();
    }).catch(function (err) {
      state.failures += 1;
      if (state.failures >= 3) {
        el.content.innerHTML =
          '<div class="offline">服务台后台没有响应（' + esc(err.message) + '）。' +
          '双击桌面「服务台」重新唤起；已启动的业务服务不受影响。</div>';
        lastPrint = '';
      }
      if (!quiet) { toast('读取状态失败：' + err.message, 'error'); }
    });
  }

  function schedule() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    if (!state.autoRefresh || !state.refreshMs) { return; }
    state.timer = setTimeout(function () {
      if (!document.hidden) {
        refresh(true).then(schedule);
      } else {
        schedule();
      }
    }, state.refreshMs);
  }

  // ------------------------------------------------------------------ 动作
  function setBusy(id, flag) {
    if (flag) { state.busy[id] = true; } else { delete state.busy[id]; }
    lastPrint = '';
  }

  function doAction(id, action, force) {
    setBusy(id, true);
    render();
    return post('/api/action', { id: id, action: action, force: !!force }).then(function (result) {
      if (result.needConfirm) {
        confirmDialog(result.message, function () { doAction(id, action, true); });
        return;
      }
      toast(result.message || '已执行', result.ok ? 'ok' : 'warn');
      return delayedRefresh();
    }).catch(function (err) {
      toast('操作失败：' + err.message, 'error');
    }).then(function () {
      setBusy(id, false);
      render();
    });
  }

  function batched(action, title) {
    confirmDialog('确定要' + title + '吗？', function () {
      toast(title + '中…');
      post('/api/batch', { action: action }).then(function (result) {
        toast(result.message || '完成', result.ok ? 'ok' : 'warn');
        delayedRefresh();
      }).catch(function (err) { toast('批量操作失败：' + err.message, 'error'); });
    });
  }

  function delayedRefresh() {
    return new Promise(function (resolve) {
      setTimeout(function () {
        refresh(true).then(function () {
          setTimeout(function () { refresh(true).then(resolve); }, 900);
        });
      }, 400);
    });
  }

  // ------------------------------------------------------------------ 弹窗
  function closeModal() { el.modalMask.hidden = true; }

  function confirmDialog(message, onYes) {
    el.modalTitle.textContent = '请确认';
    el.modalBody.textContent = message;
    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    var yes = document.createElement('button');
    yes.type = 'button';
    yes.className = 'btn danger';
    yes.textContent = '确定';
    yes.addEventListener('click', function () { closeModal(); onYes(); });
    var no = document.createElement('button');
    no.type = 'button';
    no.className = 'btn';
    no.textContent = '取消';
    no.addEventListener('click', closeModal);
    el.modalFoot.appendChild(no);
    el.modalFoot.appendChild(yes);
    el.modalMask.hidden = false;
  }

  function logDialog(id) {
    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    var again = document.createElement('button');
    again.type = 'button';
    again.className = 'btn';
    again.textContent = '刷新日志';
    again.addEventListener('click', function () { loadLog(id); });
    el.modalFoot.appendChild(again);
    el.modalTitle.textContent = '日志 · ' + (id === '__hub__' ? '服务台' : id);
    el.modalMask.hidden = false;
    loadLog(id);
  }

  function loadLog(id) {
    el.modalBody.innerHTML = '<div>读取中…</div>';
    get('/api/logs?id=' + encodeURIComponent(id) + '&lines=300').then(function (payload) {
      if (!payload.ok) {
        el.modalBody.innerHTML = '<div>' + esc(payload.message) + '</div>';
        return;
      }
      var text = (payload.lines || []).join('\n') || '（空）';
      el.modalBody.innerHTML = '<pre>' + esc(text) + '</pre>';
      el.modalBody.scrollTop = el.modalBody.scrollHeight;
    }).catch(function (err) {
      el.modalBody.innerHTML = '<div>读取失败：' + esc(err.message) + '</div>';
    });
  }

  // ------------------------------------------------------------------ 设置面板
  function settingsGroup(title, rows) {
    var box = document.createElement('section');
    box.className = 'setGroup';
    var h = document.createElement('h3');
    h.className = 'setGroupTitle';
    h.textContent = title;
    box.appendChild(h);
    rows.forEach(function (row) { box.appendChild(row); });
    return box;
  }

  function settingsRow(label, hint, ctrl) {
    var row = document.createElement('div');
    row.className = 'setRow';
    var main = document.createElement('div');
    main.className = 'setMain';
    var name = document.createElement('div');
    name.className = 'setLabel';
    name.textContent = label;
    main.appendChild(name);
    if (hint) {
      var tip = document.createElement('div');
      tip.className = 'setHint';
      tip.textContent = hint;
      main.appendChild(tip);
    }
    var right = document.createElement('div');
    right.className = 'setCtrl';
    right.appendChild(ctrl);
    row.appendChild(main);
    row.appendChild(right);
    return row;
  }

  function selectCtrl(options, value, onChange) {
    var select = document.createElement('select');
    options.forEach(function (opt) {
      var option = document.createElement('option');
      option.value = opt[0];
      option.textContent = opt[1];
      if (String(opt[0]) === String(value)) { option.selected = true; }
      select.appendChild(option);
    });
    select.addEventListener('change', function () { onChange(select.value); });
    return select;
  }

  function switchCtrl(checked, onChange) {
    var label = document.createElement('label');
    label.className = 'switch';
    var input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!checked;
    input.addEventListener('change', function () { onChange(input.checked); });
    var track = document.createElement('span');
    track.className = 'track';
    label.appendChild(input);
    label.appendChild(track);
    return label;
  }

  function saveSettings(patch, note) {
    return post('/api/settings', patch).then(function (payload) {
      state.settings = payload.settings || state.settings;
      if (typeof state.settings.refresh_ms === 'number') {
        state.refreshMs = state.settings.refresh_ms;
      }
      if (note) { toast(note, 'ok'); }
      schedule();
      return payload;
    }).catch(function (err) {
      toast('保存失败：' + err.message, 'error');
    });
  }

  function openSettings() {
    el.modalTitle.textContent = '设置';
    el.modalBody.innerHTML = '';
    el.modalFoot.hidden = true;

    var cfg = state.settings || {};
    var footer = document.createElement('div');

    // ---- 外观
    var themeRow = settingsRow('主题', '跟随系统会随 Windows 明暗模式自动切换', selectCtrl([
      ['system', '跟随系统'], ['light', '明亮'], ['dark', '暗色']
    ], cfg.theme || 'system', function (value) {
      applyTheme(value, true);
      saveSettings({ theme: value }, '主题已切换为' + (value === 'system' ? '跟随系统' : value === 'dark' ? '暗色' : '明亮'));
    }));

    var viewRow = settingsRow('默认视图', '列表视图每行一项，点表头可按列排序', selectCtrl([
      ['card', '卡片'], ['list', '列表']
    ], cfg.view || state.view, function (value) {
      setView(value, true);
      saveSettings({ view: value }, '默认视图已改为' + (value === 'list' ? '列表' : '卡片'));
    }));

    var refreshRow = settingsRow('自动刷新', '状态由后台每秒采集，这里只管网页多久取一次', selectCtrl([
      ['1000', '每 1 秒'], ['2000', '每 2 秒'], ['5000', '每 5 秒'], ['10000', '每 10 秒'], ['off', '暂停']
    ], state.autoRefresh ? String(state.refreshMs) : 'off', function (value) {
      if (value === 'off') {
        state.autoRefresh = false;
        try { localStorage.setItem('hub.autoRefresh', '0'); } catch (e) {}
        schedule();
        toast('已暂停自动刷新', 'ok');
        return;
      }
      state.autoRefresh = true;
      state.refreshMs = parseInt(value, 10);
      try { localStorage.setItem('hub.autoRefresh', '1'); } catch (e) {}
      saveSettings({ refresh_ms: state.refreshMs }, '刷新间隔已更新');
    }));

    // ---- 行为
    var autoOpenRow = settingsRow('启动服务后自动打开网页',
      '只对带网页入口的服务生效；动态端口服务会等端口出来再打开',
      switchCtrl(cfg.auto_open, function (checked) {
        saveSettings({ auto_open: checked }, '已保存自动开网页 = ' + (checked ? '开' : '关'));
      }));

    var browserOptions = [['auto', '系统默认浏览器']];
    (state.browsers || []).forEach(function (b) {
      browserOptions.push([b.id, b.name + '（' + b.id + '）']);
    });
    browserOptions.push(['custom', '自定义路径…']);

    var browserRow = settingsRow('打开网页用哪个浏览器',
      '服务台的「打开网页」和托盘入口都走这里选定的浏览器',
      selectCtrl(browserOptions, cfg.browser || 'auto', function (value) {
        saveSettings({ browser: value }, '浏览器已设为 ' + value);
        pathRow.style.display = value === 'custom' ? '' : 'none';
      }));

    var pathInput = document.createElement('input');
    pathInput.type = 'text';
    pathInput.placeholder = '例如 C:\\Program Files\\...\\chrome.exe';
    pathInput.value = cfg.browser_path || '';
    var pathRow = settingsRow('自定义浏览器路径', '仅当上面选「自定义路径」时使用；可直接粘贴 exe 完整路径', pathInput);
    pathRow.style.display = (cfg.browser || 'auto') === 'custom' ? '' : 'none';
    pathInput.addEventListener('change', function () {
      saveSettings({ browser_path: pathInput.value.trim(), browser: 'custom' }, '自定义浏览器路径已保存');
    });

    var testBtn = document.createElement('button');
    testBtn.type = 'button';
    testBtn.className = 'btn';
    testBtn.textContent = '用这个浏览器打开服务台';
    testBtn.addEventListener('click', function () {
      post('/api/browser/test', {
        browser: (state.settings || {}).browser || 'auto',
        browser_path: (state.settings || {}).browser_path || ''
      }).then(function (res) {
        toast(res.message || '已尝试打开', res.ok ? 'ok' : 'warn');
      }).catch(function (err) { toast('打开失败：' + err.message, 'error'); });
    });
    footer.appendChild(testBtn);

    // ---- 关于
    var hub = state.hub || {};
    var about = document.createElement('div');
    about.className = 'aboutGrid';
    about.innerHTML =
      '<div>服务台版本<b>v' + esc(hub.version || '—') + '</b></div>' +
      '<div>构建日期<b>' + esc(hub.build || '—') + '</b></div>' +
      '<div>监听端口<b>' + esc(hub.port || '—') + '</b></div>' +
      '<div>Python<b>' + esc(hub.python || '—') + '</b></div>' +
      '<div>启动于<b>' + esc(hub.started_at || '—') + '</b></div>' +
      '<div>服务 / 运行中<b>' + esc(hub.total || 0) + ' / ' + esc(hub.running || 0) + '</b></div>' +
      '<div>扫描耗时<b>' + esc(hub.cost_ms || 0) + ' ms</b></div>' +
      '<div>托盘<b>' + (hub.tray ? '常驻' : '未启用') + '</b></div>' +
      '<div class="pathLine">程序目录：' + esc(hub.appdir || '—') + '</div>' +
      '<div class="pathLine">日志目录：' + esc(hub.logdir || '—') + '</div>';

    var aboutBox = document.createElement('section');
    aboutBox.className = 'setGroup';
    var aboutTitle = document.createElement('h3');
    aboutTitle.className = 'setGroupTitle';
    aboutTitle.textContent = '关于';
    aboutBox.appendChild(aboutTitle);
    aboutBox.appendChild(about);

    var hubLogBtn = document.createElement('button');
    hubLogBtn.type = 'button';
    hubLogBtn.className = 'btn';
    hubLogBtn.textContent = '查看服务台日志';
    hubLogBtn.addEventListener('click', function () { logDialog('__hub__'); });
    var reloadBtn = document.createElement('button');
    reloadBtn.type = 'button';
    reloadBtn.className = 'btn';
    reloadBtn.textContent = '重读 services.json';
    reloadBtn.addEventListener('click', function () {
      post('/api/reload', {}).then(function (res) {
        toast(res.message || '已重读', 'ok');
        lastPrint = '';
        delayedRefresh();
      }).catch(function (err) { toast('重读失败：' + err.message, 'error'); });
    });
    var tools = document.createElement('div');
    tools.style.display = 'flex';
    tools.style.gap = '8px';
    tools.style.marginTop = '12px';
    tools.appendChild(hubLogBtn);
    tools.appendChild(reloadBtn);
    aboutBox.appendChild(tools);

    el.modalBody.appendChild(settingsGroup('外观', [themeRow, viewRow, refreshRow]));
    el.modalBody.appendChild(settingsGroup('行为', [autoOpenRow, browserRow, pathRow]));
    el.modalBody.appendChild(aboutBox);

    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    el.modalFoot.appendChild(footer);
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn primary';
    close.textContent = '完成';
    close.addEventListener('click', closeModal);
    el.modalFoot.appendChild(close);

    el.modalMask.hidden = false;
  }

  // ------------------------------------------------------------------ 事件
  el.content.addEventListener('click', function (event) {
    var th = event.target.closest('th[data-sort]');
    if (th) {
      var key = th.getAttribute('data-sort');
      if (state.sort.key === key) {
        state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sort.key = key;
        state.sort.dir = 'asc';
      }
      try { localStorage.setItem('hub.sort', JSON.stringify(state.sort)); } catch (e) {}
      lastPrint = '';
      render();
      return;
    }
    var btn = event.target.closest('button[data-act]');
    if (!btn || btn.disabled) { return; }
    var id = btn.getAttribute('data-id');
    var act = btn.getAttribute('data-act');
    if (act === 'open') { doAction(id, 'open'); return; }
    if (act === 'log') { logDialog(id); return; }
    if (act === 'run') {
      // 有风险的一次性工具（清理磁盘、结束 Word 等）先弹确认再跑
      var target = null;
      for (var i = 0; i < state.services.length; i++) {
        if (state.services[i].id === id) { target = state.services[i]; break; }
      }
      if (target && target.confirm) {
        confirmDialog(target.confirm, function () { doAction(id, 'run'); });
        return;
      }
    }
    doAction(id, act);
  });

  el.searchBox.addEventListener('input', function () {
    state.keyword = el.searchBox.value;
    lastPrint = '';
    render();
  });

  el.viewSeg.addEventListener('click', function (event) {
    var btn = event.target.closest('.segBtn');
    if (!btn) { return; }
    var view = btn.getAttribute('data-view');
    if (view === state.view) { return; }
    setView(view, true);
    saveSettings({ view: view }, '视图已切到' + (view === 'list' ? '列表' : '卡片'));
  });

  el.btnRefresh.addEventListener('click', function () { refresh().then(schedule); });
  el.btnStartAll.addEventListener('click', function () { batched('start-all', '启动全部常驻服务'); });
  el.btnStopAll.addEventListener('click', function () { batched('stop-all', '停止全部服务'); });
  el.btnSettings.addEventListener('click', openSettings);
  el.btnTheme.addEventListener('click', function () {
    var current = document.documentElement.getAttribute('data-theme');
    var next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next, true);
    saveSettings({ theme: next }, '已切换到' + (next === 'dark' ? '暗色' : '明亮'));
  });
  el.btnExit.addEventListener('click', function () {
    confirmDialog('退出后只结束服务台的网页后端，已启动的业务服务继续运行。确定退出服务台？', function () {
      post('/api/exit', {}).then(function () {
        el.content.innerHTML = '<div class="empty">服务台已退出。双击桌面「服务台」可重新唤起。</div>';
        lastPrint = '';
        if (state.timer) { clearTimeout(state.timer); }
      }).catch(function () {
        el.content.innerHTML = '<div class="empty">服务台已退出。</div>';
        lastPrint = '';
      });
    });
  });

  el.modalClose.addEventListener('click', closeModal);
  el.modalMask.addEventListener('click', function (event) {
    if (event.target === el.modalMask) { closeModal(); }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !el.modalMask.hidden) { closeModal(); }
  });

  // ------------------------------------------------------------------ 初始化
  function init() {
    // 网址参数可以临时指定主题/视图（截图、书签、演示用），不写回设置
    var forcedTheme = '';
    var forcedView = '';
    try {
      var params = new URLSearchParams(location.search);
      forcedTheme = params.get('theme') || '';
      forcedView = params.get('view') || '';
    } catch (e) {}

    try {
      var savedSort = localStorage.getItem('hub.sort');
      if (savedSort) {
        var parsed = JSON.parse(savedSort);
        if (parsed && parsed.key) { state.sort = parsed; }
      }
      if (localStorage.getItem('hub.autoRefresh') === '0') { state.autoRefresh = false; }
      var cachedTheme = localStorage.getItem('hub.theme');
      if (cachedTheme && !forcedTheme) { applyTheme(cachedTheme, false); }
      var cachedView = localStorage.getItem('hub.view');
      if (cachedView && !forcedView) { state.view = cachedView === 'list' ? 'list' : 'card'; }
    } catch (e) {}

    if (forcedTheme) { applyTheme(forcedTheme, false); }
    if (forcedView) { state.view = forcedView === 'list' ? 'list' : 'card'; }
    setView(state.view, false);

    get('/api/settings').then(function (payload) {
      state.settings = payload.settings || {};
      state.browsers = payload.browsers || [];
      if (typeof state.settings.refresh_ms === 'number') { state.refreshMs = state.settings.refresh_ms; }
      if (state.settings.theme && !forcedTheme) { applyTheme(state.settings.theme, true); }
      if (state.settings.view && !forcedView) { setView(state.settings.view, true); }
    }).catch(function () {});

    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      if (state.theme === 'system') {
        document.documentElement.setAttribute('data-theme', resolveTheme('system'));
      }
    });

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { refresh(true); }
    });

    refresh().then(schedule);
  }

  init();
})();
