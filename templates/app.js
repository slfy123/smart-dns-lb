"use strict";

// 页面状态与请求集中管理，刷新不会覆盖正在编辑的表单。
let state = null;
let token = "";
let refreshing = false;
let activePage = "overview";
let activeSettings = "basic";
let settingsDirty = false;
let settingsVersion = null;
let settingsBase = null;
let requestCount = 0;
let sessionId = 0;
let importedNodes = [];
let importVersion = null;
let editingNode = null;
let nodeVersion = null;
let setup = null;
let wizardStep = 0;
let wizardDelegation = null;
let eventRecords = null;
let confirmation = null;
const activeNodeActions = new Set();

const tokenStorageKey = "smart-dns-lb.admin-token";
const themeStorageKey = "smart-dns-lb.theme";
const el = (id) => document.getElementById(id);
const field = (form, name) => form.elements.namedItem(name);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character],
  );

const modes = {
  WEIGHTED: ["手动权重", "按设定权重分配健康线路，适合固定比例。"],
  LOWEST_RTT: [
    "最低延迟",
    "优先使用服务器观测到的低延迟线路，适合关注响应速度。",
  ],
  FAILOVER: ["主备故障转移", "优先使用主线路，故障后切换备用，适合主备部署。"],
  SMART_PEAK: [
    "分时与权重",
    "按高峰和平峰选择分组，再按权重分配，适合分时套餐。",
  ],
  TRAFFIC_AWARE: [
    "流量与带宽综合",
    "结合流量额度与带宽余量分配，适合多种规格混用。",
  ],
  TRAFFIC_REMAINING: [
    "剩余流量",
    "按剩余 GiB 比例分配，适合尽量均衡月度额度。",
  ],
  SMART_PEAK_REMAINING: [
    "分时与剩余流量",
    "先选时段组和优先级，再按剩余流量分配，适合分时主备。",
  ],
};

const pageInfo = {
  overview: ["总览", "运行总览", "查看服务状态，确认每一次新解析的去向。"],
  nodes: ["节点", "节点管理", "管理入口、检查线路健康，并查看当前流量分配。"],
  settings: [
    "设置",
    "服务设置",
    "配置域名与调度策略，让探测和分配符合实际套餐。",
  ],
  events: ["事件", "事件日志", "查看配置变更、健康检查与故障转移记录。"],
};

const settingGroups = {
  basic: ["基础", "设置独立 DNS 子区与监听地址。第一次使用建议运行配置向导。"],
  strategy: ["调度策略", "选择适合线路套餐的策略。只有相关参数会显示。"],
  health: [
    "健康检查",
    "探测经过实际 VLESS 链路；入口 TCP 可达不等于业务健康。",
  ],
  probe: ["探针", "可选：连接兼容的流量探针，读取周期额度与实时带宽。"],
  advanced: [
    "高级 JSON",
    "批量编辑完整配置。UUID 和令牌留空保留，导入完整备份可整体替换。",
  ],
};

// 描述表单字段，保持校验与渲染使用同一套名称。
const settingsFields = {
  basic: [
    ["zone", "独立区域", "text", "lb.example.com"],
    ["service_name", "服务域名", "text", "node.lb.example.com"],
    ["ns_name", "权威 NS 主机名", "text", "ns1.lb.example.com"],
    ["ns_ipv4", "NS 公网 IPv4", "text", "203.0.113.10"],
    ["dns_host", "DNS 监听地址", "text", "0.0.0.0"],
    ["dns_port", "DNS 端口", "number", "", 1, 65535],
    ["api_host", "面板监听地址", "text", "127.0.0.1"],
    ["api_port", "面板端口", "number", "", 1, 65535],
    ["ttl", "DNS 缓存 TTL（秒）", "number", "", 5, 86400],
  ],
  strategy: [
    ["mode", "调度模式", "select", modes],
    ["peak_start", "高峰开始", "time"],
    ["peak_end", "高峰结束（不含）", "time"],
    ["timezone", "调度时区", "text", "Asia/Shanghai"],
    ["lowest_rtt_hold_seconds", "最低延迟保持（秒）", "number", "", 0, 3600],
    [
      "lowest_rtt_improvement_percent",
      "切换改善比例（%）",
      "number",
      "",
      0,
      100,
    ],
    ["traffic_stop_at_percent", "额度停用阈值（%）", "number", "", 50.1, 100],
    [
      "all_down_policy",
      "全部故障时",
      "select",
      { SERVFAIL: "返回 SERVFAIL（推荐）", LAST_KNOWN: "短期使用最近健康线路" },
    ],
    ["last_known_grace_seconds", "最近健康宽限期（秒）", "number", "", 0, 3600],
  ],
  health: [
    ["mihomo_path", "Mihomo 可执行文件", "text", "/usr/local/bin/mihomo"],
    ["probe_interval_seconds", "探测间隔（秒）", "number", "", 2, 300],
    ["probe_timeout_seconds", "单次超时（秒）", "number", "", 0.3, 60],
    ["max_parallel_probes", "并行探测数", "number", "", 1, 64],
    ["fail_threshold", "连续失败阈值", "number", "", 1, 20],
    ["recover_threshold", "连续成功阈值", "number", "", 1, 20],
    ["recovery_steady_seconds", "恢复观察（秒）", "number", "", 0, 3600],
    ["stale_after_seconds", "探测过期（秒）", "number", "", 5, 3600],
    ["max_rtt_ms", "最大延迟（ms）", "number", "", 2, 60000],
    ["recovery_rtt_percent", "恢复延迟比例（%）", "number", "", 50, 100],
    ["expected_exit_ip", "预期落地出口 IP（可选）", "text", "留空跳过出口比对"],
    ["exit_check_url", "出口检查 HTTPS 地址", "url", "https://api.ipify.org"],
  ],
  probe: [
    [
      "probe_base_url",
      "探针 API 基础地址",
      "url",
      "https://monitor.example.com",
    ],
    ["probe_bearer_token", "探针只读令牌", "password", "留空保留原令牌"],
    ["probe_poll_interval_seconds", "同步间隔（秒）", "number", "", 10, 3600],
    ["probe_max_age_seconds", "指标有效期（秒）", "number", "", 30, 86400],
    ["probe_request_timeout_seconds", "请求超时（秒）", "number", "", 0.3, 60],
    ["clear_probe_token", "清空已保存的探针令牌", "checkbox"],
  ],
};

