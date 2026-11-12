# 本地验证记录

验证环境：Windows，Python 3.12.10；依赖版本记录于 `requirements.lock.txt`。

## R1–R8 回归修复（2026-11-13）

本节为最新结果，下方阶段 3/4 首轮验收记录保留为历史。新增 `tests/test_stage3_regressions.py` 共 16 项；修复前运行得到 9 项失败、4 项错误，修复后全部通过。原测试中的 CD 预期、噪声限频标记和 TCP 上限断言同步更新。

| 项目 | 结果 |
| --- | --- |
| R1 | 初始化 / replace_config 缓存观测超时；64 节点选择遍历次数保持 O(N)。10000 次本机选择平均 47.74µs，CI 使用遍历次数断言避免机器性能波动。 |
| R2 | add_event 捕获存储异常并保留内存事件；多个状态变化、权重刷新和手动探测继续运行。探测错误退避上限为 probe_interval。 |
| R3 | 仅显式标记的噪声限频，UP→DOWN→UP→DOWN 以及连续配置保存完整记录。 |
| R4 | DNS 响应复制 CD，继续清理 TC / Z / RCODE 等请求标志。 |
| R5 | 更换 Mihomo 的原健康节点获得有界启动宽限；就绪后缩短，真实观测后结束，未就绪不续期；含 64 节点池和过期后退出测试。 |
| R6 | 进程 wait 完成前保留跟踪且停止提供就绪出口；取消后可再次 terminate / kill 并清除配置。 |
| R7 | 相同开始 / 结束时刻表示全天高峰；缺省结束和 24:00 导入通过，前端、README 与进阶文档同步。 |
| R8 | TCP_PER_IP_LIMIT=32，注释说明大型递归并行需求；验证第 32 条接受、第 33 条拒绝。 |

全量 Python 137 项通过，Node 前端测试通过；外部网络与子进程故障使用 mock。R7 前端提示也加入本地 Edge 冒烟断言。未执行 Git 提交。

## U1–U4、阶段 3 与阶段 4（2026-11-13）

本节为当前验收记录；下方阶段 1、2 的记录按原样保留为历史。未执行 Git 提交。

界面修复：DNS 记录使用指定的 `ui-monospace, "Cascadia Mono", Consolas, "SF Mono", Menlo, monospace`；向导步骤使用有间隔的圆形序号；停用节点显示灰色“已停用”，最近健康作为次要信息且分配概率为零；390px 顶栏只显示连接圆点，完整文字保留在可访问状态与 title 中。真实 Windows Edge 的样式断言和截图检查通过。

| 审查项 | 状态与处理 |
| --- | --- |
| 1 后台任务永久退出 | 修复：统一 supervised_loop，操作和事件回调分别容错、日志及退避；含 lifespan 定时器与 poller 故障注入测试 |
| 2 串行启动与排队过期 | 修复：并发启动且管理循环独立于探测，过期阈值计入完整批次 |
| 3 设置更新清空健康 | 修复：保留尚未过期的原健康和认证记录，实际连接参数变化仍重新认证 |
| 4 监听覆盖持久化 | 修复：运行时覆盖独立保存，并基于启动配置及覆盖计算重启提示 |
| 5 鉴权前持锁 | 修复：先鉴权、接收有界请求体，再拿配置锁；1 MiB / 5 秒 |
| 6 TCP 退出挂起 | 修复：先关闭 writer 并取消连接任务，再等待服务器关闭 |
| 7 本地端口 | 修复：20000–32767，校验期间持有 socket，并发就绪检查；子进程实际绑定前的极短交接窗口见风险 |
| 8 响应头标志 | 修复：重新构造 DNSHeader，只保留事务 ID、opcode 和 RD |
| 9 EDNS | 修复：返回 OPT，版本非零返回 BADVERS |
| 10 空 question | 修复：原样复制 questions 列表，零问题 FORMERR 保持零问题 |
| 11 NODATA 统计 | 修复：apex / NS NODATA 计为正常答案 |
| 12 慢 TCP | 修复：每 IP 8 条、总计 128 条、连接总寿命 30 秒 |
| 13 SMART_PEAK | 修复：先时段组，再 UP 偏好；已在 CHANGELOG 说明 |
| 14 删除节点残留字典 | 修复：清理空闲节点锁、重试与失败计数记录 |
| 15 EWMA | 修复：重复计时刷新不推进平滑，仅新快照或权重输入变化推进；已在 CHANGELOG 说明 |
| 16 预算中位数 | 修复：排除停用与维护节点；已在 CHANGELOG 说明 |
| 17 事件洪泛与清理 | 修复：同一事件五分钟限频，每 100 次插入按 ID 修剪，启动时清理旧记录 |
| 18 clear_uuid:false | 修复：始终移除控制字段 |
| 19 Windows 强杀孤儿 | 暂缓：正常关闭已清理；Job Object 嵌套兼容需独立验证，按进程名清理会影响其他实例；生产路径为 Linux systemd / Docker |
| 20 午夜 | 修复：结束接受 24:00 并归一为 00:00，新配置默认午夜；保留旧时段 |
| S1 Encryption | 修复：公开状态与默认导出脱敏、留空编辑保留、脱敏导入按 ID 恢复 |
| S2 Windows ACL | 按审查允许的文档方案处理：说明继承权限、私有数据目录和 NTFS ACL 核查步骤 |

