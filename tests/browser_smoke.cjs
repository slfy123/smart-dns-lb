"use strict";
// 可选真实浏览器验收：只访问本地 HTTP 夹具，不接触公网 DNS 或生产数据。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { once } = require("node:events");

const root = path.resolve(__dirname, "..");
const artifacts = path.join(root, ".test-data", "stage2-browser");
fs.mkdirSync(artifacts, { recursive: true });
const profile = fs.mkdtempSync(path.join(artifacts, "profile-"));
const browserPath =
  process.env.BROWSER_PATH ||
  [
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ].find((file) => fs.existsSync(file));
if (!browserPath) throw Error("请通过 BROWSER_PATH 指定 Chromium/Edge/Chrome");
const python =
  process.platform === "win32"
    ? path.join(root, ".venv/Scripts/python.exe")
    : "python3";
const defaults = spawnSync(
  python,
  [
    "-c",
    'import json; from smart_dns.models import Settings, Node; print(json.dumps({"settings": Settings().model_dump(), "node": Node(id="fixture-a",name="线路甲",server="203.0.113.21",port=443).model_dump()}))',
  ],
  { cwd: root, encoding: "utf8", windowsHide: true },
);
assert.equal(defaults.status, 0, defaults.stderr);
const template = JSON.parse(defaults.stdout);
let state = {
  version: 1,
  settings: template.settings,
  nodes: [],
  is_peak: false,
  dns: { running: true, total_queries: 18240, answers: 18000 },
  probe: { executor_status: "Mihomo 已就绪", source_status: "等待配置" },
  events: [
    { created_at: "2026-11-13T06:00:00Z", message: "服务已启动，等待首次配置" },
  ],
};
let delegated = false;
const mutations = [];
const requests = [];
const errors = [];

function publicNode(node, index = 0) {
  return {
    ...template.node,
    ...node,
    has_uuid: true,
    status: ["UP", "DEGRADED", "DOWN"][index % 3],
    ewma_rtt_ms: 86 + index * 65,
    effective_weight: index === 2 ? 0 : 60 - index * 20,
    traffic_fresh: true,
    traffic_remaining_gib: 410 - index * 100,
    traffic_limit_gib: 600,
    traffic_used_gib: 190 + index * 100,
    last_error: "",
    traffic_error: "",
  };
}

