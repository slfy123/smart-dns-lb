"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(
  path.join(__dirname, "../templates/app.js"),
  "utf8",
);

function fixture(savedToken = "") {
  const values = new Map(
    savedToken ? [["smart-dns-lb.admin-token", savedToken]] : [],
  );
  const fields = {
    token: { value: "" },
    remember_token: { checked: false },
  };
  const form = {
    elements: { namedItem: (name) => fields[name] },
    querySelector: () => null,
    reset() {},
  };
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        open: false,
        textContent: "",
        value: "",
        hidden: false,
        dataset: {},
        children: [],
        querySelector: () => form,
        querySelectorAll: () => [],
        classList: { toggle() {} },
        showModal() {
          this.open = true;
        },
        close() {
          this.open = false;
        },
        focus() {},
        reset() {},
        append() {},
        setAttribute() {},
        removeAttribute() {},
        replaceChildren() {
          this.textContent = "";
        },
      });
    }
    return elements.get(id);
  }
  elements.set("loginForm", form);
  const sandbox = {
    document: {
      addEventListener() {},
      querySelectorAll: () => [],
      getElementById: element,
      documentElement: {
        dataset: {},
        removeAttribute() {
          delete this.dataset.theme;
        },
      },
      createElement: () => ({ remove() {} }),
    },
    AbortController,
    setTimeout: () => 1,
    clearTimeout() {},
    localStorage: {
      getItem: (key) => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key),
    },
    fetch: async () => ({
      ok: true,
      status: 200,
      json: async () => ({ nodes: [], settings: {}, version: 1 }),
    }),
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  vm.runInContext("render = () => {}; afterLogin = async () => {};", sandbox);
  return { sandbox, values, fields, element, form };
}

async function loginChecks() {
  const page = fixture();
  assert.equal(page.fields.remember_token.checked, false);
  page.fields.token.value = "session-token";
  await page.sandbox.login({ preventDefault() {}, target: page.form });
  assert.equal(page.values.size, 0);
  page.fields.remember_token.checked = true;
  page.fields.token.value = "remembered-token";
  await page.sandbox.login({ preventDefault() {}, target: page.form });
  assert.equal(page.values.get("smart-dns-lb.admin-token"), "remembered-token");
  page.sandbox.logout();
  assert.equal(page.values.size, 0);
  assert.equal(page.fields.remember_token.checked, false);
  assert.equal(page.element("loginDialog").open, true);

  const restored = fixture("stored-token");
  await restored.sandbox.restoreLogin();
  assert.equal(vm.runInContext("token", restored.sandbox), "stored-token");
  restored.sandbox.fetch = async () => ({
    ok: false,
    status: 401,
    json: async () => ({ detail: "expired" }),
  });
  await assert.rejects(restored.sandbox.api("/api/status"), /令牌无效/);
  assert.equal(restored.values.size, 0);
  assert.equal(vm.runInContext("token", restored.sandbox), "");
  assert.equal(restored.element("loginDialog").open, true);

  const disabled = fixture();
  disabled.sandbox.localStorage.getItem = () => {
    throw Error("disabled");
  };
  await disabled.sandbox.restoreLogin();
  assert.equal(disabled.element("loginDialog").open, true);
  disabled.fields.token.value = "session-only";
  disabled.sandbox.localStorage.setItem = () => {
    throw Error("disabled");
  };
  await disabled.sandbox.login({ preventDefault() {}, target: disabled.form });
  assert.equal(disabled.element("loginDialog").open, false);
  disabled.sandbox.logout();
  disabled.fields.token.value = "remember-denied";
  disabled.fields.remember_token.checked = true;
  await disabled.sandbox.login({ preventDefault() {}, target: disabled.form });
  assert.equal(disabled.element("loginDialog").open, true);
  assert.equal(vm.runInContext("token", disabled.sandbox), "");
  assert.match(disabled.element("loginError").textContent, /存储已禁用/);
}