阶段 3 另外覆盖：Mihomo 崩溃指数退避重启、启动取消时终止进程并清除生成的凭据文件；SQLite WAL 与并发写测试；`/healthz` 严格只含 `ok` / `dns` 布尔值。新增 `tests/test_stage3.py` 共 30 项回归测试，首批已在修复前运行并复现失败，修复后通过；原 Encryption 明文断言更新为脱敏断言，其他已有测试保留。

阶段 4：重写 README，包含架构图、两条克隆安装路径、六步向导、截图位、三家 DNS 服务商的具体记录表、七种调度模式和五项 FAQ。原 README 技术正文完整迁入 `docs/advanced.md`，补充运行边界与 Windows 权限；CHANGELOG 记录三项明确调度 bug；CI 显式运行 Node 前端测试。

验收命令：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
node tests/test_frontend.js
.venv\Scripts\python.exe -m compileall -q app.py smart_dns scripts tests
node --check templates/app.js
node --check tests/test_frontend.js
node --check tests/browser_smoke.cjs
"C:\Program Files\Git\bin\bash.exe" -n install.sh
node tests/browser_smoke.cjs
git diff --check
```

外部 HTTP / DNS / Release 下载均使用 mock；保留原有回环 socket 与本地辅助进程替身集成测试。浏览器只访问本地夹具，无远程页面资源。公开文本文件经字节检查均为 LF。

最终全量结果：`Ran 121 tests in 6.407s / OK`；`node tests/test_frontend.js` 与 Windows Edge headless 冒烟全部通过。compileall、三个 JS 语法检查、Git Bash 脚本语法与公开文件 LF 检查通过。

遗留风险：Windows 强杀残留进程；Mihomo 不接收监听 fd 导致的极短端口交接窗口；大节点池的有效过期阈值更长；Linux systemd / Docker、真实 VLESS 与公网委派仍需现场验收。本轮未进行外网调用或生产部署。

## A–F 修复与阶段 2 验收（2026-11-13）

本轮完成 A–F 修复及阶段 2，未执行 Git 提交，未开始阶段 3。以下阶段 1 记录保留为历史，当前行为以本节和 README 为准。

| 验收项 | 当前结果 |
| --- | --- |
| A：Mihomo 校验 | 优先读取选中资产的 `digest: sha256:<hex>`，再回退专用/汇总校验文件；摘要不符或所有校验来源缺失时保留已有内核。amd64 优先 compatible 构建。覆盖仅 digest、digest 优先、无效 digest 回退及摘要不符。 |
| B：独立子区 | 拒绝会推导主域的 `dns.example.com`、`dns.example.com.cn`、`dns.example.co.uk` 等名称；提示使用 `node.lb.example.com`。覆盖推导函数、API 422 与失败不改配置。常见多级公共后缀采用内置列表，未引入远程 PSL 请求。 |
| C：链接兼容 | 支持 3x-ui/v2rayN 常见 WS/TCP+TLS 链接及无害附加参数，保留证书校验选项语义。Reality/pbk/sid、xhttp/httpupgrade/kcp、HTTP 伪装等给出中文原因。域名异步解析 IPv4，三秒超时，SNI 留空时沿用域名；返回 `warnings` 提醒固定 IP。 |
| D：公网 IP 缓存 | 按应用实例缓存十分钟，成功和失败均缓存；`?refresh_ip=1` 强制刷新；并发初次读取只执行一次探测。 |
| E：Docker 默认可用 | Dockerfile/Compose 默认 `INSTALL_MIHOMO=1`，仍可显式设为 0。首次启动无路径时检测并持久保存 `/usr/local/bin/mihomo` 等可执行文件。 |
| F：可读性 | setup、links、API、下载器、应用入口及本轮 Python 测试经 Black 排版；HTML/CSS/JS 经 Prettier 排版。工具仅用于开发验证，未加入运行依赖或前端构建链。 |
| 控制台 | 本地 CSS/JS、系统字体、tabular-nums、中性背景和青绿强调；侧边导航/窄屏顶部标签、浅色/系统深色/记忆主题。总览、节点、设置、事件四页齐备。 |
| 向导 | 六步：服务域名、公网 IPv4、节点、Mihomo、可跳过探针、DNS 委派；记录表、一键复制、父区/递归诊断、Cloudflare 灰云和 UDP/TCP 53 指引。可暂时关闭，已保存配置保留。 |
| 节点与设置 | 链接预览后整批提交；手动添加/编辑、探测、启停、删除；健康色、延迟、额度进度和概率。设置按组展示、策略参数按模式筛选，草稿不被刷新覆盖。 |
| 交互与边界 | 请求状态、按钮 loading、中文字段/网络错误、toast 与空状态；破坏性变更二次确认，备份导入/导出齐备。静态资源严格白名单，穿越/未知路径返回 404。退出/401 清除令牌与敏感表单，迟到响应不恢复旧页面。 |

当前全量测试：`.venv\Scripts\python.exe -m unittest discover -s tests -v`，**91 项全部通过**；原有 48 项保持通过，外部 HTTP、DNS 与下载全部 mock。回环协议与本地进程替身集成测试继续保留。

已通过 `compileall`、三个 JS 文件的 `node --check`、Git Bash `bash -n install.sh`、Black `--check` 和 `git diff --check`。检查公开源码/文档 LF 行尾与 IP/UUID；无真实部署资料，原有 192.0.2.x 测试地址保留。

真实 Edge headless 执行 `node tests/browser_smoke.cjs`，全程只访问本地 HTTP 夹具，检查登录、六步向导、主域错误提示、链接预览与导入、DNS 委派、节点编辑/探测/停用、删除取消、设置草稿/保存确认、备份下载/导入、390px 四页无横向溢出、主题与退出清理；未捕获 JS 异常，页面未发起外部资源请求。截图位于被忽略的 `.test-data/stage2-browser/`：

- `overview-light.png`、`nodes-light.png`
- `settings-light.png`、`settings-dark.png`
- `wizard-delegation.png`、`nodes-mobile.png`

仍需部署环境验收：Linux root/systemd 升级与卸载、Docker 构建、真实 Release 下载、真实 VLESS 与公网委派。域名链接保存的是导入时的一个 IPv4；入口 A 记录变化后需重新导入。浏览器测试使用本地夹具，并不代替真实供应商 DNS 控制台与剪贴板权限验收。

## 阶段 1 补齐验收（2026-11-12）

本次仅完成 `docs/open-source-plan.md` 阶段 1，未提交 Git，阶段 2 尚未开始。

| 项目 | 结果 |
| --- | --- |
| 1. 隐私清理 | 部署/事件/DNS 诊断文件保留在被忽略的 `private/`；计划自身的真实部署信息已移除；测试的 192.0.2.x 保留。Docker 上下文也排除私有资料。 |
| 2. 开源基础文件 | MIT、Changelog、中文 bug/feature 模板齐备；CI 配置 Python 3.10/3.11/3.12，执行 unittest、compileall、Node 和 Bash 语法检查。 |
| 3. VLESS 导入 | 支持单条/多行、URL 解码、TCP/WS/gRPC、ALPN/encryption/flow；IPv6/域名/缺字段/无效参数给出错误，整批失败不保存。 |
| 4. 首次配置后端 | 五项完成度检查、单服务域名推导、多源公网 IPv4 探测、Mihomo 检测；委派检测直接查询父区权威及两组公共递归，核对 NS/glue，单列服务 A 记录健康。 |
| 5. 登录体验 | `--show-token`/`--reset-token` 独立于配置解析；重置采用原子替换；浏览器记忆默认关闭，退出/401 清除。运行中的服务重置令牌后需重启。 |
| 6. 配置备份 | 默认脱敏、可导出完整密钥；严格校验后整体替换；完整备份空凭据清空旧值；脱敏备份按 ID 恢复现有密钥，缺失时拒绝导入；失败保留配置。 |
| 7. 安装/升级/卸载 | 脚本 LF，Git Bash 语法检查通过；支持架构、版本、强制 SHA-256、自动保存路径、升级 stop/start、卸载保留数据及 `--purge`、resolved 指引、实际面板地址和令牌/隧道提示。 |
| 8. Docker | host 网络、数据卷、可选内置 Mihomo；复用同一强制校验下载器；支持挂载宿主机二进制。 |
| 9. 测试 | 原有 48 项继续通过；总计 76 项，新增外网调用全部 mock。 |

本次检查命令：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m compileall -q app.py smart_dns scripts tests
node --check templates/app.js
"C:\Program Files\Git\bin\bash.exe" -n install.sh
git diff --check
```

