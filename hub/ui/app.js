/* 服务台网页端交互 —— 原生 JS，无框架无 CDN，断网离网都能跑。
   原则：界面只负责显示与发指令，所有判断都在后台 Python 里，两边状态永远一致。 */
(function () {
  'use strict';

  // modified: 2.6.3 补上 2.6.2 新增的「端口冲突」，列表按状态排序时
  // 它原来取兜底 rank 9，被埋在"未安装"后面。
  var STATE_RANK = { '运行中': 0, '外部运行': 1, '端口冲突': 2, '启动中': 3,
                     '工具': 4, '已停止': 5, '未安装': 6 };

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
    browserResolved: null,   // modified: 2.6.1 后端算出的「实际会用哪个浏览器」
    theme: 'system',
    fetchedAt: Date.now(),
    // modified: 2.4.0 首页布局
    hiddenIds: [],       // 已隐藏的服务 id（来自 settings.hidden_ids）
    groupOrder: [],      // 分组顺序（来自 settings.group_order）
    density: 'comfortable', // comfortable | compact
    editMode: false,     // 布局编辑模式：只有开启时才能拖分组/拖卡片、才能点眼睛隐藏
    dragging: false,     // 拖动进行中 → 冻结重绘，别把正在拖的节点重建掉
    dragEl: null,
    // modified: 2.4.1 卡片拖拽的两个落点
    // modified: 2.6.3 组名是用户随便起的字符串，普通对象的原型链键
    // （constructor / toString / __proto__）会被当成"已有记录"读出来。
    // 实测新建名为 constructor 的空组时，orderedItems 拿到的是 Object 构造函数，
    // order.forEach 直接抛异常、整张页面渲染中断。这里统一用无原型对象。
    serviceGroup: Object.create(null),  // {id: 分组名}
    serviceOrder: Object.create(null),  // {分组名: [id, ...]}
    dragCardId: null,    // 正在拖的卡片 id
    dropTarget: null,    // 当前高亮的落点 {id, pos} 或 {group, pos:'end'}
    collapsed: Object.create(null),  // modified: 2.6.3 同上，组名做键必须无原型
    offline: '',         // modified: 2.6.3 后台连不上时的提示，交给 render 统一渲染
    settingsReady: false, // modified: 2.6.3 /api/settings 是否已返回（设置面板竞态）
    batchAll: false,      // modified: 2.6.3 顶栏批量操作的提交锁
    // modified: 2.6.3 组名做键一律用无原型对象，constructor/__proto__/toString
    // 这种用户起的组名才不会被 Object.prototype 上的同名属性冒充。
    batchBusy: Object.create(null)  // modified: 2.6.3 分组批量操作的提交锁
  };

  var el = {};
  ['hubMeta', 'content', 'searchBox', 'groupChips', 'stats', 'viewSeg',
   'btnStartAll', 'btnStopAll', 'btnRefresh', 'btnExit', 'btnTheme', 'btnSettings',
   'btnLayout',
   'toasts', 'modalMask', 'modalTitle', 'modalBody', 'modalFoot', 'modalClose'
  ].forEach(function (id) { el[id] = document.getElementById(id); });

  var lastPrint = '';
  var chipsSig = '';   // modified: 分组按钮的签名，没变就不重建 DOM
  var offlinePrinted = '';   // modified: 2.6.3 离线提示只写一次 DOM
  var modalOnClose = null;   // modified: 2.6.3 确认框被取消时的清理钩子

  // ------------------------------------------------------------------ 工具
  // modified: 2.6.3 把服务端/备份里的普通对象转成无原型字典，
  // 组名做键时不会被 Object.prototype 上的同名属性污染。
  function noProto(obj) {
    var out = Object.create(null);
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      Object.keys(obj).forEach(function (k) { out[k] = obj[k]; });
    }
    return out;
  }

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
    if (text === '端口冲突') { return 'conflict'; }   // modified: 2.6.2
    if (text === '工具') { return 'tool'; }
    if (text === '未安装') { return 'missing'; }
    return '';
  }

  // modified: 2.6.2 端口冲突时把"谁占着"挂到胶囊的悬停提示上，
  // 光看"端口冲突"四个字还得自己 netstat 才知道去找哪个程序。
  function stateTip(svc) {
    var pc = svc && svc.portConflict;
    if (!pc) { return ''; }
    return ' title="' + esc('端口 ' + pc.port + ' 被 ' + (pc.name || '未知程序') +
      '（PID ' + (pc.pid || '?') + '）占用') + '"';
  }

  // modified: 2.6.3 停着但端口被占的服务，启动按钮先给出原因，别等点了才报错
  function startTitle(svc) {
    var pc = svc && svc.portConflict;
    if (!pc) { return ''; }
    return ' title="' + esc('端口 ' + pc.port + ' 被 ' + (pc.name || '未知程序') +
      ' 占用，请先停掉占用它的程序') + '"';
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

  // modified: 2.6.1 当前设置会用哪个浏览器，给按钮 title 和设置页提示共用。
  // 优先用后端算好的结果（它知道 exe 在不在、会不会退回系统默认）。
  function browserLabel() {
    var info = state.browserResolved;
    if (info && info.name) {
      if (info.reason) { return '系统默认浏览器（' + info.name + ' 不可用）'; }
      return info.name;
    }
    var bid = (state.settings || {}).browser || 'auto';
    if (bid === 'auto') { return '系统默认浏览器'; }
    if (bid === 'custom') { return '自定义浏览器'; }
    var list = state.browsers || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === bid) { return list[i].name; }
    }
    return bid;
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

  // modified: 渲染诊断模式。界面"闪"的时候，光靠猜分不清是毛玻璃（backdrop-filter）
  // 还是背景动效造成的，这里给出三个 URL 开关，用排除法一次定位：
  //   ?fx=noanim  只停背景光斑动画
  //   ?fx=noblur  只关毛玻璃（改用不透明底色）
  //   ?fx=off     两样都停（理论上页面将完全静止）
  // 不带 ?fx= 或带不认识的值 = 正常外观，对日常使用零影响。
  var FX_LABEL = {
    noanim: '已停背景动效',
    noblur: '已关毛玻璃',
    off: '已停动效 + 关毛玻璃'
  };
  function applyFxMode(fx) {
    var label = FX_LABEL[fx];
    if (!label) { return; }
    var root = document.documentElement;
    root.classList.add('fx-' + fx);
    var badge = document.createElement('div');
    badge.className = 'fxBadge';
    badge.textContent = '渲染诊断：' + label + '（去掉地址里的 ?fx= 即恢复正常）';
    document.body.appendChild(badge);
  }


  // modified: 背景光斑漂移。默认关——首屏就是静止的，不存在"加载完设置才开始动"的跳变。
  // Edge + Intel 集显 + 高 DPI 下这三个大图层持续运动会整屏闪烁，所以做成可选。
  function applyBgMotion(on) {
    var root = document.documentElement;
    if (on) { root.classList.add('bg-motion'); } else { root.classList.remove('bg-motion'); }
  }

  // ------------------------------------------------------------------ 布局
  // modified: 以下整块为 2.4.0 新增——卡片隐藏 / 分组排序 / 密度 / 编辑模式。

  // 服务图标色块：取名字首字（英文取大写首字母），色相由 id 哈希决定，
  // 同一个服务永远同一个颜色。不引外部图片、不增依赖，靠 --icon-h 让明暗两套各自取色。
  var ICON_HUES = [166, 200, 262, 32, 342, 96, 12, 220];
  function iconOf(svc) {
    var name = String(svc.name || svc.id || '?').trim();
    var ch = name.charAt(0) || '?';
    if (/[a-z]/i.test(ch)) { ch = ch.toUpperCase(); }
    var key = String(svc.id || name);
    var hash = 0;
    for (var i = 0; i < key.length; i++) { hash = (hash * 31 + key.charCodeAt(i)) % 9973; }
    return { ch: ch, hue: ICON_HUES[hash % ICON_HUES.length] };
  }

  function isHidden(id) { return state.hiddenIds.indexOf(id) >= 0; }

  // modified: 2.4.1 有效分组——settings 里的 service_group 优先于 services.json 的 group。
  // 卡片被拖到别的组后只在本机改归属，注册表不动，所以两边可能不一致；
  // 界面显示、筛选、搜索、分组统计一律以这里为准。
  function groupOf(svc) {
    var override = state.serviceGroup[svc.id];
    return (typeof override === 'string' && override) ? override : svc.group;
  }

  function serviceById(id) {
    for (var i = 0; i < state.services.length; i++) {
      if (state.services[i].id === id) { return state.services[i]; }
    }
    return null;
  }

  // 组内顺序：service_order[组名] 里登记过的按登记位置，没登记的接在后面，
  // 并保持它们在 services.json 里的原始相对顺序。
  // Array#sort 从 ES2019 起保证稳定，所以"都没登记"时顺序与原来完全一致。
  function orderedItems(group, items) {
    var order = state.serviceOrder[group];
    // modified: 2.6.3 必须是数组；原型链键/脏数据给个函数或字符串时，
    // 原来的 order.forEach 会把整个 render() 打断。
    if (!Array.isArray(order) || !order.length) { return items; }
    var pos = Object.create(null);  // modified: 2.6.3 服务 id 也按无原型字典存
    order.forEach(function (id, i) { pos[id] = i; });
    var big = order.length + items.length + 1;   // 未登记的排到最后
    return items.slice().sort(function (a, b) {
      var pa = pos[a.id] === undefined ? big : pos[a.id];
      var pb = pos[b.id] === undefined ? big : pos[b.id];
      return pa - pb;
    });
  }

  // 拖动落盘时用它：以「全部服务」（含被隐藏的）为底算一份完整布局，
  // 这样拖动结果不会因为某组折叠、某些卡隐藏而丢掉那一部分的顺序。
  function computeLayout() {
    // modified: 2.6.3 分组名做键必须用无原型对象，否则名为 constructor 的组
    // 会读到 Object 构造函数，groups[g].push 直接抛异常（拖卡片全崩）。
    var groups = Object.create(null);
    var names = [];
    state.services.forEach(function (svc) {
      var g = groupOf(svc);
      if (!groups[g]) { groups[g] = []; names.push(g); }
      groups[g].push(svc);
    });
    var out = Object.create(null);  // modified: 2.6.3 同上，落盘布局不能被原型链污染
    names.forEach(function (g) {
      out[g] = orderedItems(g, groups[g]).map(function (s) { return s.id; });
    });
    return out;
  }

  // 把一张卡移到 targetGroup 里 beforeId 之前（beforeId 为空 = 放到该组末尾）。
  // 只改 settings.service_group / service_order，绝不写 services.json。
  function moveCard(cardId, targetGroup, beforeId) {
    var svc = serviceById(cardId);
    var target = String(targetGroup || '').trim();
    if (!svc || !target) { return; }

    var layout = computeLayout();
    // 先从所有组里摘掉，再插目标位置——这样"拖到自己前面"也不会出现重复项
    Object.keys(layout).forEach(function (g) {
      layout[g] = layout[g].filter(function (id) { return id !== cardId; });
    });
    if (!layout[target]) { layout[target] = []; }
    var idx = beforeId ? layout[target].indexOf(beforeId) : -1;
    if (idx >= 0) { layout[target].splice(idx, 0, cardId); }
    else { layout[target].push(cardId); }

    // 组归属：拖回 services.json 原本的组时就把覆盖删掉，不留下无意义的记录
    var svcGroup = groupOf(svc);            // 注意要在改 state 之前取
    if (target === svc.group) { delete state.serviceGroup[cardId]; }
    else { state.serviceGroup[cardId] = target; }

    // 组内顺序：只保留非空的组
    var nextOrder = Object.create(null);  // modified: 2.6.3 同上
    Object.keys(layout).forEach(function (g) {
      if (layout[g].length) { nextOrder[g] = layout[g]; }
    });
    state.serviceOrder = nextOrder;

    lastPrint = '';
    chipsSig = '';
    render();
    // modified: 2.4.2 成功文案走 saveSettings 的 note 参数，失败只弹它内部那条
    // 「保存失败」。原来是 .then 里弹成功——saveSettings 失败时 catch 吞错照样 resolve，
    // 会先弹「保存失败」再弹「顺序已保存」，两条矛盾提示。
    var moved = target !== svcGroup;
    saveSettings({
      service_group: state.serviceGroup,
      service_order: nextOrder
    }, moved ? '已移到「' + target + '」（只改本机布局，services.json 未动）'
             : '顺序已保存');
  }


  // 注意：隐藏只影响“看得见看不见”，不影响后台采集，
  // 所以被隐藏的服务仍在 state.services 里，启动/停止状态照常刷新。
  function visibleServices() {
    if (!state.hiddenIds.length) { return state.services; }
    return state.services.filter(function (s) { return !isHidden(s.id); });
  }

  // 分组顺序：先按 group_order 里登记的顺序，未登记的新分组接在末尾。
  // 这样以后往 services.json 加一个新分组，不用重新拖一遍。
  function orderedGroups() {
    var seen = [];
    visibleServices().forEach(function (svc) {
      var g = groupOf(svc);   // modified: 拖动过的卡算在它当前所在的组
      if (seen.indexOf(g) < 0) { seen.push(g); }
    });
    var out = [];
    state.groupOrder.forEach(function (name) {
      if (seen.indexOf(name) >= 0 && out.indexOf(name) < 0) { out.push(name); }
    });
    seen.forEach(function (name) {
      if (out.indexOf(name) < 0) { out.push(name); }
    });
    return out;
  }

  function groupCollapsed(name) {
    // modified: 2.6.3 state.collapsed 原来只写不读。隐私模式/禁用站点数据时
    // localStorage 抛异常，折叠按钮点了毫无反应；内存态优先就能在本会话生效。
    if (Object.prototype.hasOwnProperty.call(state.collapsed, name)) {
      return !!state.collapsed[name];
    }
    try {
      var saved = localStorage.getItem('hub.collapse.' + name) === '1';
      state.collapsed[name] = saved;
      return saved;
    } catch (e) { return false; }
  }

  function setGroupCollapsed(name, on) {
    try {
      if (on) { localStorage.setItem('hub.collapse.' + name, '1'); }
      else { localStorage.removeItem('hub.collapse.' + name); }
    } catch (e) {}
    state.collapsed[name] = !!on;
  }

  function applyDensity(density) {
    state.density = density === 'compact' ? 'compact' : 'comfortable';
    var root = document.documentElement;
    root.classList.toggle('density-compact', state.density === 'compact');
    try { localStorage.setItem('hub.density', state.density); } catch (e) {}
  }

  function setEditMode(on) {
    state.editMode = !!on;
    // modified: 2.6.0 列表视图里没有卡片可拖、也没有眼睛按钮，
    // 在列表里点「布局」看起来就像"功能坏了"。进入编辑时自动切回卡片视图
    // （persist=false：只改当前这一次，不动用户的默认视图设置）。
    if (state.editMode && state.view !== 'card') { setView('card', false); }
    document.documentElement.classList.toggle('edit-mode', state.editMode);
    var btn = el.btnLayout;
    if (btn) {
      btn.classList.toggle('active', state.editMode);
      btn.textContent = state.editMode ? '完成' : '布局';
      btn.title = state.editMode ? '退出布局编辑，保存当前顺序' : '进入布局编辑：可拖动分组、隐藏卡片';
    }
    lastPrint = '';
    render();
    if (state.editMode) {
      toast('布局编辑：拖卡片可换位置/换组，拖分组标题可换分组顺序，点眼睛可隐藏', 'ok');
    }
  }

  function saveGroupOrder(order) {
    // modified: 2.6.3 后台掉线时 offline 提示会替换内容区，dragend 读到的
    // order 是空数组；旧代码会把它当成"用户清空了分组顺序"直接写盘。
    if (!order.length) { toast('这次没有读到分组，顺序未改动', 'warn'); return; }
    var same = order.length === state.groupOrder.length &&
      order.every(function (name, i) { return name === state.groupOrder[i]; });
    if (same) { return; }
    state.groupOrder = order;
    lastPrint = '';
    saveSettings({ group_order: order }, null);
  }

  function setHidden(id, hidden) {
    var next = state.hiddenIds.filter(function (x) { return x !== id; });
    if (hidden) { next.push(id); }
    state.hiddenIds = next;
    lastPrint = '';
    chipsSig = '';
    render();
    // modified: 2.4.2 同 moveCard：成功文案走 note，避免保存失败时误弹「已恢复」
    saveSettings({ hidden_ids: next },
      hidden ? '已从首页隐藏，可在设置里恢复' : '已恢复到首页');
  }

  // ------------------------------------------------------------------ 渲染
  function renderChips() {
    // modified: 分组顺序改由 orderedGroups() 决定（跟随拖动结果），
    // 并加签名比对——顺序和选中项都没变时直接返回，不再每秒清空重建一遍按钮。
    var groups = ['全部'].concat(orderedGroups());
    if (groups.indexOf(state.group) < 0) { state.group = '全部'; }
    var sig = groups.join('\u0001') + '#' + state.group;
    if (sig === chipsSig) { return; }
    chipsSig = sig;
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
    var visible = visibleServices();
    var total = visible.length;
    var hidden = state.services.length - total;
    var running = visible.filter(function (s) { return s.running; }).length;
    var tools = visible.filter(function (s) { return s.kind === 'tool'; }).length;
    var cost = Math.round((state.hub || {}).cost_ms || 0);
    var parts = ['共 <b>' + total + '</b> 项'];
    if (hidden) { parts.push('隐藏 <b>' + hidden + '</b>'); }
    parts.push('运行 <b>' + running + '</b>');
    if (tools) { parts.push('工具 <b>' + tools + '</b>'); }
    if (cost) { parts.push('扫描 <b>' + cost + '</b> ms'); }
    el.stats.innerHTML = parts.join(' · ');
  }

  function matches(svc) {
    var hitGroup = state.group === '全部' || groupOf(svc) === state.group;  // modified
    if (!hitGroup) { return false; }
    var key = state.keyword.trim().toLowerCase();
    if (!key) { return true; }
    var hay = [svc.name, svc.desc, groupOf(svc), svc.portText, svc.state, svc.id,  // modified
               svc.version, svc.updated].join(' ').toLowerCase();
    return hay.indexOf(key) >= 0;
  }

  function uptimeSpan(svc) {
    if (!svc.running || !svc.uptime) { return ''; }
    return '<span>已运行 <b data-uptime="' + svc.uptime + '" data-uptime-at="' +
      state.fetchedAt + '">' + esc(humanUptime(svc.uptime)) + '</b></span>';
  }

  function buildCard(svc, hero) {
    var card = document.createElement('div');
    // modified: is-hero = Bento 大卡（占两列），is-tool = 一次性脚本（横向扁条）
    card.className = 'card' + (hero ? ' is-hero' : '') + (svc.kind === 'tool' ? ' is-tool' : '');
    card.setAttribute('data-id', svc.id);

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
    if (svc.lastExit !== null && svc.lastExit !== undefined && svc.lastExit !== 0) {  // modified
      meta += '<span class="dot"></span><span>上次退出 <b>' + esc(svc.lastExit) + '</b></span>';  // modified
    }

    var actions = '';
    if (svc.kind === 'tool') {
      actions += '<button type="button" class="btn primary" data-act="run" data-id="' + esc(svc.id) + '">运行</button>';
    } else {
      actions += svc.running
        ? '<button type="button" class="btn danger" data-act="stop" data-id="' + esc(svc.id) + '">停止</button>'
        : '<button type="button" class="btn primary" data-act="start" data-id="' + esc(svc.id) + '"'
          + startTitle(svc) + '>启动</button>';   // modified: 2.6.3 冲突时先说明原因
      if (svc.running) {  // modified: 重启只对运行中的常驻服务显示
        actions += '<button type="button" class="btn" data-act="restart" data-id="' + esc(svc.id) + '">重启</button>';
      }
    }
    // modified: 2.6.1 没有地址就不画这个按钮。
    // 以前是先画出来再 disabled：16 张卡里有 3 张永远是灰的，点了毫无反应，
    // 用户以为按钮坏了（真实原因是服务没运行，或它压根没有网页入口）。
    if (svc.kind !== 'tool' && svc.url) {
      actions += '<button type="button" class="btn" data-act="open" data-id="' + esc(svc.id) + '"'
        + ' title="用 ' + esc(browserLabel()) + ' 打开 ' + esc(svc.url) + '">打开网页</button>';
    }
    // 【删除】卡片上的「查看日志」按钮（2.4.2）：四列布局下操作区太挤。
    // 被删逻辑去向：日志入口保留在列表视图的「日志」按钮（data-act="log" 委托未动），
    // 需要看日志时切到列表视图即可；后台日志文件仍在 logs\ 目录可直接翻。

    var icon = iconOf(svc);
    // modified: 编辑模式下每张卡右上角出现「隐藏」按钮。平时不出现，
    // 避免日常操作时误点；隐藏后从首页移除，但后台照样采集状态。
    var eye = '';
    if (state.editMode) {
      eye = '<button type="button" class="eyeBtn" data-hide="' + esc(svc.id) + '"' +
        ' title="在首页隐藏这个服务" aria-label="隐藏 ' + esc(svc.name) + '">' +
        '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"' +
        ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M3.5 3.5l17 17"></path>' +
        '<path d="M10.6 10.6a2 2 0 0 0 2.8 2.8"></path>' +
        '<path d="M6.7 6.9C4.6 8.2 3.1 10.2 2.5 12c1 3 4.5 7 9.5 7 1.4 0 2.7-.3 3.8-.9"></path>' +
        '<path d="M9.8 5.2A9.7 9.7 0 0 1 12 5c5 0 8.5 4 9.5 7-.3 1-1 2.2-2.1 3.4"></path>' +
        '</svg></button>';
    }

    card.innerHTML =
      '<div class="cardHead">' +
        '<span class="svcIcon" style="--icon-h:' + icon.hue + '" aria-hidden="true">' +
          esc(icon.ch) + '</span>' +
        '<div class="cardName" title="' + esc(svc.name) + '">' + esc(svc.name) +
          '<span class="cardId">' + esc(svc.id) + '</span>' +
        '</div>' +
        '<span class="pill ' + stateClass(svc.state) + '"' + stateTip(svc) + '>' + esc(svc.state) + '</span>' +
        eye +
      '</div>' +
      '<div class="cardDesc">' + esc(svc.desc) + '</div>' +
      '<div class="cardMeta">' + meta + '</div>' +
      // modified: 2.5.1 版本号并进操作按钮同一行——原来 cardVer 独占一行
      // （虚线分隔 + 9px 上距 ≈ 30px 高度），17 张卡合计少滚近一屏。
      // 【删除】卡片上的「更新于 xx」文本：去向 = 版本号的 title 提示（悬停可见）、
      // 列表视图的「更新时间」列；信息没丢，只是不占版面。
      '<div class="cardActions">' + actions +
        '<span class="cardVersion" title="更新于 ' + esc(svc.updated || '—') + '">' +
          esc(svc.version || '—') + '</span>' +
      '</div>';

    // modified: 编辑模式下卡片本身也能拖——组内换序、跨组移动。
    // 平时不给 draggable，避免看页面时手滑把元素拖出来（浏览器默认拖动会拖走文字）。
    if (state.editMode) {
      card.setAttribute('draggable', 'true');
      card.classList.add('cardSortable');
    }
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
    if (key === 'group') {
      // modified: 2.4.2 列表「分组」列显示的是 groupOf（拖动后的组），
      // 排序也必须用它——原来走通用分支拿 a.group 原值，显示与排序不一致。
      return String(groupOf(a) || '').localeCompare(String(groupOf(b) || ''), 'zh');
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
        return '<th>' + esc(col.label) + '</th>';   // modified: 2.6.3 去掉恒空 class
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
          : '<button type="button" class="btn primary" data-act="start" data-id="' + esc(svc.id) + '"'
            + startTitle(svc) + '>启动</button>';   // modified: 2.6.3
        if (svc.running) {  // modified
          actions += '<button type="button" class="btn" data-act="restart" data-id="' + esc(svc.id) + '">重启</button>';
        }
      }
      // modified: 2.6.1 与卡片一致——没地址就不画按钮，避免一排灰按钮看着像坏了
      // modified: 2.6.3 补上 kind 判断：卡片排除了工具卡，列表漏了，两个视图不一致
      if (svc.kind !== 'tool' && svc.url) {
        actions += '<button type="button" class="btn" data-act="open" data-id="' + esc(svc.id) + '"'
          + ' title="用 ' + esc(browserLabel()) + ' 打开 ' + esc(svc.url) + '">打开网页</button>';
      }
      actions += '<button type="button" class="linkBtn" data-act="log" data-id="' + esc(svc.id) + '">日志</button>';

      return '<tr>' +
        '<td class="cName">' + esc(svc.name) + '<span class="cId">' + esc(svc.id) + '</span>' +
          '<div class="cDesc">' + esc(svc.desc) + '</div></td>' +
        '<td>' + esc(groupOf(svc)) + '</td>' +   // modified: 显示拖动后的有效分组
        '<td class="cVer"><code>' + esc(svc.version || '—') + '</code></td>' +
        '<td class="cUpd">' + esc(svc.updated || '—') + '</td>' +
        '<td><span class="pill ' + stateClass(svc.state) + '"' + stateTip(svc) + '">' + esc(svc.state) + '</span></td>' +
        '<td class="cPort">' + esc(svc.portText) + '</td>' +
        '<td class="cAct">' + actions + '</td>' +
      '</tr>';
    }).join('');

    var wrap = document.createElement('div');
    wrap.className = 'tableWrap glass';
    wrap.innerHTML = '<table class="svcTable"><thead><tr>' + head + '</tr></thead><tbody>' +
      body + '</tbody></table>';

    if (state.busy && Object.keys(state.busy).length) {
      Array.prototype.forEach.call(wrap.querySelectorAll('button[data-id]'), function (btn) {
        if (state.busy[btn.getAttribute('data-id')]) { btn.disabled = true; }  // modified: 不再拼选择器，id 带引号也不会注入
      });
    }
    el.content.appendChild(wrap);
  }

  // modified: 原 Bento 主卡（每组挑一张占两列）已停用——用户要求所有卡片等宽，
  // 这样 1440 宽下一行能排 4 张，一屏看到更多服务。
  // buildCard 的 hero 形参与 CSS 的 .is-hero 规则都保留着，将来要恢复只需
  // 在 renderContent 里给某张卡传 true。

  function buildGroupHead(name, items) {
    var head = document.createElement('header');
    head.className = 'groupHead';

    var running = items.filter(function (s) { return s.running; }).length;
    var startable = items.filter(function (s) {
      // modified: 2.6.3 端口冲突的服务启动了也只会秒退，别算进"可启动 N 项"
      return s.kind !== 'tool' && !s.running && s.autostart !== false &&
        s.state !== '端口冲突';
    }).length;
    var collapsed = groupCollapsed(name);
    // modified: 2.5.0 编辑模式下追加「改名 / 删除」，平时不显示
    var editActs = '';
    if (state.editMode) {
      editActs =
        '<button type="button" class="btn tiny" data-groupedit="rename" title="重命名分组">改名</button>' +
        '<button type="button" class="btn tiny danger" data-groupedit="delete" title="删除分组">删除</button>';
    }

    head.innerHTML =
      '<button type="button" class="groupToggle" aria-expanded="' + (collapsed ? 'false' : 'true') + '"' +
        ' title="' + (collapsed ? '展开' : '折叠') + '">' +
        '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor"' +
        ' stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M9 6l6 6-6 6"></path></svg></button>' +
      '<span class="groupName">' + esc(name) + '</span>' +
      '<span class="groupCount">' + items.length + ' 项' +
        (running ? ' · <b>' + running + ' 运行</b>' : '') + '</span>' +
      '<span class="groupActs">' +
        '<button type="button" class="btn tiny" data-groupact="start"' + (startable ? '' : ' disabled') +
          '>启动本组</button>' +
        '<button type="button" class="btn tiny" data-groupact="stop"' + (running ? '' : ' disabled') +
          '>停止本组</button>' +
        editActs +
      '</span>' +
      '<span class="dragGrip" title="拖动调整分组顺序" aria-hidden="true">' +
        '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">' +
        '<circle cx="9" cy="6" r="1.5"></circle><circle cx="15" cy="6" r="1.5"></circle>' +
        '<circle cx="9" cy="12" r="1.5"></circle><circle cx="15" cy="12" r="1.5"></circle>' +
        '<circle cx="9" cy="18" r="1.5"></circle><circle cx="15" cy="18" r="1.5"></circle>' +
        '</svg></span>';

    head.querySelector('.groupToggle').addEventListener('click', function () {
      setGroupCollapsed(name, !groupCollapsed(name));
      lastPrint = '';
      render();
    });

    Array.prototype.forEach.call(head.querySelectorAll('[data-groupact]'), function (btn) {
      btn.addEventListener('click', function () {
        batchGroup(name, btn.getAttribute('data-groupact'));
      });
    });

    // modified: 2.5.0 分组改名 / 删除
    Array.prototype.forEach.call(head.querySelectorAll('[data-groupedit]'), function (btn) {
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();   // 别让 groupHead 的拖动/折叠逻辑误触
        var act = btn.getAttribute('data-groupedit');
        if (act === 'rename') { renameGroup(name); }
        else if (act === 'delete') { deleteGroup(name); }
      });
    });
    return head;
  }

  // 组内批量启停：逐条串行发单条 action，而不是后端新开批量接口。
  // 串行是刻意的——几条 Python 进程同时冷启动会抢 CPU，串行反而更快看到结果。
  function batchGroup(name, action) {
    // modified: 2.6.3 连点「启动本组」会并行发起两轮，第二轮全撞"已在运行"
    var key = name + ':' + action;
    if (state.batchBusy[key]) { return; }
    state.batchBusy[key] = true;
    var label = action === 'start' ? '启动' : '停止';
    var items = visibleServices().filter(function (s) {
      if (groupOf(s) !== name) { return false; }   // modified
      if (action === 'start') {
        // modified: 2.6.3 端口被别人占着的服务启动了也只会秒退，别算进批量
        return s.kind !== 'tool' && !s.running && s.autostart !== false &&
          s.state !== '端口冲突';
      }
      return !!s.running;
    });
    if (!items.length) {
      delete state.batchBusy[key];
      toast('「' + name + '」没有需要' + label + '的服务', 'warn');
      return;
    }
    toast('正在' + label + '「' + name + '」' + items.length + ' 项…');
    var index = 0, done = 0, failed = 0;
    function step() {
      if (index >= items.length) {
        delete state.batchBusy[key];   // modified: 2.6.3
        toast(label + '完成：成功 ' + done + ' 项' + (failed ? '，失败 ' + failed + ' 项' : ''),
              failed ? 'warn' : 'ok');
        delayedRefresh();
        return;
      }
      var svc = items[index++];
      setBusy(svc.id, true);
      post('/api/action', { id: svc.id, action: action }).then(function (res) {
        if (res.ok) { done += 1; } else { failed += 1; }
      }).catch(function () {
        failed += 1;
      }).then(function () {
        setBusy(svc.id, false);
        step();
      });
    }
    step();
  }

  function makeDraggable(section) {
    var head = section.querySelector('.groupHead');
    if (!head) { return; }
    head.setAttribute('draggable', 'true');
    head.addEventListener('dragstart', function (ev) {
      state.dragging = true;
      state.dragEl = section;
      section.classList.add('dragging');
      try {
        ev.dataTransfer.effectAllowed = 'move';
        ev.dataTransfer.setData('text/plain', section.getAttribute('data-group') || '');
      } catch (e) {}
    });
    head.addEventListener('dragend', function () {
      section.classList.remove('dragging');
      state.dragging = false;
      state.dragEl = null;
      // modified: 2.4.2 搜索/分组筛选下 DOM 里只有部分分组，照 DOM 顺序落盘会把
      // 没显示的分组从 group_order 里挤掉（orderedGroups 会把它们甩到末尾）。
      // 此时放弃本次拖动，提示清筛选后再拖。
      if (state.keyword || state.group !== '全部') {
        toast('搜索/筛选中只显示了部分分组，本次顺序未保存。请切回「全部」并清空搜索后拖一次', 'warn');
        render();
        return;
      }
      // 松手时按当前 DOM 顺序落盘：拖到一半的中间态就是最终结果，
      // 不存在"预览与保存不一致"的问题。
      var order = Array.prototype.map.call(
        el.content.querySelectorAll('section.group'),
        function (node) { return node.getAttribute('data-group'); }
      ).filter(function (name) { return !!name; });
      saveGroupOrder(order);
      render();
    });
  }

  function renderHiddenBar() {
    var box = document.createElement('div');
    box.className = 'hiddenBar';
    var parts = ['<span class="hiddenLabel">已隐藏 ' + state.hiddenIds.length + ' 项</span>'];
    state.hiddenIds.forEach(function (id) {
      var svc = null;
      for (var i = 0; i < state.services.length; i++) {
        if (state.services[i].id === id) { svc = state.services[i]; break; }
      }
      parts.push('<button type="button" class="hiddenChip" data-show="' + esc(id) + '">' +
        esc(svc ? svc.name : id) + '<span>恢复</span></button>');
    });
    box.innerHTML = parts.join('');
    Array.prototype.forEach.call(box.querySelectorAll('[data-show]'), function (btn) {
      btn.addEventListener('click', function () { setHidden(btn.getAttribute('data-show'), false); });
    });
    el.content.appendChild(box);
  }

  // ================================================================ modified: 2.5.0 分组管理
  // 新建 / 改名 / 删除分组。全部只动 settings.json（group_order / service_order /
  // service_group），services.json 一个字节都不碰——它描述"服务是什么"，
  // 这里改的是"我怎么看它"。改名后原始分组名仍留在 services.json，想要可以改回来。

  function renderNewGroupBox() {
    var box = document.createElement('div');
    box.className = 'newGroupBox';
    box.innerHTML = '<button type="button" class="btn ghost" id="btnAddGroup">＋ 新建分组</button>';
    box.querySelector('button').addEventListener('click', addGroup);
    el.content.appendChild(box);
  }

  function inputDialog(title, hint, defaultValue, onOk) {
    modalOnClose = null;   // modified: 2.6.3 别把上一次确认框的取消钩子带进来
    el.modalTitle.textContent = title;
    el.modalBody.innerHTML =
      (hint ? '<p class="setHint" style="margin:0 0 10px">' + esc(hint) + '</p>' : '') +
      '<input type="text" class="inputDlg" id="inputDlgValue" maxlength="64"' +
      ' value="' + esc(defaultValue || '') + '">';
    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn';
    cancel.textContent = '取消';
    cancel.addEventListener('click', closeModal);
    var ok = document.createElement('button');
    ok.type = 'button';
    ok.className = 'btn primary';
    ok.textContent = '确定';
    ok.addEventListener('click', function () {
      var val = document.getElementById('inputDlgValue').value;
      closeModal();
      onOk(val);
    });
    el.modalFoot.appendChild(cancel);
    el.modalFoot.appendChild(ok);
    el.modalMask.hidden = false;
    var input = document.getElementById('inputDlgValue');
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); ok.click(); }
    });
    input.focus();
    input.select();
  }

  function allGroupNames() {
    // modified: 2.6.3 普通对象的原型链键（constructor / toString /
    // __proto__）会被误判成"已存在同名分组"，改用无原型对象。
    var names = Object.create(null);
    state.groupOrder.forEach(function (n) { names[n] = true; });
    state.services.forEach(function (s) { names[groupOf(s)] = true; });
    return names;
  }

  function addGroup() {
    inputDialog('新建分组', '输入分组名称（最多 64 字）', '', function (raw) {
      var name = String(raw || '').trim();
      if (!name) { toast('分组名不能为空', 'warn'); return; }
      if (name.length > 64) { toast('分组名太长（最多 64 字）', 'warn'); return; }
      if (Object.prototype.hasOwnProperty.call(allGroupNames(), name)) {
        toast('已经存在同名分组「' + name + '」', 'warn'); return;
      }
      // modified: 新组排到当前显示顺序的末尾。
      // 直接 push(groupOrder) 会让它挤在"已登记组"的最前面（未登记组排在登记组之后），
      // 与"拖到最后"的手感不一致；把完整顺序+新名固化下来，落盘语义与拖动完全等价。
      // modified: 2.6.3 原来直接用 orderedGroups() 覆盖 groupOrder。
      // 那个函数只从"有可见服务"的组里取名，连续新建两个空组时，
      // 第一个空组会被丢掉并写进 settings.json，刷新后找不回来。
      var full = state.groupOrder.slice();
      orderedGroups().forEach(function (n) {
        if (full.indexOf(n) < 0) { full.push(n); }
      });
      full.push(name);
      state.groupOrder = full;
      lastPrint = '';
      chipsSig = '';
      render();
      saveSettings({ group_order: state.groupOrder },
        '已新建分组「' + name + '」，把卡片拖进来即可');
    });
  }

  function renameGroup(oldName) {
    inputDialog('重命名分组', '给「' + oldName + '」起个新名字', oldName, function (raw) {
      var next = String(raw || '').trim();
      if (!next) { toast('分组名不能为空', 'warn'); return; }
      if (next.length > 64) { toast('分组名太长（最多 64 字）', 'warn'); return; }
      if (next === oldName) { return; }
      var names = allGroupNames();
      delete names[oldName];
      if (Object.prototype.hasOwnProperty.call(names, next)) {
        toast('已经存在同名分组「' + next + '」', 'warn'); return;
      }
      // 三处联动：显示顺序 / 组内顺序 / 卡片归属覆盖
      state.groupOrder = state.groupOrder.map(function (n) { return n === oldName ? next : n; });
      // modified: 2.6.3 只搬自有记录，别把原型链上的函数当成"组内顺序"
      if (Object.prototype.hasOwnProperty.call(state.serviceOrder, oldName)) {
        state.serviceOrder[next] = state.serviceOrder[oldName];
        delete state.serviceOrder[oldName];
      }
      Object.keys(state.serviceGroup).forEach(function (id) {
        if (state.serviceGroup[id] === oldName) { state.serviceGroup[id] = next; }
      });
      // 出厂分组名就是 oldName 的服务也要写覆盖，否则显示会分裂成新旧两个组
      state.services.forEach(function (s) {
        if (s.group === oldName && !state.serviceGroup[s.id]) { state.serviceGroup[s.id] = next; }
      });
      // 折叠状态跟着迁移
      try {
        if (localStorage.getItem('hub.collapse.' + oldName) === '1') {
          localStorage.setItem('hub.collapse.' + next, '1');
        }
        localStorage.removeItem('hub.collapse.' + oldName);
      } catch (e) {}
      lastPrint = '';
      chipsSig = '';
      render();
      saveSettings({
        group_order: state.groupOrder,
        service_order: state.serviceOrder,
        service_group: state.serviceGroup
      }, '分组已改名为「' + next + '」');
    });
  }

  function deleteGroup(name) {
    // 出厂分组（services.json 里有服务的原始 group 就是它）不能删：
    // 覆盖删掉后这些服务又会把这个组"长"出来，删了等于没删。
    var natives = state.services.filter(function (s) { return s.group === name; });
    if (natives.length) {
      toast('「' + name + '」是 ' + natives.length + ' 个服务的原始分组，不能删除。' +
        '可以点「改名」，或把这些服务拖到别的组', 'warn');
      return;
    }
    var members = state.services.filter(function (s) { return groupOf(s) === name; });
    confirmDialog(members.length
      ? '删除分组「' + name + '」？组内 ' + members.length + ' 项将回到原来分组。'
      : '删除空分组「' + name + '」？', function () {
      // 组内卡片删掉归属覆盖 → 回 services.json 原分组
      Object.keys(state.serviceGroup).forEach(function (id) {
        if (state.serviceGroup[id] === name) { delete state.serviceGroup[id]; }
      });
      state.groupOrder = state.groupOrder.filter(function (n) { return n !== name; });
      delete state.serviceOrder[name];
      setGroupCollapsed(name, false);
      lastPrint = '';
      chipsSig = '';
      render();
      saveSettings({
        group_order: state.groupOrder,
        service_order: state.serviceOrder,
        service_group: state.serviceGroup
      }, '已删除分组「' + name + '」');
    });
  }
  // ================================================================ 分组管理 end

  function renderContent(list) {
    el.content.innerHTML = '';
    if (!state.services.length) {
      el.content.innerHTML = '<div class="empty">还没有登记任何服务，去 services.json 里加一条就行。</div>';
      return;
    }
    if (!list.length) {
      el.content.innerHTML = '<div class="empty">' +
        (state.hiddenIds.length && !state.keyword
          ? '当前可见的服务都被隐藏了。点顶栏「布局」→ 底部可恢复。'
          : '没有匹配的服务。') + '</div>';
      // modified: 2.6.0 空态也要给恢复入口。原来在这里直接 return，
      // 全部卡片被隐藏时连「已隐藏 N 项」恢复栏都不渲染——首页没有任何办法把卡找回来，
      // 只能去设置里翻（用户以为数据丢了）。编辑模式下照常渲染恢复栏与新建分组入口。
      if (state.editMode && state.hiddenIds.length) { renderHiddenBar(); }
      if (state.editMode) { renderNewGroupBox(); }
      return;
    }
    if (state.view === 'list') {
      renderList(list);
      return;
    }

    // modified: 2.6.3 组名是任意字符串；普通对象里 byGroup['constructor']
    // 会拿到 Object 构造函数，items.filter 抛异常、整页白屏。改无原型字典。
    var byGroup = Object.create(null);
    var present = [];
    list.forEach(function (svc) {
      // modified: 用 groupOf() 而不是 svc.group——拖动过的卡可能已被移到别的组
      var g = groupOf(svc);
      if (!byGroup[g]) { byGroup[g] = []; present.push(g); }
      byGroup[g].push(svc);
    });

    // modified: 分组顺序由 group_order 决定；搜索/筛选后只剩部分分组时，
    // 依然按用户拖出来的顺序排，不会因为过滤而跳回默认顺序。
    var order = [];
    // modified: 2.5.0 无筛选时显示空组（新建分组先是个空壳，等卡片拖进来）；
    // 搜索/筛选下不显示空组，免得一堆空壳占屏。
    var showEmpty = !state.keyword && state.group === '全部';
    state.groupOrder.forEach(function (name) {
      if ((byGroup[name] || showEmpty) && order.indexOf(name) < 0) { order.push(name); }
    });
    present.forEach(function (name) {
      if (order.indexOf(name) < 0) { order.push(name); }
    });

    order.forEach(function (name) {
      var items = orderedItems(name, byGroup[name] || []);   // modified: 空组给空数组
      var section = document.createElement('section');
      section.className = 'group' + (groupCollapsed(name) ? ' collapsed' : '');
      section.setAttribute('data-group', name);
      section.appendChild(buildGroupHead(name, items));
      if (!groupCollapsed(name)) {
        var grid = document.createElement('div');
        grid.className = 'grid';
        // modified: 不再挑主卡，全部等宽（一行 4 张）
        items.forEach(function (svc) { grid.appendChild(buildCard(svc, false)); });
        // modified: 2.5.0 编辑模式下组末尾放"拖到这里"占位，提示空位可以拖入
        if (state.editMode) {
          var slot = document.createElement('div');
          slot.className = 'dropSlot';
          slot.textContent = '+ 拖到这里';
          grid.appendChild(slot);
        }
        section.appendChild(grid);
      }
      if (state.editMode) { makeDraggable(section); }
      el.content.appendChild(section);
    });

    if (state.editMode && state.hiddenIds.length) { renderHiddenBar(); }
    if (state.editMode) { renderNewGroupBox(); }  // modified: 2.5.0 新建分组入口
  }

  // modified: 卡片布局的指纹。必须排序后再拼，否则对象键顺序一变就误判成"布局变了"，
  // 白白触发一次整块重建（页面会闪一下）。
  function layoutSignature() {
    var g = Object.keys(state.serviceGroup).sort().map(function (k) {
      return k + '=' + state.serviceGroup[k];
    }).join(';');
    var o = Object.keys(state.serviceOrder).sort().map(function (k) {
      var seq = state.serviceOrder[k];   // modified: 2.6.3 脏值不再让签名计算抛异常
      return k + ':' + (Array.isArray(seq) ? seq.join(',') : '');
    }).join(';');
    return g + '|' + o;
  }

  function fingerprint(list) {
    // modified: 2.6.3 两处一起改：
    // ① 补 portConflict——占用者换人时胶囊 title 原来永不更新（2.6.2 回归）；
    // ② 用 JSON.stringify 取代裸分隔符 join，服务名/分组名里带 | ~ # 时
    //    不同数据可能拼出同一个签名，卡片会漏重绘。
    var rows = list.map(function (s) {
      return JSON.stringify([s.id, s.state, s.portText, s.url, s.pid, s.version,
              s.updated, s.name, s.desc, s.group, s.kind, s.autostart,
              s.running, s.lastExit, s.missing, s.portConflict || null]);
    });
    // modified: 编辑模式 / 密度 / 隐藏 / 分组顺序 也计入指纹，
    // 否则改了这几项不会触发卡片区重建（它们不在服务数据里）。
    return [state.view, state.keyword, state.group, state.sort.key, state.sort.dir,
            state.editMode, state.density,
            state.hiddenIds.join(','), state.groupOrder.join(','),
            layoutSignature(),
            Object.keys(state.busy).join(','), JSON.stringify(rows)].join('#');
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
    // modified: 拖动进行中冻结重绘——每 2 秒的轮询重建 DOM 会把正在拖的节点抽走
    if (state.dragging) { return; }
    // modified: 2.6.3 离线提示改由 render 统一产出。原来 refresh 的 catch
    // 直接 el.content.innerHTML = ...，绕过 dragging 冻结（拖分组时会把
    // 顺序读成空数组），且随便一次交互重绘就把提示抹掉。
    if (state.offline) {
      var html = '<div class="offline">服务台后台没有响应（' + esc(state.offline) + '）。' +
        '双击桌面「服务台」重新唤起；已启动的业务服务不受影响。</div>';
      if (offlinePrinted !== html) { el.content.innerHTML = html; offlinePrinted = html; }
      lastPrint = '';
      return;
    }
    offlinePrinted = '';
    var list = visibleServices().filter(matches);
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
    var url = 'http://127.0.0.1:' + hub.port + '/';
    // modified: 2.5.0 地址做成可点击复制（data-copy），其余信息照旧拼
    var bits = [
      '<span class="copyable" data-copy="' + esc(url) + '" title="点击复制服务台地址">' +
        esc('127.0.0.1:' + hub.port) + '</span>',
      'v' + esc(hub.version || '?'),
      'Python ' + esc(hub.python),
      '已运行 ' + esc(humanUptime(hub.uptime) || '刚刚')
    ];
    if (hub.tray) { bits.push('托盘常驻'); }
    if (!hub.psutil) { bits.push('psutil 缺失'); }
    // modified: 2.6.3 前两次轮询失败也要有轻量提示，别等满 3 次才说
    if (state.failures) { bits.push('<span style="color:var(--warn)">后台连接中断，正在重试</span>'); }
    el.hubMeta.innerHTML = bits.join(' · ');
    el.hubMeta.title = '安装目录：' + (hub.appdir || '') + '\n日志目录：' + (hub.logdir || '');
  }

  // modified: 2.5.0 复制到剪贴板。优先 clipboard API（127.0.0.1 属安全上下文），
  // 旧内核/非安全上下文退回 textarea + execCommand。
  function fallbackCopy(text) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      return true;
    } catch (e) {
      return false;
    }
  }
  function copyText(text) {
    var done = function () { toast('已复制 ' + text, 'ok'); };
    var fail = function () {
      if (fallbackCopy(text)) { done(); }
      else { toast('复制失败，请手动选择复制', 'error'); }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fail);
    } else {
      fail();
    }
  }
  el.hubMeta.addEventListener('click', function (event) {
    var node = event.target.closest ? event.target.closest('[data-copy]') : null;
    if (node) { copyText(node.getAttribute('data-copy')); }
  });

  // ------------------------------------------------------------------ 轮询
  function refresh(quiet) {
    return get('/api/status').then(function (payload) {
      state.failures = 0;
      // modified: 2.6.3 恢复连接时强制重绘一次，别留着离线横幅
      if (state.offline) { state.offline = ''; offlinePrinted = ''; lastPrint = ''; }
      state.fetchedAt = Date.now();
      state.services = payload.services || [];
      state.hub = payload.hub || {};
      renderHub();
      render();
    }).catch(function (err) {
      state.failures += 1;
      renderHub();   // modified: 2.6.3 马上在顶栏标出连接中断
      if (state.failures >= 3) {
        state.offline = err.message || '未知错误';
        lastPrint = '';
        render();
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
    var awaitingConfirm = false;   // modified: 2.6.3
    setBusy(id, true);
    render();
    return post('/api/action', { id: id, action: action, force: !!force }).then(function (result) {
      if (result.needConfirm) {
        // modified: 2.6.3 原来这里 return 之后末尾 .then 照样把 busy 解掉，
        // 确认框还开着时按钮已可重复点；两个确认交错还会串台。
        awaitingConfirm = true;
        confirmDialog(result.message, function () {
          setBusy(id, false);
          doAction(id, action, true);
        }, function () {
          setBusy(id, false);
          render();
        });
        return;
      }
      toast(result.message || '已执行', result.ok ? 'ok' : 'warn');
      return delayedRefresh();
    }).catch(function (err) {
      toast('操作失败：' + err.message, 'error');
    }).then(function () {
      if (awaitingConfirm) { return; }   // modified: 等用户确认，别提前解锁
      setBusy(id, false);
      render();
    });
  }

  function batched(action, title) {
    // modified: 2.6.3 顶栏「全部启动/停止」连点会并行发起两轮批量操作
    if (state.batchAll) { toast('还有批量操作在进行中', 'warn'); return; }
    state.batchAll = true;
    confirmDialog('确定要' + title + '吗？', function () {
      toast(title + '中…');
      post('/api/batch', { action: action }).then(function (result) {
        toast(result.message || '完成', result.ok ? 'ok' : 'warn');
        delayedRefresh();
      }).catch(function (err) { toast('批量操作失败：' + err.message, 'error');
      }).then(function () { state.batchAll = false; });
    }, function () { state.batchAll = false; });
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
  function closeModal() {
    el.modalMask.hidden = true;
    // modified: 2.6.3 取消确认框（含 ESC、点遮罩、右上角 ×）时也要清理，
    // 否则 doAction 为等确认保留的 busy 会永远禁用按钮。
    var fn = modalOnClose;
    modalOnClose = null;
    if (fn) { fn(); }
  }

  function confirmDialog(message, onYes, onNo) {
    modalOnClose = onNo || null;
    el.modalTitle.textContent = '请确认';
    el.modalBody.textContent = message;
    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    var yes = document.createElement('button');
    yes.type = 'button';
    yes.className = 'btn danger';
    yes.textContent = '确定';
    yes.addEventListener('click', function () {
      modalOnClose = null;   // modified: 点"确定"不算取消
      closeModal();
      onYes();
    });
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
    modalOnClose = null;   // modified: 2.6.3
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
    // modified: 服务端可能存着选项表里没有的值（如手动改成 3000ms），补一个临时项，避免显示错位
    var known = options.some(function (opt) { return String(opt[0]) === String(value); });
    if (!known && value !== undefined && value !== null && value !== '') {
      options = options.concat([[String(value), String(value) + ' ms']]);
    }
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

  // modified: 2.6.0 服务端设置 → 前端状态的统一同步口。
  // 原来这段逻辑写在 init 的 get('/api/settings') 里，新增「从备份恢复」时又需要一遍；
  // 抽出来共用，以后加设置项只改这一处，不会出现"恢复时漏同步某项"。
  function adoptSettings(cfg, options) {
    options = options || {};
    state.settings = cfg || {};
    if (typeof state.settings.refresh_ms === 'number') { state.refreshMs = state.settings.refresh_ms; }
    if (state.settings.theme && !options.keepTheme) { applyTheme(state.settings.theme, true); }
    if (state.settings.view && !options.keepView) { setView(state.settings.view, true); }
    applyBgMotion(state.settings.bg_motion);
    // 首页布局偏好：数组/对象都做类型兜底，手改坏的 settings.json 不至于让首页白屏
    state.hiddenIds = Array.isArray(state.settings.hidden_ids) ? state.settings.hidden_ids : [];
    state.groupOrder = Array.isArray(state.settings.group_order) ? state.settings.group_order : [];
    var sg = state.settings.service_group;
    state.serviceGroup = noProto(sg);   // modified: 2.6.3
    var so = state.settings.service_order;
    state.serviceOrder = noProto(so);   // modified: 2.6.3
    applyDensity(state.settings.density || 'comfortable');
    chipsSig = '';
    lastPrint = '';
    render();
  }

  // ================================================================ modified: 2.6.0 设置备份 / 恢复
  // 布局（分组顺序、卡片归属、隐藏名单）只存在 runtime\settings.json，
  // 一次误操作或自动化测试就可能被清掉（2.5.0 验收脚本就清过一次，靠手动备份救回）。
  // 参考 Homepage / Homarr 的"配置可导出备份"做法：导出 JSON 下载，导入先给摘要再确认。
  // 不新增后端接口：导入仍走 /api/settings，服务端 settings 层有白名单与类型校验。
  var IMPORT_KEYS = ['auto_open', 'browser', 'browser_path', 'theme', 'view', 'refresh_ms',
                     'bg_motion', 'hidden_ids', 'group_order', 'density',
                     'service_group', 'service_order'];

  function exportSettings() {
    var payload = {
      app: 'ServiceHub',
      version: (state.hub || {}).version || '',
      saved_at: new Date().toISOString(),
      settings: state.settings || {}
    };
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    var a = document.createElement('a');
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    var now = new Date();
    a.href = URL.createObjectURL(blob);
    a.download = 'servicehub-settings-' + now.getFullYear() + pad(now.getMonth() + 1) +
      pad(now.getDate()) + '-' + pad(now.getHours()) + pad(now.getMinutes()) + '.json';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    toast('已导出备份（含首页布局），在浏览器下载目录里', 'ok');
  }

  function importSettings(file) {
    if (!file) { return; }
    // modified: 2.6.3 几百 MB 的文件会把主线程堵死，设置备份正常只有几 KB
    if (file.size > 1024 * 1024) {
      toast('备份文件超过 1MB，已拒绝读取', 'error');
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      var parsed;
      try { parsed = JSON.parse(String(reader.result || '')); }
      catch (e) { toast('这个文件不是合法的 JSON，无法恢复', 'error'); return; }
      // 既接受本工具导出的 {app, settings:{...}}，也接受直接拖进来的 settings.json
      var src = (parsed && parsed.settings && typeof parsed.settings === 'object')
        ? parsed.settings : parsed;
      if (!src || typeof src !== 'object' || Array.isArray(src)) {
        toast('文件里没有可识别的设置内容', 'error'); return;
      }
      var patch = {};
      IMPORT_KEYS.forEach(function (key) { if (key in src) { patch[key] = src[key]; } });
      if (!Object.keys(patch).length) {
        toast('文件里没有服务台认识的设置项', 'error'); return;
      }
      // modified: 2.6.3 类型不对的字段会让摘要算出"undefined 组"，确认后
      // 还把坏值 POST 给后端。前端先挡一道，服务端 normalize 仍是最后防线。
      var badTypes = [];
      ['hidden_ids', 'group_order'].forEach(function (k) {
        if (k in patch && !Array.isArray(patch[k])) { badTypes.push(k); }
      });
      ['service_group', 'service_order'].forEach(function (k) {
        if (k in patch && (!patch[k] || typeof patch[k] !== 'object' ||
                           Array.isArray(patch[k]))) { badTypes.push(k); }
      });
      if (badTypes.length) {
        toast('备份里这些字段类型不对，已拒绝恢复：' + badTypes.join('、'), 'error');
        return;
      }
      var brief = '将从备份恢复：主题 ' + (patch.theme || '保持') +
        '、视图 ' + (patch.view || '保持') +
        '、密度 ' + (patch.density || '保持') +
        '；隐藏 ' + ((patch.hidden_ids || []).length || 0) + ' 项' +
        '、分组顺序 ' + ((patch.group_order || []).length || 0) + ' 组' +
        '、卡片归属 ' + Object.keys(patch.service_group || {}).length + ' 条。' +
        '当前布局会被覆盖，确定继续吗？';
      confirmDialog(brief, function () {
        saveSettings(patch, null).then(function (res) {
          // 成功：用服务端 normalize 后的值同步；失败：回滚界面到旧设置（saveSettings 已弹错误）
          adoptSettings(state.settings, {});
          if (!res) { return; }
          closeModal();
          toast('已从备份恢复设置与首页布局', 'ok');
        });
      });
    };
    reader.onerror = function () { toast('读取文件失败', 'error'); };
    reader.readAsText(file, 'utf-8');
  }

  // modified: 2.6.3 布局保存失败时从服务端重新拉回真实设置，只同步布局字段
  // （不动主题/视图，免得打断正在进行的编辑），让界面与磁盘保持一致。
  function syncLayoutFromServer() {
    return get('/api/settings').then(function (payload) {
      var cfg = payload.settings || {};
      state.settings = cfg;
      state.hiddenIds = Array.isArray(cfg.hidden_ids) ? cfg.hidden_ids : [];
      state.groupOrder = Array.isArray(cfg.group_order) ? cfg.group_order : [];
      var sg = cfg.service_group;
      state.serviceGroup = noProto(sg);   // modified: 2.6.3
      var so = cfg.service_order;
      state.serviceOrder = noProto(so);   // modified: 2.6.3
      chipsSig = '';
      lastPrint = '';
      render();
    }).catch(function () {});
  }

  function saveSettings(patch, note) {
    return post('/api/settings', patch).then(function (payload) {
      state.settings = payload.settings || state.settings;
      if (payload.browserResolved) { state.browserResolved = payload.browserResolved; }  // modified: 2.6.1
      if (typeof state.settings.refresh_ms === 'number') {
        state.refreshMs = state.settings.refresh_ms;
      }
      if (note) { toast(note, 'ok'); }
      schedule();
      return payload;
    }).catch(function (err) {
      toast('保存失败：' + err.message, 'error');
      syncLayoutFromServer();   // modified: 2.6.3 界面别和磁盘分叉
    });
  }

  function openSettings() {
    // modified: 2.6.3 首屏 /api/settings 还没回来时点开设置，面板里全是
    // 默认值，用户据此改动会误改配置。先等设置就绪再渲染面板。
    if (state.settingsReady) { openSettingsPanel(); return; }
    get('/api/settings').then(function (payload) {
      state.browsers = payload.browsers || [];
      state.browserResolved = payload.browserResolved || null;
      state.autostart = payload.autostart || {};
      // modified: 只备齐面板要读的数据，不调 adoptSettings——那会顺带改
      // 主题/视图，把 URL 里的 ?theme= / ?view= 覆盖掉。
      state.settings = payload.settings || {};
      state.settingsReady = true;
    }).catch(function () {}).then(function () { openSettingsPanel(); });
  }

  function openSettingsPanel() {
    modalOnClose = null;   // modified: 2.6.3
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

    // modified: 背景动效开关（默认关，见 style.css 的 html.bg-motion）
    var bgMotionRow = settingsRow('背景动效',
      '三团光斑缓慢漂移。部分浏览器 + 集显组合下持续运动会整屏闪烁，默认关闭；关掉后页面彻底静止',
      switchCtrl(!!cfg.bg_motion, function (checked) {
        applyBgMotion(checked);
        saveSettings({ bg_motion: checked }, '背景动效已' + (checked ? '打开' : '关闭'));
      }));

    // ---- 行为
    var autoOpenRow = settingsRow('启动服务后自动打开网页',
      '只对带网页入口的服务生效；动态端口服务会等端口出来再打开',
      switchCtrl(cfg.auto_open, function (checked) {
        saveSettings({ auto_open: checked }, '已保存自动开网页 = ' + (checked ? '开' : '关'));
      }));

    // modified: 开机自动启动（写 HKCU Run，注册表才是真相，所以不进 settings.json）
    var au = state.autostart || {};
    var auHint = '写当前用户的注册表启动项（HKCU\\...\\Run），不需要管理员权限；'
      + '开机只让托盘出现，不会自动弹浏览器';
    if (au.supported === false) { auHint = '只有 Windows 才支持开机自启'; }
    if (au.stale) { auHint += '。⚠ 注册表里登记的还是旧路径（程序目录挪过？），重新打开一次即可更新'; }
    var auSwitch = null;
    auSwitch = switchCtrl(!!au.enabled, function (checked) {
      post('/api/settings', { autostart: checked }).then(function (res) {
        state.autostart = res.autostart || {};
        if (res.settings) { state.settings = res.settings; }
        toast(res.message || (checked ? '已设为开机自启' : '已取消开机自启'), 'ok');
      }).catch(function (err) {
        toast('设置失败：' + err.message, 'error');
        var box = auSwitch && auSwitch.querySelector('input');  // 失败要把开关拨回去
        if (box) { box.checked = !checked; }
      });
    });
    var autostartRow = settingsRow('开机自动启动', auHint, auSwitch);

    var browserOptions = [['auto', '系统默认浏览器']];
    (state.browsers || []).forEach(function (b) {
      browserOptions.push([b.id, b.name + '（' + b.id + '）']);
    });
    browserOptions.push(['custom', '自定义路径…']);

    // modified: 2.6.1 记住下拉框「当前选中的值」。原来测试按钮读的是 state.settings，
    // 而改设置是异步落盘的——刚选完 ego 就点测试，可能还在用旧的 auto，
    // 结果又开了 Edge，看起来就像设置没生效。
    var pickedBrowser = cfg.browser || 'auto';
    var pickedPath = cfg.browser_path || '';

    var resolvedNote = document.createElement('div');
    resolvedNote.className = 'setResolved';
    function paintResolved(info) {
      info = info || state.browserResolved || null;
      if (!info) {
        resolvedNote.className = 'setResolved';
        resolvedNote.textContent = '当前设置：' + browserLabel();
        return;
      }
      if (info.reason) {
        resolvedNote.className = 'setResolved warn';
        resolvedNote.textContent = '⚠ ' + info.reason + '——点「打开网页」会退回系统默认浏览器（本机是 Edge）。';
        return;
      }
      resolvedNote.className = 'setResolved';
      if (info.mode === 'auto') {
        resolvedNote.textContent = '实际使用：系统默认浏览器（本机是 Edge）';
      } else {
        resolvedNote.textContent = '实际使用：' + (info.name || '') +
          (info.path ? '　' + info.path : '');
        resolvedNote.title = info.path || '';
      }
    }

    var browserRow = settingsRow('打开网页用哪个浏览器',
      '服务台的「打开网页」和托盘入口都走这里选定的浏览器',
      selectCtrl(browserOptions, pickedBrowser, function (value) {
        pickedBrowser = value;
        pathRow.style.display = value === 'custom' ? '' : 'none';
        saveSettings({ browser: value }, '浏览器已设为 ' + value).then(function (payload) {
          paintResolved(payload && payload.browserResolved);   // modified: 用后端算出的实际结果回填
        });
      }));
    browserRow.querySelector('.setMain').appendChild(resolvedNote);
    paintResolved();

    var pathInput = document.createElement('input');
    pathInput.type = 'text';
    pathInput.placeholder = '例如 C:\\Program Files\\...\\chrome.exe';
    pathInput.value = cfg.browser_path || '';
    var pathRow = settingsRow('自定义浏览器路径', '仅当上面选「自定义路径」时使用；可直接粘贴 exe 完整路径', pathInput);
    pathRow.style.display = (cfg.browser || 'auto') === 'custom' ? '' : 'none';
    pathInput.addEventListener('change', function () {
      pickedPath = pathInput.value.trim();
      pickedBrowser = 'custom';
      saveSettings({ browser_path: pickedPath, browser: 'custom' }, '自定义浏览器路径已保存')
        .then(function (payload) { paintResolved(payload && payload.browserResolved); });
    });

    var testBtn = document.createElement('button');
    testBtn.type = 'button';
    testBtn.className = 'btn';
    testBtn.textContent = '用这个浏览器打开服务台';
    testBtn.addEventListener('click', function () {
      // modified: 2.6.1 用下拉框当前值，并显示实际用了哪个浏览器（含退回提示）
      post('/api/browser/test', {
        browser: pickedBrowser,
        browser_path: pickedPath
      }).then(function (res) {
        toast(res.message || '已尝试打开', res.ok && !res.fallback ? 'ok' : 'warn');
        if (res.fallback) {
          // 后端明确说了「退回系统默认」，就直接在设置里留一条红字，别让用户下次再猜
          resolvedNote.className = 'setResolved warn';
          resolvedNote.textContent = '⚠ 刚才那次没有用上指定浏览器，已退回系统默认（Edge）。';
        }
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

    // ---- 首页布局（2.4.0 新增，2.4.1 扩到卡片拖拽）
    // modified: 密度——两档都是等宽四列，区别只在信息多少与间距。
    var densityRow = settingsRow('卡片密度',
      '舒适：说明两行、间距宽松；紧凑：说明一行、间距更小。两档都是等宽四列，版本号都在按钮行右端',
      selectCtrl([
        ['comfortable', '舒适'], ['compact', '紧凑']
      ], cfg.density || state.density, function (value) {
        applyDensity(value);
        lastPrint = '';
        render();
        saveSettings({ density: value }, '密度已改为' + (value === 'compact' ? '紧凑' : '舒适'));
      }));

    // modified: 已隐藏卡片的恢复入口 + 分组顺序复位。
    // 这两个是「布局编辑」的后悔药——隐藏了找不到、拖乱了想还原时都从这里走。
    var hiddenBox = document.createElement('div');
    hiddenBox.className = 'hiddenManage';
    function paintHidden() {
      hiddenBox.innerHTML = '';
      if (!state.hiddenIds.length) {
        hiddenBox.innerHTML = '<span class="hiddenNone">没有隐藏任何卡片</span>';
        return;
      }
      state.hiddenIds.forEach(function (id) {
        var svc = null;
        for (var i = 0; i < state.services.length; i++) {
          if (state.services[i].id === id) { svc = state.services[i]; break; }
        }
        var line = document.createElement('div');
        line.className = 'hiddenManageRow';
        var label = document.createElement('span');
        label.textContent = svc ? svc.name : id + '（已不在注册表）';
        var back = document.createElement('button');
        back.type = 'button';
        back.className = 'btn tiny';
        back.textContent = '恢复';
        back.addEventListener('click', function () { setHidden(id, false); paintHidden(); });
        line.appendChild(label);
        line.appendChild(back);
        hiddenBox.appendChild(line);
      });
    }
    paintHidden();
    var hiddenRow = settingsRow('首页隐藏的卡片',
      '在首页点「布局」后，卡片右上角会出现眼睛按钮；隐藏后到这里恢复', hiddenBox);

    var orderReset = document.createElement('button');
    orderReset.type = 'button';
    orderReset.className = 'btn tiny';
    orderReset.textContent = '恢复默认顺序';
    orderReset.addEventListener('click', function () {
      // modified: 2.4.1 一并清掉卡片的组内顺序和跨组归属，
      // 否则"恢复默认"只还原了分组，卡片还杵在拖过去的位置，看着像没生效。
      state.groupOrder = [];
      state.serviceOrder = Object.create(null);  // modified: 2.6.3 还原后仍保持无原型
      state.serviceGroup = Object.create(null);  // modified: 2.6.3
      chipsSig = '';
      lastPrint = '';
      render();
      saveSettings({
        group_order: [],
        service_order: {},
        service_group: {}
      }, '分组顺序、卡片顺序与分组归属都已还原为 services.json 里的样子');
      closeModal();
    });
    var orderRow = settingsRow('分组顺序与卡片位置',
      '在首页点「布局」后：拖卡片可换组内位置或拖到别的组；拖分组标题可换分组先后。'
      + '全部记在设置里，不动 services.json', orderReset);

    // modified: 2.6.0 备份 / 恢复入口（函数见上方 exportSettings / importSettings）
    var backupBox = document.createElement('div');
    backupBox.style.display = 'flex';
    backupBox.style.gap = '6px';
    var exportBtn = document.createElement('button');
    exportBtn.type = 'button';
    exportBtn.className = 'btn tiny';
    exportBtn.textContent = '导出备份';
    exportBtn.addEventListener('click', exportSettings);
    var importBtn = document.createElement('button');
    importBtn.type = 'button';
    importBtn.className = 'btn tiny';
    importBtn.textContent = '从备份恢复';
    var fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = '.json,application/json';
    fileInput.style.display = 'none';
    fileInput.addEventListener('change', function () {
      importSettings(fileInput.files && fileInput.files[0]);
      fileInput.value = '';   // modified: 清空才能连续选同一个文件再触发 change
    });
    importBtn.addEventListener('click', function () { fileInput.click(); });
    backupBox.appendChild(exportBtn);
    backupBox.appendChild(importBtn);
    backupBox.appendChild(fileInput);
    var backupRow = settingsRow('备份与恢复',
      '把当前设置与首页布局（分组顺序、卡片位置、隐藏名单）导出成 JSON；'
      + '布局被改乱、或换电脑时，从备份一键恢复（导入前会先给摘要再确认）', backupBox);

    el.modalBody.appendChild(settingsGroup('外观', [themeRow, viewRow, refreshRow, bgMotionRow]));
    el.modalBody.appendChild(settingsGroup('首页布局', [densityRow, orderRow, backupRow, hiddenRow]));
    el.modalBody.appendChild(settingsGroup('行为', [autostartRow, autoOpenRow, browserRow, pathRow]));
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
    // modified: 编辑模式下的「眼睛」按钮——把卡片从首页隐藏（可恢复）
    var eyeBtn = event.target.closest('button[data-hide]');
    if (eyeBtn) {
      setHidden(eyeBtn.getAttribute('data-hide'), true);
      return;
    }
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

  // modified: 2.4.1 卡片拖拽（组内换序 + 跨组移动）。全部走事件委托，不给每张卡绑监听。
  // 拖动期间只更新「落点提示」，不改 DOM 顺序——松手才一次性改数据并重建，
  // 这样不会出现「拖到一半页面乱掉、松手又跳回去」。
  function clearDropHint() {
    Array.prototype.forEach.call(
      el.content.querySelectorAll('.drop-before, .drop-after, .drop-end'),
      function (node) { node.classList.remove('drop-before', 'drop-after', 'drop-end'); }
    );
  }

  function setDropHint(card, section, beforeId) {
    clearDropHint();
    if (card) { card.classList.add(beforeId ? 'drop-before' : 'drop-after'); }
    else if (section) { section.classList.add('drop-end'); }
  }

  // 同一组里某张卡的下一张卡（跨过 hidden/折叠时返回 null = 放到组末尾）
  function nextCardId(card) {
    var node = card.nextElementSibling;
    while (node) {
      if (node.classList && node.classList.contains('card') && node.getAttribute('data-id')) {
        return node.getAttribute('data-id');
      }
      node = node.nextElementSibling;
    }
    return null;
  }

  el.content.addEventListener('dragstart', function (event) {
    if (!state.editMode) { return; }
    var card = event.target.closest ? event.target.closest('.card[data-id]') : null;
    if (!card) { return; }              // 拖的是分组标题，交给 makeDraggable
    state.dragCardId = card.getAttribute('data-id');
    state.dragging = true;              // 冻结定时重绘，别把正在拖的节点换掉
    card.classList.add('cardDragging');
    try {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', state.dragCardId);
    } catch (e) {}
  });

  el.content.addEventListener('dragend', function (event) {
    var card = event.target.closest ? event.target.closest('.card[data-id]') : null;
    if (card) { card.classList.remove('cardDragging'); }
    if (state.dragCardId) {             // 没走到 drop（拖到窗口外等）= 取消，不改数据
      state.dragCardId = null;
      state.dropTarget = null;
      state.dragging = false;
      clearDropHint();
    }
  });

  // 分组拖动。只在编辑模式下有 draggable（见 makeDraggable），
  // 这里负责「跟着鼠标实时换位」，松手才落盘，所以预览和最终结果永远一致。
  el.content.addEventListener('dragover', function (event) {
    // modified: 卡片拖动优先——按左右半边判定插到目标卡的前面还是后面
    if (state.dragCardId) {
      var section = event.target.closest ? event.target.closest('section.group') : null;
      if (!section) {
        // modified: 2.6.3 移出有效落点（隐藏栏、新建分组按钮、空白间隙）时
        // 原来直接 return，竖线高亮和过期的 dropTarget 会一直留着。
        clearDropHint();
        state.dropTarget = null;
        return;
      }
      event.preventDefault();
      try { event.dataTransfer.dropEffect = 'move'; } catch (e) {}
      var overCard = event.target.closest('.card[data-id]');
      if (overCard && overCard.getAttribute('data-id') === state.dragCardId) {
        return;                          // 悬停在自己身上，什么都不做
      }
      var beforeId = null;
      if (overCard) {
        var box = overCard.getBoundingClientRect();
        var after = event.clientX > box.left + box.width / 2;
        beforeId = after ? nextCardId(overCard) : overCard.getAttribute('data-id');
      }
      setDropHint(overCard, section, beforeId);
      state.dropTarget = {
        group: section.getAttribute('data-group'),
        beforeId: beforeId
      };
      return;
    }

    if (!state.dragEl) { return; }
    event.preventDefault();
    try { event.dataTransfer.dropEffect = 'move'; } catch (e) {}
    var node = event.target;
    while (node && node !== el.content &&
           !(node.classList && node.classList.contains('group'))) {
      node = node.parentNode;
    }
    if (!node || node === el.content || node === state.dragEl) { return; }
    var rect = node.getBoundingClientRect();
    var after = event.clientY > rect.top + rect.height / 2;
    el.content.insertBefore(state.dragEl, after ? node.nextSibling : node);
  });

  el.content.addEventListener('drop', function (event) {
    if (state.dragCardId) {
      event.preventDefault();
      event.stopPropagation();
      var id = state.dragCardId;
      var target = state.dropTarget;
      state.dragCardId = null;
      state.dropTarget = null;
      state.dragging = false;
      clearDropHint();
      if (target && target.group) { moveCard(id, target.group, target.beforeId || null); }
      return;
    }
    if (state.dragEl) { event.preventDefault(); }   // 不 preventDefault 部分内核不会派发 dragend
  });

  var searchTimer = null;
  el.searchBox.addEventListener('input', function () {
    var value = el.searchBox.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {  // modified: 大列表逐字重绘会卡，防抖 120ms
      state.keyword = value;
      lastPrint = '';
      render();
    }, 120);
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
  el.btnStopAll.addEventListener('click', function () {
    // modified: 2.6.0 与后端新行为对齐——只停本工作台拉起的，外部运行的服务不动
    batched('stop-all', '停止本工作台启动的全部服务（外部运行的不动）');
  });
  el.btnSettings.addEventListener('click', openSettings);
  el.btnLayout.addEventListener('click', function () { setEditMode(!state.editMode); });  // modified
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
    var forcedFx = '';  // modified: 渲染诊断开关
    try {
      var params = new URLSearchParams(location.search);
      forcedTheme = params.get('theme') || '';
      forcedView = params.get('view') || '';
      forcedFx = (params.get('fx') || '').toLowerCase();  // modified
    } catch (e) {}

    // modified: 2.6.3 原来所有首屏偏好共用一个 try：hub.sort 存了个坏 JSON，
    // 主题/视图/密度/自动刷新就一起失效。逐项隔离，并校验排序列名。
    try {
      var savedSort = localStorage.getItem('hub.sort');
      if (savedSort) {
        var parsed = JSON.parse(savedSort);
        var keys = COLUMNS.map(function (c) { return c.key; });
        if (parsed && keys.indexOf(parsed.key) >= 0) {
          state.sort = { key: parsed.key, dir: parsed.dir === 'desc' ? 'desc' : 'asc' };
        }
      }
    } catch (e) {}
    try {
      if (localStorage.getItem('hub.autoRefresh') === '0') { state.autoRefresh = false; }
    } catch (e) {}
    try {
      var cachedTheme = localStorage.getItem('hub.theme');
      if (cachedTheme && !forcedTheme) { applyTheme(cachedTheme, false); }
    } catch (e) {}
    try {
      var cachedView = localStorage.getItem('hub.view');
      if (cachedView && !forcedView) { state.view = cachedView === 'list' ? 'list' : 'card'; }
    } catch (e) {}
    try {
      var cachedDensity = localStorage.getItem('hub.density');
      if (cachedDensity) { applyDensity(cachedDensity); }
    } catch (e) {}

    if (forcedTheme) { applyTheme(forcedTheme, false); }
    if (forcedView) { state.view = forcedView === 'list' ? 'list' : 'card'; }
    setView(state.view, false);
    applyFxMode(forcedFx);  // modified: ?fx= 渲染诊断

    get('/api/settings').then(function (payload) {
      state.browsers = payload.browsers || [];
      state.browserResolved = payload.browserResolved || null;  // modified: 2.6.1
      state.autostart = payload.autostart || {};  // modified: 开机自启状态
      // modified: 2.6.0 同步逻辑抽到 adoptSettings（与「从备份恢复」共用）
      adoptSettings(payload.settings || {}, { keepTheme: !!forcedTheme, keepView: !!forcedView });
      state.settingsReady = true;   // modified: 2.6.3
    }).catch(function () {});

    var darkMq = window.matchMedia('(prefers-color-scheme: dark)');
    var onSchemeChange = function () {
      if (state.theme === 'system') {
        document.documentElement.setAttribute('data-theme', resolveTheme('system'));
      }
    };
    if (darkMq.addEventListener) {
      darkMq.addEventListener('change', onSchemeChange);  // modified: 旧内核没有 addEventListener
    } else if (darkMq.addListener) {
      darkMq.addListener(onSchemeChange);  // modified
    }

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { refresh(true); }
    });

    refresh().then(schedule);
  }

  init();
})();