const nodeFields = [
  ["name", "节点名称", "text", "例如：线路甲"],
  ["server", "入口 IPv4", "text", "203.0.113.10"],
  ["port", "入口端口", "number", "", 1, 65535],
  [
    "category",
    "时段分组",
    "select",
    { NORMAL: "NORMAL · 平峰", PREMIUM: "PREMIUM · 高峰" },
  ],
  ["priority", "优先级（数字越小越优先）", "number", "", 1, 10000],
  ["base_weight", "基础权重（0 为停用）", "number", "", 0, 10000],
  [
    "transport",
    "传输类型",
    "select",
    { tcp: "TCP", ws: "WebSocket", grpc: "gRPC" },
  ],
  ["uuid", "VLESS UUID", "password", "编辑时留空保留"],
  ["tls_sni", "TLS SNI", "text", "origin.example.com"],
  [
    "flow",
    "Flow",
    "select",
    { "": "无", "xtls-rprx-vision": "xtls-rprx-vision" },
  ],
  ["encryption", "VLESS Encryption（编辑时留空保留）", "password", "none"],
  ["alpn", "ALPN（逗号分隔）", "text", "h2,http/1.1"],
  ["ws_path", "WebSocket 路径", "text", "/"],
  ["ws_host", "WebSocket Host", "text", "cdn.example.com"],
  ["grpc_service_name", "gRPC 服务名称", "text", ""],
  ["client_fingerprint", "客户端指纹", "text", "chrome"],
  ["probe_server_id", "探针 Server ID（可选）", "text", "与探针中的 ID 一致"],
  ["probe_server_name", "探针节点名称（可选）", "text", "无 ID 时精确匹配"],
  ["capacity_mbps", "套餐带宽 Mbps（可选）", "number", "", 0.1, 1000000],
  ["max_rtt_ms", "节点延迟阈值 ms（可选）", "number", "", 2, 60000],
  [
    "traffic_limit_gib_override",
    "流量额度 GiB（可选）",
    "number",
    "",
    0,
    100000000,
  ],
  ["reset_day_override", "重置日（可选，1–31）", "number", "", 0, 31],
  [
    "traffic_counter",
    "计费口径",
    "select",
    {
      auto: "沿用探针",
      total: "上下行总和",
      upload: "上传",
      download: "下载",
      max: "取较大值",
    },
  ],
  ["enabled", "启用此节点", "checkbox"],
  ["maintenance", "维护模式（退出调度）", "checkbox"],
  ["skip_cert_verify", "跳过证书校验（请确认链路要求）", "checkbox"],
];

function storageGet(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch (_) {
    return "";
  }
}

function forgetToken() {
  try {
    localStorage.removeItem(tokenStorageKey);
  } catch (_) {
    // 隐私模式可能禁用浏览器存储，页面内存登录仍可使用。
  }
}

function toast(message, error = false) {
  const item = document.createElement("div");
  item.className = "toast" + (error ? " error" : "");
  item.innerHTML = `<span>${esc(message)}</span><button class="toast-close" data-dismiss-toast="1" aria-label="关闭提示">关闭</button>`;
  el("toastRegion").append(item);
  while (el("toastRegion").children.length > 3) {
    el("toastRegion").firstElementChild.remove();
  }
  setTimeout(() => item.remove(), error ? 9000 : 5000);
}

function showLogin() {
  if (!el("loginDialog").open) {
    el("loginDialog").showModal();
  }
}

function closeDialogs() {
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  if (confirmation) {
    confirmation(false);
    confirmation = null;
  }
}

function updateRequestStatus() {
  el("requestStatus").classList.toggle("loading", requestCount > 0);
  el("requestStatus").textContent =
    requestCount > 0
      ? "正在处理请求…"
      : token
        ? "已连接 · 自动刷新"
        : "等待登录";
  el("requestStatus").title = el("requestStatus").textContent;
  el("requestStatus").dataset.connected = token ? "true" : "false";
}

async function api(path, options = {}) {
  const requestSession = sessionId;
  const requestToken = token;
  requestCount += 1;
  updateRequestStatus();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(path, {
      ...options,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + token,
        ...(options.headers || {}),
      },
    });
    const data = await response
      .json()
      .catch(() => ({ detail: "服务器响应格式异常" }));
    if (response.status === 401) {
      logout(false);
    }
    if (!response.ok) {
      const detail =
        typeof data.detail === "string"
          ? data.detail
          : "表单字段格式有误，请检查输入";
      throw new Error(
        response.status === 401 ? "令牌无效或已过期，请重新登录" : detail,
      );
    }
    if (requestSession !== sessionId || requestToken !== token) {
      throw new Error("登录状态已变化，请重新操作");
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("请求超时，请检查服务连接后重试");
    }
    if (error.name === "TypeError") {
      throw new Error("连接服务失败，请检查网络或服务是否运行");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    requestCount -= 1;
    updateRequestStatus();
  }
}

async function action(button, task, errorId = "") {
  if (button?.disabled) {
    return;
  }
  const previous = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "处理中…";
    button.setAttribute("aria-busy", "true");
  }
  if (errorId) {
    el(errorId).textContent = "";
  }
  try {
    await task();
  } catch (error) {
    if (errorId) {
      el(errorId).textContent = error.message;
    }
    toast(error.message, true);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = previous;
      button.removeAttribute("aria-busy");
    }
  }
}

function confirmAction(title, message) {
  el("confirmTitle").textContent = title;
  el("confirmMessage").textContent = message;
  el("confirmDialog").showModal();
  el("confirmCancel").focus();
  return new Promise((resolve) => {
    confirmation = resolve;
  });
}

function finishConfirmation(accepted) {
  el("confirmDialog").close();
  if (confirmation) {
    confirmation(accepted);
    confirmation = null;
  }
}

function applyTheme(theme) {
  const selected = ["light", "dark", "auto"].includes(theme) ? theme : "auto";
  if (selected === "auto") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.dataset.theme = selected;
  }
  el("themeButton").textContent =
    "主题：" + { auto: "自动", light: "浅色", dark: "深色" }[selected];
  el("themeButton").dataset.selection = selected;
  return selected;
}

function cycleTheme() {
  const choices = ["auto", "light", "dark"];
  const selected = applyTheme(
    choices[(choices.indexOf(el("themeButton").dataset.selection) + 1) % 3],
  );
  try {
    localStorage.setItem(themeStorageKey, selected);
  } catch (_) {
    toast("浏览器存储已禁用，主题仅在本次页面生效");
  }
}

async function login(event) {
  event.preventDefault();
  const form = event.target;
  await action(
    form.querySelector('[type="submit"]'),
    async () => {
      token = field(form, "token").value.trim();
      state = await api("/api/status");
      forgetToken();
      if (field(form, "remember_token").checked) {
        try {
          localStorage.setItem(tokenStorageKey, token);
        } catch (_) {
          token = "";
          throw new Error("浏览器存储已禁用，请取消记住令牌后登录");
        }
      }
      sessionId += 1;
      field(form, "token").value = "";
      el("loginDialog").close();
      render();
      await afterLogin();
    },
    "loginError",
  );
}

function logout(notify = true) {
  sessionId += 1;
  token = "";
  state = null;
  setup = null;
  eventRecords = null;
  settingsDirty = false;
  settingsVersion = null;
  settingsBase = null;
  importedNodes = [];
  editingNode = null;
  forgetToken();
  closeDialogs();
  field(el("loginForm"), "remember_token").checked = false;
  el("loginForm").reset();
  el("settingsForm").reset();
  el("nodeForm").reset();
  el("linkText").value = "";
  [
    "nodeGrid",
    "overviewNodes",
    "eventList",
    "recentEvents",
    "settingsFields",
    "nodeFields",
    "linkPreview",
    "wizardContent",
  ].forEach((id) => {
    el(id).replaceChildren();
  });
  showLogin();
  updateRequestStatus();
  if (notify) {
    toast("已退出登录，并清除此浏览器保存的令牌");
  }
}