async function interactionChecks() {
  const page = fixture();
  const { sandbox, element } = page;
  assert.equal(sandbox.applyTheme("dark"), "dark");
  assert.equal(sandbox.document.documentElement.dataset.theme, "dark");
  sandbox.cycleTheme();
  assert.equal(sandbox.document.documentElement.dataset.theme, undefined);
  assert.equal(page.values.get("smart-dns-lb.theme"), "auto");
  assert.equal(
    vm.runInContext('esc("<script>\\\"&")', sandbox),
    "&lt;script&gt;&quot;&amp;",
  );
  assert.match(
    vm.runInContext('statusBadge("DEGRADED")', sandbox),
    /status degraded/,
  );
  assert.match(
    vm.runInContext('statusBadge("STALE")', sandbox),
    /status unknown/,
  );
  assert.match(
    vm.runInContext('statusBadge("DISABLED")', sandbox),
    /status unknown[\s\S]*已停用/,
  );
  vm.runInContext(
    `state = {nodes: [{id: "disabled", name: "停用线路", server: "192.0.2.1", port: 443, transport: "tcp", enabled: false, status: "UP", category: "NORMAL", has_uuid: true}]};`,
    sandbox,
  );
  sandbox.renderNodes();
  assert.match(element("nodeGrid").innerHTML, /status unknown[\s\S]*已停用/);
  assert.doesNotMatch(element("nodeGrid").innerHTML, /status up/);
  assert.match(element("nodeGrid").innerHTML, /最近健康：UP/);
  assert.match(element("nodeGrid").innerHTML, /0\.0%/);
  sandbox.updateRequestStatus();
  assert.equal(element("requestStatus").title, "等待登录");
  assert.equal(element("requestStatus").dataset.connected, "false");

  const cancelled = sandbox.confirmAction("删除？", "此操作将删除节点");
  assert.equal(element("confirmDialog").open, true);
  sandbox.finishConfirmation(false);
  assert.equal(await cancelled, false);
  const accepted = sandbox.confirmAction("保存？", "更新配置");
  sandbox.finishConfirmation(true);
  assert.equal(await accepted, true);

  const button = {
    disabled: false,
    textContent: "保存",
    setAttribute() {},
    removeAttribute() {},
  };
  await sandbox.action(
    button,
    async () => {
      assert.equal(button.disabled, true);
      throw Error("中文错误信息");
    },
    "settingsError",
  );
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "保存");
  assert.equal(element("settingsError").textContent, "中文错误信息");

  sandbox.fetch = async () => {
    throw new TypeError("network error");
  };
  await assert.rejects(sandbox.api("/api/status"), /连接服务失败/);
  assert.equal(vm.runInContext("requestCount", sandbox), 0);
}

async function staleRefreshCheck() {
  const { sandbox } = fixture();
  vm.runInContext("token = 'fixture'; state = {version: 8};", sandbox);
  sandbox.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ version: 7 }),
  });
  await sandbox.refresh();
  assert.equal(vm.runInContext("state.version", sandbox), 8);
}

async function expiredSessionResponseCheck() {
  const { sandbox } = fixture();
  vm.runInContext("token = 'fixture'; state = {version: 1};", sandbox);
  let release;
  sandbox.fetch = () =>
    new Promise((resolve) => {
      release = resolve;
    });
  const pendingRequest = sandbox.api("/api/status");
  sandbox.logout(false);
  release({ ok: true, status: 200, json: async () => ({ version: 2 }) });
  await assert.rejects(pendingRequest, /登录状态已变化/);
  assert.equal(vm.runInContext("state", sandbox), null);
}

(async () => {
  await loginChecks();
  await interactionChecks();
  await staleRefreshCheck();
  await expiredSessionResponseCheck();
  console.log(
    "Frontend authentication, theme, confirmation, loading and stale refresh checks passed",
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