测试额外覆盖公网 IPv4 失败回退、父区缺少 glue/查询超时、DNS TCP 短包接收和问题匹配、配置导入校验/持久化失败/并发冲突/Mihomo 切换，以及下载摘要缺失/错误/损坏压缩包时保留已有内核。浏览器记忆使用 Node.js VM 模拟页面和 localStorage，无前端依赖。

原有回环 UDP/TCP、HTTP 和辅助进程替身集成测试继续保留；新增阶段 1 测试的 HTTP/DNS/下载全部使用 mock。Linux root/systemd 安装、Docker 镜像构建、真实 GitHub Release 资源和公网 NS/glue 传播未在本 Windows 环境执行，需部署环境验收。CI 的三版本矩阵为已配置状态，本地实际运行 Python 3.12。

## 早期验证记录（原有）

- `python -m unittest discover -s tests -q`：29 项通过。
- `python -m compileall -q app.py smart_dns tests`：通过。
- `node --check templates/app.js`：通过。
- Git Bash 的 `bash -n install.sh`：通过，仅语法校验。
- `python -m pip check`：依赖一致性通过。
- `git diff --check`：通过。
- 使用独立本地数据目录启动实际 Uvicorn 应用，在 127.0.0.1:18080 验证 healthz、令牌鉴权、静态脚本、节点新增/编辑及 DNS 15353 端口的真实 UDP/TCP SOA 查询。
- 浏览器确认登录成功、仪表盘正确显示 DNS 运行状态和 Mihomo 未配置状态；新增线路表单显示默认端口、权重、优先级、额度和维护选项。