async function restoreLogin() {
  token = storageGet(tokenStorageKey);
  if (token) {
    try {
      state = await api("/api/status");
      render();
      await afterLogin();
      return;
    } catch (error) {
      el("loginError").textContent = error.message;
    }
  }
  showLogin();
}

async function afterLogin() {
  await loadSetup();
  if (setup && !setup.complete) {
    openWizard();
  }
}

async function refresh(manual = false) {
  if (!token || refreshing) {
    return;
  }
  const currentSession = sessionId;
  refreshing = true;
  try {
    const next = await api("/api/status");
    if (
      currentSession !== sessionId ||
      !token ||
      (state && next.version < state.version)
    ) {
      return;
    }
    state = next;
    render();
    if (manual) {
      toast("运行状态已更新");
    }
  } catch (error) {
    el("lastUpdated").textContent = error.message;
    if (manual) {
      throw error;
    }
  } finally {
    refreshing = false;
  }
}

function navigate(page) {
  if (!pageInfo[page]) {
    return;
  }
  activePage = page;
  const [label, title, subtitle] = pageInfo[page];
  el("breadcrumb").textContent = label;
  el("pageTitle").textContent = title;
  el("pageSubtitle").textContent = subtitle;
  Object.keys(pageInfo).forEach((name) => {
    el("page-" + name).hidden = name !== page;
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === page);
    if (button.dataset.page === page) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  });
  if (page === "events" && token) {
    action(null, loadEvents);
  }
}

function number(value, digits = 0) {
  return Number.isFinite(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits })
    : "—";
}

function dateTime(value) {
  if (!value) {
    return "尚无记录";
  }
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime())
    ? "时间未知"
    : date.toLocaleString("zh-CN", { hour12: false });
}

function statusClass(status) {
  if (status === "UP") {
    return "up";
  }
  if (status === "DEGRADED") {
    return "degraded";
  }
  return status === "DOWN" ? "down" : "unknown";
}

function statusBadge(status) {
  return /* HTML */ `<span class="status ${statusClass(status)}"
    >${esc(status === "DISABLED" ? "已停用" : status || "UNKNOWN")}</span
  >`;
}

function probability(node) {
  if (!node.enabled || node.maintenance) {
    return "0.0%";
  }
  const total = state.nodes.reduce(
    (sum, item) =>
      sum +
      (item.enabled && !item.maintenance
        ? Math.max(0, item.effective_weight || 0)
        : 0),
    0,
  );
  return total > 0
    ? number((Math.max(0, node.effective_weight || 0) / total) * 100, 1) + "%"
    : "—";
}

function emptyState(title, description, actionName = "", buttonLabel = "") {
  return /* HTML */ `<div class="empty-state full">
    <h3>${esc(title)}</h3>
    <p>${esc(description)}</p>
    ${actionName
      ? `<button data-action="${actionName}" class="button primary">${esc(buttonLabel)}</button>`
      : ""}
  </div>`;
}

function render() {
  if (!state) {
    return;
  }
  renderOverview();
  renderNodes();
  renderEvents(el("recentEvents"), (state.events || []).slice(0, 5));
  if (activePage === "events" && !eventRecords) {
    renderEvents(el("eventList"), state.events || []);
  }
  if (!settingsDirty && settingsVersion !== state.version) {
    renderSettings();
  }
  el("lastUpdated").textContent =
    "上次同步 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function renderOverview() {
  const settings = state.settings;
  const healthy = state.nodes.filter(
    (node) => node.enabled && !node.maintenance && node.status === "UP",
  ).length;
  const degraded = state.nodes.filter(
    (node) => node.enabled && !node.maintenance && node.status === "DEGRADED",
  ).length;
  const fresh = state.nodes.filter(
    (node) => node.traffic_fresh && Number.isFinite(node.traffic_remaining_gib),
  );
  const remaining = fresh.reduce(
    (sum, node) => sum + Math.max(0, node.traffic_remaining_gib),
    0,
  );
  el("dnsStatus").innerHTML =
    statusBadge(state.dns.running ? "UP" : "DOWN") +
    (state.dns.running ? " 运行中" : " 未运行");
  el("dnsMeta").textContent =
    `${settings.dns_host}:${settings.dns_port} · 查询 ${number(state.dns.total_queries)}`;
  el("modeName").textContent = modes[settings.mode]?.[0] || settings.mode;
  el("modeSummary").textContent = modes[settings.mode]?.[1] || "请检查调度配置";
  el("healthCount").textContent = `${healthy} / ${state.nodes.length}`;
  el("healthMeta").textContent =
    `${degraded} 个降级 · ${state.nodes.length - healthy - degraded} 个未就绪或停用`;
  el("trafficRemaining").textContent = fresh.length
    ? number(remaining, 1) + " GiB"
    : "—";
  el("trafficMeta").textContent = fresh.length
    ? `汇总 ${fresh.length} 个节点的有效周期额度`
    : "连接探针并映射节点后显示";
  const entries = [
    ["服务域名", settings.service_name],
    ["当前时段", (state.is_peak ? "高峰" : "平峰") + " · " + settings.timezone],
    ["Mihomo", state.probe.executor_status || "等待探测"],
    ["流量探针", state.probe.source_status || "未配置"],
    ["配置版本", String(state.version)],
  ];
  el("runtimeInfo").innerHTML = entries
    .map(
      ([label, value]) =>
        `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`,
    )
    .join("");
  el("overviewNodes").innerHTML = state.nodes.length
    ? state.nodes
        .slice(0, 5)
        .map(
          (node) => `
    <div class="compact-node">
      <div><strong>${esc(node.name)}</strong><small>${esc(node.server)}</small></div>
      <div>${statusBadge(!node.enabled || node.maintenance ? "DISABLED" : node.status)}</div>
      <span class="numeric">${number(node.ewma_rtt_ms)} ms</span>
      <span class="numeric probability">${probability(node)}</span>
    </div>`,
        )
        .join("")
    : emptyState(
        "还没有线路节点",
        "粘贴 VLESS 链接即可添加第一条线路，或先跟随向导配置域名。",
        "import-links",
        "导入第一条线路",
      );
}

