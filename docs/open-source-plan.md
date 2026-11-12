# 开源发布改造计划

目标：把自用的 Smart DNS LB 改造成可公开发布的项目——小白能装、能配、界面好看、稳定。
约束：保持现有调度算法/健康判定语义不变（已有 48 个测试必须继续通过）；不引入前端构建链（保持原生 HTML/CSS/JS，无 npm）；Python 3.10+；所有用户可见文案中文为主。

## 阶段 1：开源卫生 + 一键安装 + 配置简化（后端）

1. 隐私清理（不要删除，移动）：把 `docs/deployment-*.md`、`docs/incident-*.md`、`docs/dns-diagnosis-*.md` 移到 `private/`，并把 `private/` 加入 `.gitignore`。全仓库公开文件（README、templates、tests、示例配置和本计划）不得再出现真实部署 IP、真实 UUID、供应商名。示例一律用 203.0.113.0/24、198.51.100.0/24 和 example.com；测试已有的 192.0.2.0/24 文档保留地址可保留。
2. 新增 `LICENSE`（MIT）、`CHANGELOG.md`、`.github/workflows/ci.yml`（Python 3.10/3.11/3.12 跑 unittest + compileall + `node --check` + `bash -n install.sh`）、`.github/ISSUE_TEMPLATE`（bug/feature）。
3. VLESS 分享链接导入：后端 `POST /api/nodes/parse-link`，解析 `vless://uuid@host:port?type=ws&security=tls&sni=..&path=..&host=..&serviceName=..&alpn=..&encryption=..&flow=..#名称`，返回可直接填入节点表单的字段；支持一次粘贴多行批量导入。写完整单元测试（URL 编码、IPv6/域名 host 的错误提示、缺字段）。
4. 首次配置向导所需后端：
   - `GET /api/setup/status`：返回是否已完成基础配置（区域/服务名/NS IP/至少一个节点/mihomo 可用）以及每项缺什么。
   - 只填一个“服务域名”（如 `node.lb.example.com`）即可自动推导 zone、ns_name、soa_mname、soa_rname；`ns_ipv4` 自动探测本机公网 IP（多个公共源，超时短，失败可手填）。
   - Mihomo 路径自动检测（PATH、/usr/local/bin/mihomo、数据目录内），检测结果给前端。
   - `POST /api/setup/check-delegation`：用 dnslib/socket 直接向父区权威和公共递归（1.1.1.1、223.5.5.5）查询服务域名，给出“委派是否生效”的诊断与中文修复建议。
5. 登录体验：`python app.py --reset-token` 重新生成令牌；`--show-token` 打印令牌。前端可选“在此浏览器记住”（localStorage，默认关）。
6. 配置备份：`GET /api/config/export`（默认脱敏，`?include_secrets=1` 含密钥）和 `POST /api/config/import`（校验后整体替换，失败不改动）。
7. install.sh：
   - 自动检测架构并从 MetaCubeX/mihomo 官方 GitHub Release 下载 Mihomo、校验 sha256（优先使用 GitHub API 资产 digest，缺少时查找 release 校验文件），可用 `SKIP_MIHOMO=1` 跳过、`MIHOMO_VERSION=` 指定版本；下载后自动写入设置的 mihomo_path。
   - 53 端口被 systemd-resolved 占用时给出清晰的中文指引（提示 `DNSStubListener=no` 做法），仍不自动改系统。
   - 支持 `install.sh uninstall`（保留数据目录，除非 `--purge`）和重复执行=升级（自动 stop/start）。
   - 结束时打印：面板地址、令牌、SSH 隧道命令、下一步要做什么。
8. 提供 `Dockerfile` + `docker-compose.yml`（network_mode: host，数据卷，内置 Mihomo 可选）。
9. 所有新接口都要有测试；已有测试保持通过。

## 阶段 2：界面重做（前端）

`templates/index.html` + `templates/app.js` 重写为可读的、格式化的代码（不要再压成一行）。可以拆成 `templates/app.css`，由后端静态路由提供。
- 视觉：干净现代的控制台风格，浅色/深色跟随系统并可切换；清晰的信息层级；移动端可用。
- 首页总览：DNS 运行状态、当前调度模式（附一句白话说明）、健康节点数、本周期剩余流量、最近事件。
- 节点：卡片或表格，状态色（UP/DEGRADED/DOWN/UNKNOWN）、延迟、剩余流量进度条、当前分配概率；一键探测、启用/停用、编辑、删除（二次确认）。
- 添加节点：顶部“粘贴 VLESS 链接”一步导入（支持多条），手动表单为备选。
- 首次进入且未完成配置时显示分步向导：①服务域名 ②本机公网 IP（自动探测）③导入节点 ④Mihomo 检测 ⑤探针（可跳过）⑥委派检测，并展示需要在 DNS 服务商添加的 NS/glue 记录（可复制）。
- 设置页分组：基础 / 调度策略（每种模式用白话 + 适用场景说明，只显示该模式相关的参数）/ 健康检查 / 探针 / 高级 JSON。
- 备份导出/导入按钮；toast 提示；所有操作有 loading 与错误提示；表单字段校验提示中文。

## 阶段 3：稳定性与 Bug 审查

由 Claude 审查核心模块（dns.py、state.py、prober.py、scheduler.py、probe_source.py、storage.py）后列出问题清单，Codex 修复并补测试。另需：
- 进程/任务异常不导致整个服务挂掉（后台任务异常要记录并自动重启）。
- Mihomo 子进程崩溃自动拉起、退出时清理干净。
- SQLite 事件表限长、WAL、并发写安全。
- `/healthz` 无鉴权健康检查端点（不泄露信息）。

## 阶段 4：文档

README 重写为：一句话介绍 → 架构图（ASCII 或 mermaid）→ 5 分钟快速开始（一键脚本 / Docker）→ 向导截图位 → DNS 委派图解（Cloudflare/阿里云/DNSPod 示例）→ 调度模式对照表 → FAQ → 进阶说明（保留现有 README 中的技术细节，放到 `docs/` 子文档）。