function setupStatus() {
  const configured = state.settings.zone !== "example.com.";
  const flags = {
    zone: configured,
    service_name: configured,
    ns_ipv4: state.settings.ns_ipv4 !== "127.0.0.1",
    nodes: state.nodes.length > 0,
    mihomo: !!state.settings.mihomo_path,
  };
  return {
    complete: Object.values(flags).every(Boolean),
    checks: Object.fromEntries(
      Object.entries(flags).map(([key, ok]) => [
        key,
        { ok, message: ok ? "已配置" : "请配置" },
      ]),
    ),
    missing: Object.keys(flags).filter((key) => !flags[key]),
    mihomo: {
      available: true,
      path: "/usr/local/bin/mihomo",
      message: "已找到可执行文件",
    },
    public_ip: {
      detected: true,
      ip: "203.0.113.10",
      message: "本地测试夹具，请核对公网 IPv4",
    },
    records: [
      { name: state.settings.zone, type: "NS", value: state.settings.ns_name },
      {
        name: state.settings.ns_name,
        type: "A",
        value: state.settings.ns_ipv4,
      },
    ],
    settings: state.settings,
  };
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, "http://127.0.0.1");
    const staticFiles = {
      "/": ["index.html", "text/html"],
      "/assets/app.js": ["app.js", "text/javascript"],
      "/assets/app.css": ["app.css", "text/css"],
    };
    if (staticFiles[url.pathname]) {
      const [file, type] = staticFiles[url.pathname];
      response.writeHead(200, { "Content-Type": type + "; charset=utf-8" });
      response.end(fs.readFileSync(path.join(root, "templates", file)));
      return;
    }
    const json = (value) => {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify(value));
    };
    if (request.headers.authorization !== "Bearer fixture-token") {
      response.writeHead(401, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ detail: "需要管理令牌" }));
      return;
    }
    let raw = "";
    for await (const chunk of request) raw += chunk;
    const body = raw ? JSON.parse(raw) : {};
    if (request.method !== "GET")
      mutations.push({ method: request.method, path: url.pathname, body });
    if (url.pathname === "/api/status") return json(state);
    if (url.pathname === "/api/setup/status") return json(setupStatus());
    if (url.pathname === "/api/events") return json({ events: state.events });
    if (url.pathname === "/api/config/export") {
      return json({
        version: state.version,
        settings: state.settings,
        nodes: state.nodes,
        redacted: !url.searchParams.get("include_secrets"),
      });
    }
    if (url.pathname === "/api/config/import") {
      state.settings = body.settings;
      state.nodes = body.nodes.map(publicNode);
      state.version += 1;
      return json(state);
    }
    if (url.pathname === "/api/nodes/parse-link") {
      return json({
        nodes: [
          publicNode({
            ...template.node,
            uuid: "00000000-0000-4000-8000-000000000001",
            tls_sni: "edge.example.com",
            name: "线路甲 <测试>",
          }),
        ],
        warnings: ["edge.example.com：已将域名解析为 203.0.113.21"],
      });
    }
    if (url.pathname === "/api/config" && request.method === "PUT") {
      const incoming = body.settings || {};
      if (incoming.service_domain) {
        const domain = incoming.service_domain.replace(/\.$/, "");
        if (domain.split(".").length < 4) {
          response.writeHead(422, { "Content-Type": "application/json" });
          response.end(
            JSON.stringify({
              detail: "请使用独立子区，例如 node.lb.example.com",
            }),
          );
          return;
        }
        const zone = domain.split(".").slice(1).join(".") + ".";
        Object.assign(incoming, {
          service_name: domain + ".",
          zone,
          ns_name: "ns1." + zone,
        });
        delete incoming.service_domain;
      }
      state.settings = { ...state.settings, ...incoming };
      if (body.nodes) state.nodes = body.nodes.map(publicNode);
      state.version += 1;
      return json(state);
    }
    if (url.pathname === "/api/setup/check-delegation") {
      delegated = true;
      return json({
        effective: true,
        message: "委派已生效",
        suggestions: [],
        parent: [
          { hostname: "ns.example.com", delegated: true, glue_ok: true },
        ],
        recursive: [
          { server: "1.1.1.1", delegated: true, rcode: "NOERROR" },
          { server: "223.5.5.5", delegated: true, rcode: "NOERROR" },
        ],
      });
    }
    if (/\/api\/nodes\/[^/]+\/probe$/.test(url.pathname))
      return json({ success: true, rtt_ms: 86 });
    if (url.pathname.startsWith("/api/nodes/")) {
      const id = decodeURIComponent(url.pathname.split("/")[3]);
      if (request.method === "DELETE")
        state.nodes = state.nodes.filter((node) => node.id !== id);
      else
        state.nodes = state.nodes.map((node) =>
          node.id === id ? { ...node, ...body } : node,
        );
      state.version += 1;
      return json(state);
    }
    response.writeHead(404, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ detail: "fixture route missing" }));
  } catch (error) {
    response.writeHead(500);
    response.end(String(error));
  }
});

let browser;
let socket;
let session;
let nextId = 0;
const pending = new Map();

async function send(method, params = {}, sessionId = session) {
  const id = ++nextId;
  const promise = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(Error("CDP 超时：" + method));
    }, 15000);
    pending.set(id, { resolve, reject, timer });
  });
  socket.send(
    JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }),
  );
  return promise;
}