function renderNodes() {
  const search = el("nodeSearch").value.trim().toLowerCase();
  const status = el("nodeFilter").value;
  const nodes = state.nodes.filter((node) => {
    const matchesSearch = (node.name + " " + node.server)
      .toLowerCase()
      .includes(search);
    const matchesStatus =
      !status ||
      (status === "UNKNOWN"
        ? !["UP", "DOWN", "DEGRADED"].includes(node.status)
        : node.status === status);
    return matchesSearch && matchesStatus;
  });
  el("nodeCount").textContent =
    `共 ${state.nodes.length} 个节点，当前显示 ${nodes.length} 个`;
  if (!nodes.length) {
    el("nodeGrid").innerHTML = state.nodes.length
      ? emptyState("没有匹配的节点", "尝试其他名称、IP 或健康状态。")
      : emptyState(
          "添加你的第一条线路",
          "从 3x-ui 或 v2rayN 复制 VLESS 链接，可一次粘贴多条。",
          "import-links",
          "粘贴 VLESS 链接",
        );
    return;
  }
  el("nodeGrid").innerHTML = nodes
    .map((node) => {
      const used =
        node.traffic_limit_gib > 0
          ? Math.max(
              0,
              Math.min(
                100,
                ((node.traffic_used_gib || 0) / node.traffic_limit_gib) * 100,
              ),
            )
          : 0;
      const quota = node.traffic_fresh
        ? `${number(node.traffic_remaining_gib, 1)} / ${number(node.traffic_limit_gib, 1)} GiB 剩余`
        : "流量额度待同步";
      return /* HTML */ `<article class="node-card">
        <div class="section-heading">
          <div>
            <h3>${esc(node.name)}</h3>
            <span class="node-address"
              >${esc(node.server)}:${node.port} ·
              ${esc(node.transport.toUpperCase())}</span
            >
          </div>
          ${statusBadge(
            !node.enabled || node.maintenance ? "DISABLED" : node.status,
          )}
        </div>
        <div class="node-stats">
          <div>
            <small>平滑延迟</small
            ><strong
              >${number(node.ewma_rtt_ms)}
              <small style="display:inline">ms</small></strong
            >
          </div>
          <div>
            <small>当前分配概率</small><strong>${probability(node)}</strong>
          </div>
        </div>
        <div class="traffic-label">
          <span>${esc(quota)}</span
          ><span>${node.traffic_fresh ? number(used, 1) + "% 已用" : "—"}</span>
        </div>
        <progress
          max="100"
          value="${used}"
          aria-label="${esc(node.name)} 已用流量比例"
        ></progress>
        <div class="traffic-label">
          <span>${esc(node.category)} · 优先级 ${node.priority}</span
          ><span
            >${node.maintenance
              ? "维护中"
              : node.enabled
                ? "已启用"
                : "已停用"}</span
          >
        </div>
        <p class="node-error">
          ${!node.enabled || node.maintenance
            ? `最近健康：${esc(node.health_status || node.status)} · `
            : ""}
          ${esc(
            node.last_error ||
              node.traffic_error ||
              (node.has_uuid
                ? "等待或持续进行端到端健康检查"
                : "请编辑并填写 VLESS UUID"),
          )}
        </p>
        <div class="button-row">
          <button
            data-action="probe-node"
            data-id="${esc(node.id)}"
            class="button small"
          >
            探测
          </button>
          <button
            data-action="edit-node"
            data-id="${esc(node.id)}"
            class="button small"
          >
            编辑
          </button>
          <button
            data-action="toggle-node"
            data-id="${esc(node.id)}"
            class="button small"
          >
            ${node.enabled ? "停用" : "启用"}
          </button>
          <button
            data-action="delete-node"
            data-id="${esc(node.id)}"
            class="button small quiet"
          >
            删除
          </button>
        </div>
      </article>`;
    })
    .join("");
  el("nodeGrid")
    .querySelectorAll("button[data-id]")
    .forEach((button) => {
      if (activeNodeActions.has(button.dataset.id)) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      }
    });
}

function renderEvents(container, records) {
  container.innerHTML = records.length
    ? records
        .map(
          (item) =>
            `<div class="event-row"><time>${esc(dateTime(item.created_at))}</time><p>${esc(item.message)}</p></div>`,
        )
        .join("")
    : emptyState(
        "暂时没有事件",
        "服务启动、配置修改和线路状态变化会记录在这里。",
      );
}

async function loadEvents() {
  const result = await api("/api/events");
  eventRecords = result.events || [];
  renderEvents(el("eventList"), eventRecords);
}

function control(definition, value, prefix = "") {
  const [name, label, type, options, minimum, maximum] = definition;
  const id = prefix + name;
  if (type === "checkbox") {
    return /* HTML */ `<label class="checkbox" data-field="${name}"
      ><input
        id="${id}"
        name="${name}"
        type="checkbox"
        ${value ? "checked" : ""}
      />${esc(label)}</label
    >`;
  }
  let input;
  if (type === "select") {
    input = `<select id="${id}" name="${name}">${Object.entries(options)
      .map(
        ([key, title]) =>
          `<option value="${esc(key)}" ${key === String(value) ? "selected" : ""}>${esc(Array.isArray(title) ? title[0] : title)}</option>`,
      )
      .join("")}</select>`;
  } else {
    input = `<input id="${id}" name="${name}" type="${type}" value="${esc(value ?? "")}" placeholder="${esc(options || "")}" ${type === "number" ? `step="any" ${minimum !== undefined ? `min="${minimum}"` : ""} ${maximum !== undefined ? `max="${maximum}"` : ""}` : ""}>`;
  }
  return /* HTML */ `<label data-field="${name}">${esc(label)}${input}</label>`;
}

function probeValue(name, settings) {
  if (
    !name.startsWith("probe_") ||
    !settingsFields.probe.some((item) => item[0] === name)
  ) {
    return settings[name];
  }
  return name === "probe_bearer_token"
    ? ""
    : settings.probe_source[name.slice(6)];
}

function editableNodes() {
  return state.nodes.map((node) =>
    Object.fromEntries([
      ["id", node.id],
      ...nodeFields
        .filter((item) => item[0] !== "uuid")
        .map((item) => [item[0], node[item[0]]]),
    ]),
  );
}

function renderSettings() {
  settingsVersion = state.version;
  settingsBase = structuredClone(state.settings);
  el("settingsTabs").innerHTML = Object.entries(settingGroups)
    .map(
      ([name, [title]]) =>
        `<button data-setting-tab="${name}" class="settings-tab ${name === activeSettings ? "active" : ""}">${title}</button>`,
    )
    .join("");
  el("settingsFields").innerHTML = Object.entries(settingGroups)
    .map(([name, [title, description]]) => {
      const intro = `<div class="form-intro"><h2>${title}</h2><p class="muted">${description}</p></div>`;
      let content;
      if (name === "advanced") {
        content = /* HTML */ `<label
          >完整配置 JSON<textarea
            name="advanced_json"
            class="json-editor"
            spellcheck="false"
          ></textarea>
        </label>`;
      } else {
        content = /* HTML */ `<div class="form-grid">
          ${settingsFields[name]
            .map((definition) =>
              control(
                definition,
                probeValue(definition[0], settingsBase),
                "setting-",
              ),
            )
            .join("")}
        </div>`;
      }
      if (name === "basic") {
        content +=
          '<button type="button" data-action="setup" class="button" style="margin-top:20px">重新运行配置向导</button>';
      }
      if (name === "strategy") {
        content +=
          '<p id="strategyHelp" class="callout"></p><p class="note">高峰开始与结束相同表示全天高峰；00:00 表示午夜。结束时间在高级 JSON 中也可填 24:00。</p>';
      }
      if (name === "probe") {
        content +=
          '<p class="note">保存设置后可测试连接；节点中填写 Server ID 或精确名称进行映射。</p><div class="button-row"><button type="button" data-action="test-probe" class="button">测试已保存的连接</button><button type="button" data-action="sync-probe" class="button">立即同步</button></div><div id="probeTestResult" class="note"></div>';
      }
      return /* HTML */ `<section
        data-setting-panel="${name}"
        ${name !== activeSettings ? "hidden" : ""}
      >
        ${intro}${content}
      </section>`;
    })
    .join("");
  field(el("settingsForm"), "advanced_json").value = JSON.stringify(
    {
      version: settingsVersion,
      settings: settingsBase,
      nodes: editableNodes(),
    },
    null,
    2,
  );
  el("settingsDirty").textContent = "配置已同步 · 版本 " + settingsVersion;
  updateStrategyFields();
}