测试涵盖 DNS A/AAAA、SOA/NS、NXDOMAIN/NODATA、REFUSED、SERVFAIL、EDNS 大小及截断、忽略 DNS 响应报文；加权概率、主备、最低延迟滞回、跨午夜时段、故障阈值、恢复观察、过期状态、配置版本隔离；探针计费单位及四种口径、流量过期回退、额度阈值、流量因素；管理鉴权、秘密字段保留/遮罩、冲突校验、配置持久化；应用完整 lifespan 及端口释放；辅助进程启动、节点参数变更重启、退出与生成凭证文件清理。

VLESS 请求测试使用受控 HTTP 传输模拟成功、代理失败、目标共同故障及出口 IP 不符；进程监督测试使用本地辅助进程替身。这些测试验证程序逻辑，不代表真实 VLESS 协议链路已验证。

## 部署后验收

1. 安装实际 Mihomo，分别用 TLS+TCP、WS 或 gRPC 的真实参数测试线路与证书校验。
2. 对照供应商账单核对探针周期流量、上下行计费口径与重置日。
3. 在 Linux 执行安装，检查 systemd 权限、绑定、防火墙、服务重启和辅助进程退出。
4. 完成父区 NS/glue 委派，分别从外网验证 UDP/TCP 53 与递归 DNS 缓存行为。
5. 分别停止线路转发、停止落地 VLESS、恢复线路，记录权威答案变更和实际 Clash/Mihomo 连接恢复时间。
6. 观察真实字节流量进度和高负载延迟，调整基础权重、带宽及额度余量阈值。DNS 查询比率与字节流量比率分开核对。