async function evaluate(expression) {
  const result = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  });
  if (result.exceptionDetails)
    throw Error(
      result.exceptionDetails.text +
        ": " +
        result.exceptionDetails.exception?.description,
    );
  return result.result.value;
}

async function waitFor(expression) {
  for (let count = 0; count < 100; count += 1) {
    if (await evaluate(expression)) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw Error("等待条件失败：" + expression);
}

async function click(selector) {
  assert.equal(
    await evaluate(
      `Boolean(document.querySelector(${JSON.stringify(selector)}))`,
    ),
    true,
    selector,
  );
  await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
}

async function fill(selector, value) {
  await evaluate(
    `(() => { const input = document.querySelector(${JSON.stringify(selector)}); input.value = ${JSON.stringify(value)}; input.dispatchEvent(new Event('input', {bubbles:true})); })()`,
  );
}

async function screenshot(name) {
  await evaluate(
    'document.querySelectorAll("[data-dismiss-toast]").forEach(button => button.click())',
  );
  await send("Page.bringToFront");
  const result = await send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
  });
  fs.writeFileSync(
    path.join(artifacts, name + ".png"),
    Buffer.from(result.data, "base64"),
  );
}

(async () => {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const port = server.address().port;
  browser = spawn(
    browserPath,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      "--remote-debugging-port=0",
      "--user-data-dir=" + profile,
      "about:blank",
    ],
    { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] },
  );
  const endpoint = await new Promise((resolve, reject) => {
    let stderr = "";
    const timer = setTimeout(
      () => reject(Error("浏览器 DevTools 启动超时")),
      15000,
    );
    browser.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
      const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) {
        clearTimeout(timer);
        resolve(match[1]);
      }
    });
    browser.once("error", reject);
    browser.once("exit", (code) => {
      clearTimeout(timer);
      reject(Error("浏览器退出：" + code + " " + stderr.slice(-800)));
    });
  });
  socket = new WebSocket(endpoint);
  await once(socket, "open");
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const request = pending.get(message.id);
      clearTimeout(request.timer);
      pending.delete(message.id);
      if (message.error) request.reject(Error(JSON.stringify(message.error)));
      else request.resolve(message.result);
    } else if (message.method === "Runtime.exceptionThrown") {
      errors.push(
        message.params.exceptionDetails.exception?.description ||
          message.params.exceptionDetails.text,
      );
    } else if (message.method === "Network.requestWillBeSent") {
      requests.push(message.params.request.url);
    }
  });
  const { targetId } = await send(
    "Target.createTarget",
    { url: "about:blank" },
    null,
  );
  session = (
    await send("Target.attachToTarget", { targetId, flatten: true }, null)
  ).sessionId;
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Network.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 1080,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-color-scheme", value: "light" }],
  });
  await send("Page.navigate", { url: `http://127.0.0.1:${port}/` });
  await waitFor('document.getElementById("loginDialog")?.open');
  await fill('#loginForm [name="token"]', "fixture-token");
  await click('#loginForm [type="submit"]');
  await waitFor(
    'document.getElementById("wizardDialog").open && wizardStep === 0',
  );
  await fill('#wizardForm [name="service_domain"]', "dns.example.com");
  await click("#wizardNext");
  await waitFor(
    'document.getElementById("wizardError").textContent.includes("独立子区")',
  );
  await fill('#wizardForm [name="service_domain"]', "node.lb.example.com");
  await click("#wizardNext");
  await waitFor(
    'wizardStep === 1 && !document.getElementById("wizardNext").disabled',
  );
  await click("#wizardNext");
  await waitFor(
    'wizardStep === 2 && !document.getElementById("wizardNext").disabled',
  );
  await click('#wizardContent [data-action="import-links"]');
  await fill(
    "#linkText",
    "vless://00000000-0000-4000-8000-000000000001@edge.example.com:443?type=ws&security=tls&headerType=none&allowInsecure=0",
  );
  await click('[data-action="parse-links"]');
  await waitFor('!document.getElementById("saveImported").hidden');
  assert.match(
    await evaluate('document.getElementById("linkPreview").textContent'),
    /已将域名解析为/,
  );
  assert.equal(
    await evaluate(
      'document.getElementById("linkPreview").querySelector("测试") !== null',
    ),
    false,
  );
  await click("#saveImported");
  await waitFor('document.getElementById("confirmDialog").open');
  await click("#confirmAccept");
  await waitFor(
    '!document.getElementById("nodeDialog").open && state.nodes.length === 1',
  );
  for (const step of [3, 4, 5]) {
    await click("#wizardNext");
    await waitFor(
      `wizardStep === ${step} && !document.getElementById("wizardNext").disabled`,
    );
  }
  assert.equal(
    await evaluate(
      'document.querySelectorAll("#wizardContent tbody tr").length',
    ),
    2,
  );
  assert.match(
    await evaluate('document.getElementById("wizardContent").textContent'),
    /灰云/,
  );
  const font = await evaluate(
    'getComputedStyle(document.querySelector("#wizardContent td code")).fontFamily',
  );
  assert.match(
    font,
    /ui-monospace.*Cascadia Mono.*Consolas.*SF Mono.*Menlo.*monospace/,
  );
  assert.equal(
    await evaluate('document.querySelectorAll(".step-number").length'),
    6,
  );
  assert.equal(
    await evaluate(
      'getComputedStyle(document.querySelector(".step-number")).borderRadius',
    ),
    "50%",
  );
  await click('[data-action="check-delegation"]');
  await waitFor(
    'document.getElementById("delegationResult").textContent.includes("委派已生效")',
  );
  assert.equal(delegated, true);
  await screenshot("wizard-delegation");
  await click("#wizardNext");
  await waitFor('!document.getElementById("wizardDialog").open');

  state.nodes.push(
    publicNode({ ...template.node, id: "fixture-b", name: "线路乙" }, 1),
    publicNode({ ...template.node, id: "fixture-c", name: "线路丙" }, 2),
  );
  await evaluate("refresh()");
  await screenshot("overview-light");
  await click('.nav-item[data-page="nodes"]');
  await screenshot("nodes-light");
  const beforeDelete = mutations.filter(
    (item) => item.method === "DELETE",
  ).length;
  await click('[data-action="delete-node"][data-id="fixture-c"]');
  await waitFor('document.getElementById("confirmDialog").open');
  await click("#confirmCancel");
  assert.equal(
    mutations.filter((item) => item.method === "DELETE").length,
    beforeDelete,
  );
  await click('[data-action="toggle-node"][data-id="fixture-a"]');
  await waitFor('document.getElementById("confirmDialog").open');
  await click("#confirmAccept");
  await waitFor(
    'state.nodes.find(node => node.id === "fixture-a").enabled === false',
  );
  assert.equal(
    await evaluate(
      'document.querySelector("#nodeGrid .node-card .status").textContent.trim()',
    ),
    "已停用",
  );
  assert.equal(
    await evaluate(
      'document.querySelector("#nodeGrid .node-card .status").classList.contains("unknown")',
    ),
    true,
  );
  await click('[data-action="probe-node"][data-id="fixture-b"]');
  await waitFor("requestCount === 0");
  await click('[data-action="edit-node"][data-id="fixture-b"]');
  await fill("#node-name", "线路乙（已编辑）");
  await click('#nodeForm [type="submit"]');
  await waitFor('document.getElementById("confirmDialog").open');
  await click("#confirmAccept");
  await waitFor(
    '!document.getElementById("nodeDialog").open && state.nodes.some(node => node.name === "线路乙（已编辑）")',
  );

  await click('.nav-item[data-page="settings"]');
  await click('[data-setting-tab="strategy"]');
  await evaluate(
    'document.querySelector("#setting-mode").value="LOWEST_RTT"; document.querySelector("#setting-mode").dispatchEvent(new Event("input",{bubbles:true}));',
  );
  assert.equal(
    await evaluate('document.querySelector("[data-field=peak_start]").hidden'),
    true,
  );
  assert.equal(
    await evaluate(
      'document.querySelector("[data-field=lowest_rtt_hold_seconds]").hidden',
    ),
    false,
  );
  await evaluate("refresh()");
  assert.equal(
    await evaluate('document.querySelector("#setting-mode").value'),
    "LOWEST_RTT",
  );
  await screenshot("settings-light");
  assert.match(
    await evaluate(
      'document.querySelector("[data-setting-panel=strategy]").textContent',
    ),
    /高峰开始与结束相同表示全天高峰/,
  );
  await evaluate('applyTheme("dark")');
  await screenshot("settings-dark");
  await click('#settingsForm [type="submit"]');
  await waitFor('document.getElementById("confirmDialog").open');
  await click("#confirmCancel");
  assert.equal(state.settings.mode, "TRAFFIC_AWARE");
  await click('#settingsForm [type="submit"]');
  await waitFor('document.getElementById("confirmDialog").open');
  await click("#confirmAccept");
  await waitFor('state.settings.mode === "LOWEST_RTT" && !settingsDirty');

  const downloads = path.join(artifacts, "downloads");
  fs.mkdirSync(downloads, { recursive: true });
  await send(
    "Browser.setDownloadBehavior",
    { behavior: "allow", downloadPath: downloads },
    null,
  );
  await click('[data-action="export"]');
  await waitFor("requestCount === 0");
  const replacement = { settings: state.settings, nodes: [state.nodes[0]] };
  await evaluate(
    `(() => { const transfer = new DataTransfer(); transfer.items.add(new File([${JSON.stringify(JSON.stringify(replacement))}], 'fixture-backup.json', {type:'application/json'})); const input = document.getElementById('configFile'); input.files = transfer.files; input.dispatchEvent(new Event('change')); })()`,
  );
  await waitFor('document.getElementById("confirmDialog").open');
  await click("#confirmAccept");
  await waitFor("state.nodes.length === 1 && requestCount === 0");

  await send("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  for (const page of ["overview", "nodes", "settings", "events"]) {
    await evaluate(`navigate('${page}')`);
    assert.equal(
      await evaluate("document.documentElement.scrollWidth <= innerWidth"),
      true,
      "移动端溢出：" + page,
    );
  }
  await evaluate('navigate("nodes"); applyTheme("light")');
  assert.equal(
    await evaluate(
      'getComputedStyle(document.getElementById("requestStatus")).fontSize',
    ),
    "0px",
  );
  assert.equal(
    await evaluate(
      'document.getElementById("requestStatus").getBoundingClientRect().width',
    ),
    10,
  );
  await screenshot("nodes-mobile");
  await evaluate("logout(false)");
  assert.equal(
    await evaluate('document.getElementById("loginDialog").open'),
    true,
  );
  assert.equal(
    await evaluate('document.getElementById("nodeGrid").textContent'),
    "",
  );
  assert.deepEqual(errors, []);
  assert.equal(
    requests.every(
      (url) =>
        url.startsWith(`http://127.0.0.1:${port}/`) || url.startsWith("data:"),
    ),
    true,
    "页面包含外部资源请求",
  );
  console.log(
    "Browser smoke passed: six-step wizard, link import, delegation, confirmations, settings preservation, 390px layout, light/dark and logout.",
  );
  console.log("Screenshots: " + artifacts);
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (socket?.readyState === WebSocket.OPEN) {
      try {
        await send("Browser.close", {}, null);
      } catch (_) {}
      socket.close();
    }
    if (browser && browser.exitCode === null) browser.kill();
    server.close();
    for (const request of pending.values()) clearTimeout(request.timer);
  });