function selectSettings(name) {
  activeSettings = name;
  document.querySelectorAll("[data-setting-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.settingPanel !== name;
  });
  document.querySelectorAll("[data-setting-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.settingTab === name);
  });
}

function updateStrategyFields() {
  const form = el("settingsForm");
  const mode = field(form, "mode")?.value;
  if (!mode) {
    return;
  }
  const visibility = {
    peak_start: mode.startsWith("SMART_PEAK"),
    peak_end: mode.startsWith("SMART_PEAK"),
    timezone: mode.startsWith("SMART_PEAK"),
    lowest_rtt_hold_seconds: mode === "LOWEST_RTT",
    lowest_rtt_improvement_percent: mode === "LOWEST_RTT",
    traffic_stop_at_percent: [
      "TRAFFIC_AWARE",
      "TRAFFIC_REMAINING",
      "SMART_PEAK_REMAINING",
    ].includes(mode),
    last_known_grace_seconds:
      field(form, "all_down_policy").value === "LAST_KNOWN",
  };
  Object.entries(visibility).forEach(([name, visible]) => {
    field(form, name).closest("[data-field]").hidden = !visible;
  });
  el("strategyHelp").textContent = modes[mode][1];
}

function validateForm(form, requiredNames = []) {
  for (const input of form.elements) {
    if (!input.name || input.disabled || input.type === "submit") {
      continue;
    }
    if (requiredNames.includes(input.name) && !input.value.trim()) {
      input.setAttribute("aria-invalid", "true");
      input.focus();
      throw new Error(
        (input.closest("label")?.firstChild.textContent || input.name).trim() +
          " 请填写完整",
      );
    }
    if (!input.checkValidity()) {
      input.setAttribute("aria-invalid", "true");
      input.focus();
      throw new Error(
        "请检查字段格式或数值范围：" +
          (input.closest("label")?.firstChild.textContent || input.name).trim(),
      );
    }
    input.removeAttribute("aria-invalid");
  }
}

function valueOf(form, definition) {
  const input = field(form, definition[0]);
  return input.type === "checkbox"
    ? input.checked
    : input.type === "number"
      ? input.value === ""
        ? null
        : Number(input.value)
      : input.value.trim();
}

async function saveSettings(event) {
  event.preventDefault();
  await action(
    event.submitter,
    async () => {
      let payload;
      if (activeSettings === "advanced") {
        try {
          payload = JSON.parse(field(event.target, "advanced_json").value);
        } catch (_) {
          throw new Error("JSON 格式有误，请检查括号、逗号与引号");
        }
        if (
          !(await confirmAction(
            "保存高级配置？",
            "将按 JSON 更新服务配置和节点列表。请确认监听地址、域名和节点参数。",
          ))
        ) {
          return;
        }
        payload.version = settingsVersion;
      } else {
        validateForm(event.target);
        const settings = structuredClone(settingsBase);
        let clearToken = false;
        Object.values(settingsFields)
          .flat()
          .forEach((definition) => {
            const name = definition[0];
            const value = valueOf(event.target, definition);
            if (name === "clear_probe_token") {
              clearToken = value;
            } else if (settingsFields.probe.some((item) => item[0] === name)) {
              settings.probe_source[name.slice(6)] = value;
            } else {
              settings[name] = value;
            }
          });
        settings.soa_mname = settings.ns_name;
        if (
          !(await confirmAction(
            "保存服务设置？",
            "配置会重新加载。域名、监听或调度策略变更会影响后续解析；清空令牌会中断探针认证。",
          ))
        ) {
          return;
        }
        payload = {
          settings,
          version: settingsVersion,
          clear_probe_token: clearToken,
        };
      }
      const next = await api("/api/config", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      settingsDirty = false;
      state = next;
      render();
      await loadSetup();
      toast(
        next.restart_required
          ? "设置已保存；监听参数变更需重启服务生效"
          : "设置已保存并重新加载",
      );
    },
    "settingsError",
  );
}

// 链接先预览，再一次提交，避免批量导入到一半失败。
function openNode(id = "", manual = false) {
  if (!state) {
    return;
  }
  editingNode = state.nodes.find((node) => node.id === id) || null;
  nodeVersion = state.version;
  importedNodes = [];
  el("linkText").value = "";
  el("linkPreview").replaceChildren();
  el("linkError").textContent = "";
  el("nodeError").textContent = "";
  el("saveImported").hidden = true;
  el("linkImport").hidden = Boolean(editingNode);
  el("nodeTitle").textContent = editingNode
    ? "编辑节点 · " + editingNode.name
    : "添加节点";
  const defaults = editingNode || {
    port: 443,
    category: "NORMAL",
    priority: 1,
    base_weight: 100,
    transport: "tcp",
    encryption: "none",
    ws_path: "/",
    client_fingerprint: "chrome",
    enabled: true,
    traffic_counter: "auto",
  };
  el("nodeFields").innerHTML = nodeFields
    .map((definition) => {
      let value = defaults[definition[0]];
      if (definition[0] === "uuid") {
        value = "";
      } else if (definition[0] === "alpn") {
        value = (value || []).join(",");
      }
      return control(definition, value, "node-");
    })
    .join("");
  el("manualDetails").open = Boolean(editingNode) || manual;
  el("nodeDialog").showModal();
}

async function parseLinks() {
  const text = el("linkText").value.trim();
  importedNodes = [];
  el("saveImported").hidden = true;
  el("linkPreview").replaceChildren();
  if (!text) {
    throw new Error("请先粘贴至少一条 VLESS 分享链接");
  }
  importVersion = state.version;
  const result = await api("/api/nodes/parse-link", {
    method: "POST",
    body: JSON.stringify({ links: text }),
  });
  importedNodes = result.nodes;
  el("linkPreview").innerHTML =
    `<p class="note">已解析 ${result.nodes.length} 个节点，请确认入口后导入。</p>` +
    result.nodes
      .map(
        (node) =>
          `<div class="preview-item"><strong>${esc(node.name)}</strong><small>${esc(node.server)}:${node.port} · ${esc(node.transport.toUpperCase())} · SNI ${esc(node.tls_sni || "内核默认")}</small></div>`,
      )
      .join("") +
    (result.warnings || [])
      .map((warning) => `<p class="callout">${esc(warning)}</p>`)
      .join("");
  el("saveImported").hidden = false;
  el("saveImported").textContent = `确认导入 ${result.nodes.length} 个节点`;
}

async function saveImported() {
  if (!importedNodes.length) {
    throw new Error("请先解析链接");
  }
  if (
    !(await confirmAction(
      "导入这些节点？",
      `将新增 ${importedNodes.length} 个节点并启用端到端探测。现有节点保留。`,
    ))
  ) {
    return;
  }
  state = await api("/api/config", {
    method: "PUT",
    body: JSON.stringify({
      version: importVersion,
      nodes: [...editableNodes(), ...importedNodes],
    }),
  });
  importedNodes = [];
  el("linkText").value = "";
  el("nodeDialog").close();
  render();
  await loadSetup();
  if (el("wizardDialog").open) {
    renderWizard();
  }
  toast("节点已导入，正在等待健康探测");
}

async function saveNode(event) {
  event.preventDefault();
  await action(
    event.submitter,
    async () => {
      validateForm(event.target, [
        "name",
        "server",
        "port",
        "client_fingerprint",
      ]);
      const node = Object.fromEntries(
        nodeFields.map((definition) => [
          definition[0],
          valueOf(event.target, definition),
        ]),
      );
      node.alpn = node.alpn
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean);
      if (
        !(await confirmAction(
          editingNode ? "保存节点修改？" : "添加节点？",
          editingNode
            ? "连接参数变更会重新检查线路健康，停用或维护会使节点退出调度。"
            : "节点保存后将开始端到端健康探测。",
        ))
      ) {
        return;
      }
      const path =
        "/api/nodes" +
        (editingNode ? "/" + encodeURIComponent(editingNode.id) : "");
      state = await api(path, {
        method: editingNode ? "PUT" : "POST",
        headers: { "If-Match": String(nodeVersion) },
        body: JSON.stringify(node),
      });
      el("nodeDialog").close();
      render();
      await loadSetup();
      toast("节点已保存");
    },
    "nodeError",
  );
}

async function nodeAction(name, id) {
  const node = state.nodes.find((item) => item.id === id);
  if (!node) {
    throw new Error("节点已变化，请刷新后重试");
  }
  const path = "/api/nodes/" + encodeURIComponent(id);
  const version = state.version;
  if (name === "probe-node") {
    const result = await api(path + "/probe", { method: "POST" });
    toast(
      result.success === true
        ? `探测成功 · ${number(result.rtt_ms)} ms`
        : "探测未通过：" + (result.error || "请检查链路参数"),
      result.success !== true,
    );
    await refresh();
    return;
  }
  const deleting = name === "delete-node";
  const title = deleting
    ? "删除节点？"
    : node.enabled
      ? "停用节点？"
      : "启用节点？";
  const message = deleting
    ? `删除「${node.name}」后将停止探测并退出后续 DNS 调度。该操作需要重新导入才能恢复。`
    : `确认${node.enabled ? "停用" : "启用"}「${node.name}」？这会影响后续 DNS 调度。`;
  if (!(await confirmAction(title, message))) {
    return;
  }
  state = await api(path, {
    method: deleting ? "DELETE" : "PUT",
    headers: { "If-Match": String(version) },
    ...(deleting ? {} : { body: JSON.stringify({ enabled: !node.enabled }) }),
  });
  render();
  await loadSetup();
  toast(deleting ? "节点已删除" : "节点状态已更新");
}

async function exportConfig() {
  const secrets = el("exportSecrets").checked;
  if (
    secrets &&
    !(await confirmAction(
      "导出包含密钥的备份？",
      "文件包含节点 UUID 和探针令牌。请保存到可信位置，避免公开分享。",
    ))
  ) {
    return;
  }
  const payload = await api(
    "/api/config/export" + (secrets ? "?include_secrets=1" : ""),
  );
  const blob = new Blob([JSON.stringify(payload, null, 2) + "\n"], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `smart-dns-${secrets ? "full" : "redacted"}-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("配置备份已导出");
}

async function importConfig(file) {
  if (!file) {
    return;
  }
  if (file.size > 2 * 1024 * 1024) {
    throw new Error("备份文件请控制在 2 MiB 以内");
  }
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch (_) {
    throw new Error("备份不是有效的 JSON 文件");
  }
  if (
    !payload ||
    typeof payload.settings !== "object" ||
    !payload.settings ||
    !Array.isArray(payload.nodes)
  ) {
    throw new Error("备份应包含 settings 对象和 nodes 数组");
  }
  const version = state.version;
  if (
    !(await confirmAction(
      "整体替换当前配置？",
      `此备份包含 ${payload.nodes.length} 个节点。现有设置与节点列表将整体替换。建议先导出当前备份。`,
    ))
  ) {
    return;
  }
  const next = await api("/api/config/import", {
    method: "POST",
    headers: { "If-Match": String(version) },
    body: JSON.stringify(payload),
  });
  state = next;
  settingsDirty = false;
  render();
  await loadSetup();
  toast(
    next.restart_required
      ? "备份已导入；监听变更需要重启服务"
      : "备份已导入并重新加载",
  );
}

async function loadSetup(force = false) {
  setup = await api("/api/setup/status" + (force ? "?refresh_ip=1" : ""));
  el("setupBanner").hidden = setup.complete;
  const labels = {
    zone: "区域",
    service_name: "服务域名",
    ns_ipv4: "公网 IP",
    nodes: "节点",
    mihomo: "Mihomo",
  };
  el("setupSummary").textContent = setup.complete
    ? "基础配置已完成"
    : "待完成：" +
      setup.missing.map((name) => labels[name] || name).join("、") +
      "。向导会一步步说明。";
  return setup;
}

function openWizard() {
  if (!state || !setup) {
    return;
  }
  wizardStep = 0;
  wizardDelegation = null;
  renderWizard();
  if (!el("wizardDialog").open) {
    el("wizardDialog").showModal();
  }
}

function delegationRecords() {
  return setup?.records || [];
}

function recordsTable() {
  return /* HTML */ `<div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>记录类型</th>
          <th>名称</th>
          <th>目标 / 内容</th>
        </tr>
      </thead>
      <tbody>
        ${delegationRecords()
          .map(
            (record) =>
              `<tr><td>${esc(record.type)}</td><td><code>${esc(record.name)}</code></td><td><code>${esc(record.value)}</code></td></tr>`,
          )
          .join("")}
      </tbody>
    </table>
  </div>`;
}

function renderWizard() {
  const titles = [
    "服务域名",
    "公网 IP",
    "导入节点",
    "Mihomo",
    "流量探针",
    "DNS 委派",
  ];
  el("wizardSteps").innerHTML = titles
    .map(
      (title, index) =>
        `<li class="${index === wizardStep ? "active" : ""}" ${index === wizardStep ? 'aria-current="step"' : ""}><span class="step-number">${index + 1}</span><span class="step-title">${title}</span></li>`,
    )
    .join("");
  el("wizardError").textContent = "";
  el("wizardBack").disabled = wizardStep === 0;
  el("wizardNext").textContent =
    wizardStep === 5
      ? "完成向导"
      : wizardStep === 4
        ? "保存或跳过"
        : "保存并继续";
  const settings = state.settings;
  let content = "";
  if (wizardStep === 0) {
    const value = setup.checks.zone.ok
      ? settings.service_name.replace(/\.$/, "")
      : "";
    content = /* HTML */ `<h3>选择一个专用服务域名</h3>
      <p>
        假设你拥有 example.com，使用
        <strong>node.lb.example.com</strong> 作为客户端统一入口。我们只接管 lb
        这个独立子区，主域和网站继续由原 DNS 服务商管理。
      </p>
      <label
        >服务域名<input
          name="service_domain"
          value="${esc(value)}"
          placeholder="node.lb.example.com"
          autocomplete="off"
      /></label>
      <div class="callout">
        格式：服务名.独立子区.你的主域名。请使用自己的域名，避免直接填写
        dns.example.com。
      </div>`;
  } else if (wizardStep === 1) {
    const detected = setup.public_ip;
    const value = setup.checks.ns_ipv4.ok
      ? settings.ns_ipv4
      : detected.ip || "";
    content = /* HTML */ `<h3>确认运行本程序的公网 IPv4</h3>
      <p>
        这应是安装 Smart DNS LB 的服务器地址。DNS 查询会到达这台服务器的 UDP/TCP
        53 端口。探测结果只是建议，请核对云服务器控制台。
      </p>
      <label
        >NS 公网 IPv4<input
          name="ns_ipv4"
          value="${esc(value)}"
          placeholder="203.0.113.10"
          inputmode="decimal"
      /></label>
      <p class="note">
        ${esc(
          detected.message ||
            (detected.detected
              ? "已自动探测，请确认地址正确"
              : "自动探测未成功，请手动填写"),
        )}
      </p>
      <button type="button" data-action="refresh-ip" class="button">
        重新探测公网 IP
      </button>`;
  } else if (wizardStep === 2) {
    content = /* HTML */ `<h3>添加至少一条 VLESS 线路</h3>
      <p>
        从 3x-ui 或 v2rayN
        复制分享链接，然后粘贴到导入窗口。多条链接各占一行。所有入口应接收相同的客户端协议、端口和连接参数。
      </p>
      <div class="callout">
        当前已有 ${state.nodes.length} 个节点。导入后会自动启用健康探测。
      </div>
      <div class="button-row">
        <button type="button" data-action="import-links" class="button primary">
          粘贴 VLESS 链接</button
        ><button type="button" data-action="manual-node" class="button">
          手动添加
        </button>
      </div>`;
  } else if (wizardStep === 3) {
    content = /* HTML */ `<h3>检查 Mihomo 探测内核</h3>
      <p>
        Mihomo 用于验证实际 VLESS 链路。安装脚本和默认 Docker
        镜像已内置；已有内核可直接填写绝对路径。
      </p>
      <div class="callout">
        ${esc(setup.mihomo.message)}${setup.mihomo.path
          ? "<br>" + esc(setup.mihomo.path)
          : ""}
      </div>
      <label
        >Mihomo 路径<input
          name="mihomo_path"
          value="${esc(setup.mihomo.path || settings.mihomo_path || "")}"
          placeholder="/usr/local/bin/mihomo"
      /></label>
      <p class="note">保存时会使用此内核校验已配置节点。</p>`;
  } else if (wizardStep === 4) {
    content = /* HTML */ `<h3>连接流量探针（可跳过）</h3>
      <p>
        已有兼容探针时填写基础地址与只读令牌。没有探针也可使用手动权重、最低延迟或主备策略；稍后在节点编辑中映射
        Server ID。
      </p>
      <div class="form-grid">
        <label
          >探针基础地址<input
            name="probe_base_url"
            type="url"
            value="${esc(settings.probe_source.base_url)}"
            placeholder="https://monitor.example.com" /></label
        ><label
          >只读令牌（可选）<input
            name="probe_bearer_token"
            type="password"
            autocomplete="off"
            placeholder="留空保留已保存令牌"
        /></label>
      </div>
      <p class="note">留空地址并继续，将跳过这一步，不清空已有设置。</p>`;
  } else {
    content = /* HTML */ `<h3>在原 DNS 服务商处添加两条记录</h3>
      <p>
        打开你主域名的 DNS 控制台。把下面的 NS
        记录加到父区，将独立子区交给本程序；A/glue
        记录告诉解析器去哪里寻找这台权威服务器。
      </p>
      ${recordsTable()}
      <div class="button-row">
        <button type="button" data-action="copy-records" class="button">
          一键复制记录</button
        ><button
          type="button"
          data-action="check-delegation"
          class="button primary"
        >
          检查委派
        </button>
      </div>
      <div class="callout">
        Cloudflare：A 记录必须关闭代理，使用<strong>灰云 / 仅 DNS</strong
        >。如果控制台要求 glue / 主机记录，请在域名注册商处为 NS 主机名登记上表
        IPv4。放通服务器和云安全组的 UDP 与 TCP 53。
      </div>
      <p class="note">
        部分控制台会自动补全主域名；名称可按其要求填写相对子域。NS
        记录的值是主机名，A 记录的值是 IPv4。记录保存后，缓存生效可能需要等待
        TTL。
      </p>
      <div id="delegationResult">
        ${wizardDelegation
          ? diagnosticHTML(wizardDelegation)
          : '<p class="note">添加记录后点击“检查委派”，查看父区与公共递归是否确认。</p>'}
      </div>`;
  }
  el("wizardContent").innerHTML = content;
}

function diagnosticHTML(result) {
  const rows = [
    ...(result.parent || []).map((item) => [
      "父区 " + (item.hostname || item.server),
      item.delegated && item.glue_ok
        ? "NS / glue 已匹配"
        : item.error || "NS 或 glue 尚未匹配",
    ]),
    ...(result.recursive || []).map((item) => [
      "公共递归 " + item.server,
      item.error ||
        (item.delegated ? "委派已确认" : "委派未确认") +
          " · A " +
          (item.rcode || "未知"),
    ]),
  ];
  return /* HTML */ `<div class="callout">
    <strong>${esc(result.message || "诊断已完成")}</strong>${rows
      .map(([name, value]) => `<div>${esc(name)}：${esc(value)}</div>`)
      .join("")}${(result.suggestions || [])
      .map((message) => `<p>${esc(message)}</p>`)
      .join("")}
  </div>`;
}

async function wizardNext(event) {
  event.preventDefault();
  const completedStep = wizardStep;
  await action(
    event.submitter,
    async () => {
      const form = event.target;
      let settings = null;
      if (completedStep === 0) {
        validateForm(form, ["service_domain"]);
        const domain = field(form, "service_domain").value.trim();
        if (
          setup.checks.zone.ok &&
          domain.replace(/\.$/, "") !==
            state.settings.service_name.replace(/\.$/, "")
        ) {
          if (
            !(await confirmAction(
              "更新服务域名？",
              "这会同时推导并更新独立区域、NS 和 SOA。原委派记录需要相应调整。",
            ))
          ) {
            return;
          }
        }
        settings = { service_domain: domain, ns_ipv4: state.settings.ns_ipv4 };
      } else if (completedStep === 1) {
        validateForm(form, ["ns_ipv4"]);
        settings = { ns_ipv4: field(form, "ns_ipv4").value.trim() };
      } else if (completedStep === 2) {
        if (!state.nodes.length) {
          throw new Error("请先导入或手动添加至少一个节点");
        }
      } else if (completedStep === 3) {
        validateForm(form, ["mihomo_path"]);
        settings = { mihomo_path: field(form, "mihomo_path").value.trim() };
      } else if (completedStep === 4) {
        validateForm(form);
        const url = field(form, "probe_base_url").value.trim();
        if (url) {
          settings = {
            probe_source: {
              base_url: url,
              bearer_token: field(form, "probe_bearer_token").value.trim(),
            },
          };
        }
      } else {
        if (
          !wizardDelegation?.effective &&
          !(await confirmAction(
            "稍后继续检查委派？",
            "委派尚未确认生效。可以先结束向导，保存的配置会保留；稍后从设置页重新检查。",
          ))
        ) {
          return;
        }
        el("wizardDialog").close();
        toast("基础配置已保存，请继续观察节点健康与 DNS 委派");
        return;
      }
      if (settings) {
        state = await api("/api/config", {
          method: "PUT",
          body: JSON.stringify({ version: state.version, settings }),
        });
        settingsDirty = false;
        render();
      }
      await loadSetup();
      wizardStep = Math.min(5, completedStep + 1);
      renderWizard();
    },
    "wizardError",
  );
  // action 会恢复旧按钮文字；向导成功换页后再次同步步骤名称。
  if (wizardStep !== completedStep) {
    el("wizardNext").textContent =
      wizardStep === 5
        ? "完成向导"
        : wizardStep === 4
          ? "保存或跳过"
          : "保存并继续";
  }
}

async function copyRecords() {
  const text = delegationRecords()
    .map((record) => `${record.name}\t${record.type}\t${record.value}`)
    .join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast("NS 与 A/glue 记录已复制");
  } catch (_) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    el("wizardContent").append(textarea);
    textarea.focus();
    textarea.select();
    toast("浏览器未开放剪贴板，请复制已选中的记录文本");
  }
}

async function dispatchAction(name, button) {
  if (name === "theme") {
    cycleTheme();
    return;
  }
  if (name === "logout") {
    logout();
    return;
  }
  if (!state) {
    showLogin();
    return;
  }
  const tasks = {
    refresh: () => refresh(true),
    "events-refresh": loadEvents,
    "import-links": () => openNode(),
    "manual-node": () => openNode("", true),
    "edit-node": () => openNode(button.dataset.id),
    "parse-links": parseLinks,
    "save-imported": saveImported,
    "probe-node": () => nodeAction(name, button.dataset.id),
    "toggle-node": () => nodeAction(name, button.dataset.id),
    "delete-node": () => nodeAction(name, button.dataset.id),
    export: exportConfig,
    "import-config": () => el("configFile").click(),
    setup: async () => {
      if (
        settingsDirty &&
        !(await confirmAction(
          "打开配置向导？",
          "向导保存设置后会覆盖表单中尚未保存的修改。",
        ))
      ) {
        return;
      }
      await loadSetup();
      openWizard();
    },
    "wizard-close": () => el("wizardDialog").close(),
    "wizard-back": () => {
      wizardStep = Math.max(0, wizardStep - 1);
      renderWizard();
    },
    "refresh-ip": async () => {
      await loadSetup(true);
      renderWizard();
    },
    "copy-records": copyRecords,
    "check-delegation": async () => {
      wizardDelegation = await api("/api/setup/check-delegation", {
        method: "POST",
        body: "{}",
      });
      el("delegationResult").innerHTML = diagnosticHTML(wizardDelegation);
    },
    "test-probe": async () => {
      const result = await api("/api/probe-source/test", { method: "POST" });
      el("probeTestResult").textContent =
        `连接成功，发现 ${result.count} 个服务器：` +
        result.servers
          .map((server) => `${server.name} [${server.id}]`)
          .join("、");
      toast("探针连接成功");
    },
    "sync-probe": async () => {
      state = await api("/api/probe-source/sync", { method: "POST" });
      render();
      toast("流量指标已同步");
    },
  };
  const errorId = ["parse-links", "save-imported"].includes(name)
    ? "linkError"
    : ["refresh-ip", "check-delegation"].includes(name)
      ? "wizardError"
      : "";
  if (tasks[name]) {
    // 节点卡片会随轮询重绘，使用节点级锁避免同一操作重复提交。
    const key = button?.dataset.id || "";
    if (key && activeNodeActions.has(key)) {
      return;
    }
    if (key) {
      activeNodeActions.add(key);
    }
    try {
      await action(button, tasks[name], errorId);
    } finally {
      activeNodeActions.delete(key);
      if (key && state) {
        renderNodes();
      }
    }
  }
}

function initialize() {
  applyTheme(storageGet(themeStorageKey));
  document.addEventListener("click", (event) => {
    const target = event.target.closest("button, [data-close]");
    if (!target || target.disabled) {
      return;
    }
    if (target.dataset.dismissToast) {
      target.closest(".toast").remove();
    } else if (target.dataset.page) {
      navigate(target.dataset.page);
    } else if (target.dataset.settingTab) {
      selectSettings(target.dataset.settingTab);
    } else if (target.dataset.close) {
      el(target.dataset.close).close();
    } else if (target.dataset.action) {
      dispatchAction(target.dataset.action, target);
    }
  });
  el("loginForm").addEventListener("submit", login);
  el("settingsForm").addEventListener("submit", saveSettings);
  el("nodeForm").addEventListener("submit", saveNode);
  el("wizardForm").addEventListener("submit", wizardNext);
  el("settingsForm").addEventListener("input", () => {
    settingsDirty = true;
    el("settingsDirty").textContent = "有尚未保存的修改";
    updateStrategyFields();
  });
  el("nodeSearch").addEventListener("input", () => state && renderNodes());
  el("nodeFilter").addEventListener("change", () => state && renderNodes());
  el("linkText").addEventListener("input", () => {
    importedNodes = [];
    el("saveImported").hidden = true;
    el("linkPreview").replaceChildren();
  });
  el("configFile").addEventListener("change", (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    const button = document.querySelector('[data-action="import-config"]');
    action(button, () => importConfig(file));
  });
  el("loginDialog").addEventListener("cancel", (event) => {
    if (!token) {
      event.preventDefault();
    }
  });
  el("confirmDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    finishConfirmation(false);
  });
  el("confirmCancel").addEventListener("click", () =>
    finishConfirmation(false),
  );
  el("confirmAccept").addEventListener("click", () => finishConfirmation(true));
  window.addEventListener("beforeunload", (event) => {
    if (settingsDirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  restoreLogin();
  setInterval(() => {
    if (!document.hidden) {
      refresh();
    }
  }, 5000);
}

document.addEventListener("DOMContentLoaded", initialize);
