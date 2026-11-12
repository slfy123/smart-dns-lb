# 进阶运行、调度与接口说明

以下保留原 README 的全部技术说明；新手安装入口见 [README](../README.md)。末尾补充阶段 3 的稳定性行为。

## 原技术说明

这是一个面向 `3x-ui + VLESS + Clash/Mihomo` 的权威 DNS 调度程序。它让客户端使用一个固定域名，后台根据端到端 VLESS 探测结果选择线路入口，并可读取 CF-Server-Monitor 兼容的 `/api/servers` 数据，用月流量和带宽参数动态修正新 DNS 查询的权重。

## 当前能力

- Python 单进程控制服务：FastAPI、权威 DNS、调度状态和 SQLite 配置。
- UDP/TCP 权威 DNS，A、AAAA、SOA、NS、NXDOMAIN、NODATA、SERVFAIL、区域外 REFUSED。
- `WEIGHTED`、`LOWEST_RTT`、`FAILOVER`、`SMART_PEAK`、`TRAFFIC_AWARE`、`TRAFFIC_REMAINING`、`SMART_PEAK_REMAINING` 七种模式。
- Mihomo 辅助进程为每个节点提供隔离 SOCKS 出口，真实请求经过指定 VLESS 线路；支持 TLS+TCP、WebSocket、gRPC 参数。
- CF-Server-Monitor 只读接口适配：`traffic_limit`、`net_rx_monthly`、`net_tx_monthly`、`traffic_calc_type`、`reset_day`、实时上下行速度和 `last_updated`。
- 中文运维控制台、六步首次配置向导、VLESS 批量导入、节点编辑、探针同步和事件日志；支持系统主题与手动浅色/深色切换。

## 本地运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock.txt
python app.py --data-dir ./data
```

本地开发可使用 `python app.py --dns-host 127.0.0.1 --dns-port 15353 --port 18080`。首次启动生成管理令牌，保存到数据目录的 `admin.token`，终端只打印文件位置。浏览器访问面板后在全局设置中填写区域、服务域名、NS、探针 API、Mihomo 绝对路径和探测目标。“高级配置”支持编辑全部 JSON 参数并批量配置节点。

首次导入：`python app.py --data-dir ./data --init-config config.example.json`。已有数据库时此参数不覆盖配置；示例包含占位 IP/UUID，应先按实际情况修改。`--check-config` 校验并输出脱敏的全局配置。生产安装首次使用空节点池，配置前服务域名返回 SERVFAIL。

## 探针对接

在面板中填写探针基础地址，例如 `https://monitor.example.com`。程序只读请求：

```text
GET https://monitor.example.com/api/servers
Authorization: Bearer <可选令牌>
```

每个线路节点需要在面板填写探针 `server_id` 或精确名称。优先使用 ID。带宽上限由你按供应商套餐填写，单位 Mbps。探针数据是整机网卡的统计，如果线路机还有其他业务，不能把它解释成代理专用流量。

完整字段映射、四种计费口径、额度覆盖和第三方探针适配见 [探针对接文档](docs/probe-integration.md)。

`TRAFFIC_AWARE` 会在健康节点池内，将基础权重乘以流量进度、剩余额度相对值、相对带宽容量和当前带宽余量因素。额度达到停用阈值的节点会从流量调度池移除；所有节点都达到阈值时返回 `SERVFAIL`。指标过期或映射缺失时，流量因素退回基础权重；这不会改变 VLESS 健康状态。探针的“GB”按其源码约定使用 `1024^3` 字节，面板显示为 GiB，避免和十进制 GB 混算。

`TRAFFIC_REMAINING`（面板“剩余流量分配”）按健康节点的绝对剩余 GiB 比例分配新的 DNS 查询。例如剩余 720 / 410 GiB 时概率约为 63.7% / 36.3%。带宽、实时速率、周期进度和正数基础权重不参与该比例，基础权重设为 0 仍表示停用。达到额度停用阈值立即剔除；额度缺失、未限额或指标过期的节点按基础权重兜底。带宽配置继续用于面板展示，周期重置仍由探针负责。

`SMART_PEAK_REMAINING`（面板“分时 + 优先级 + 剩余流量”）将时段、手动优先级与上述剩余流量算法组合：

1. 排除禁用、维护、健康不可用、基础权重 0 及有效探针数据显示额度已达停用阈值的节点。
2. 平峰优先 `NORMAL`，高峰优先 `PREMIUM`；首选组存在可用节点时，另一组不参与。首选组全部不可用或耗尽时使用另一组。
3. 组内仅使用数字最小的可用 `priority`。同一优先级有 UP 时优先 UP，其余情况下可使用该级 DEGRADED；时段分组和显式优先级先于延迟偏好。
4. 入选同级节点按绝对剩余 GiB 比例分配。带宽、实时速率及正数基础权重不参与；缺少有效额度的节点沿用基础权重兜底。下一优先级仅在较高优先级全部不可用时接替。

面板按中国大陆时区 `Asia/Shanghai`（北京时间 UTC+8）保存调度时段，无需修改操作系统时区。高峰包含开始、不包含结束；`19:30–00:00` 表示每天 19:30 到次日午夜，`22:00–02:00` 支持跨午夜。修改时间和分组后，新的权威 DNS 查询立即遵循新规则；已有 DNS 缓存和连接仍有各自生命周期。

示例：平峰机器设置 `NORMAL / priority=1`，线路甲 和 线路乙 均设为 `PREMIUM / priority=1`。平峰优先平峰机器，高峰在两台优化机间按剩余流量分配；如果再增加一台 `PREMIUM / priority=2`，仅在高峰一级优化机全部不可用时接替，仍早于跨组兜底。`NORMAL` 表示平峰优先，并非高峰禁用。

组合模式沿用全局 `all_down_policy`：默认全不可用返回 SERVFAIL；显式选择 LAST_KNOWN 时，仅在所设宽限期内尝试最近验证成功且未禁用、未耗尽的线路。本模式作为可选项添加，升级不自动修改现有模式或线路分组。

客户端首次解析只决定新建连接使用的线路。DNS 缓存、Clash/Mihomo 的 DNS 缓存和已建立的长连接会延迟看到新的分配；请将 TTL、Clash DNS 缓存和连接复用时间一起压测后再决定生产值。DNS 分配计数不等于线路字节流量，月末平衡以探针的整机网卡周期数据为准。

## Mihomo 探测

设置 `mihomo_path` 后，程序为每个已配置 UUID 的节点生成隔离配置并运行 Mihomo。探测请求固定经该节点的本地 SOCKS 入口；不会因为某个节点失败而自动选择其他节点。若配置了预期落地出口 IP，会额外调用出口检查 URL并严格比对响应内容。

如果只填写入口 IP/端口而没有 UUID、TLS SNI 或传输参数，节点会显示为探测不可用；入口 TCP 可达不会被误判为 VLESS 业务健康。

VLESS 分享链接包含非 `none` 的 `encryption` 参数时，在节点的 VLESS Encryption 中填写原始参数串，程序会完整传给 Mihomo；ALPN 按链接值以逗号分隔填写。旧配置默认 `encryption=none`、ALPN 使用内核默认。修改加密或 ALPN 会重新进行健康判定；普通权重编辑保留原参数。内核须支持所用加密模式，保存时会调用实际 Mihomo 校验。

探测延迟是 VLESS 路径完成 HTTPS 请求的耗时，包含服务器到线路机及返回落地、TLS 和目标网站延迟，不代表用户到入口的网络延迟。进入 DEGRADED 后，默认要求 EWMA RTT 连续低于最大 RTT 的 80% 才恢复 UP；可在面板设置 recovery_rtt_percent（50%–100%）调整恢复门槛。优先以 TRAFFIC_AWARE 分配容量，用最大 RTT 把持续慢线路标为 DEGRADED；LOWEST_RTT 仅依据此服务器观测值。所有探测目标与线路外控制检查都失败时，观测标为不确定并等待重试；过期节点退出调度。无 Mihomo 路径、UUID 或就绪的 SOCKS 入口时，节点保持 UNKNOWN。

同一个 DNS 服务名返回的各入口，必须接收同一客户端配置：统一入口端口、VLESS UUID、TLS SNI、传输类型及 WS path/Host 或 gRPC service name。节点表允许不同配置便于独立探测，但 DNS A 记录只携带 IP，无法下发端口或协议参数。Realm/Gost 应透传 TLS 到落地机，证书按客户端 SNI 校验。TCP 的 `flow`（例如 `xtls-rprx-vision`）可在高级配置中填写。Clash 中只保留统一域名入口，避免用户继续手动挑旧线路。不同传输组合请使用不同服务实例/域名。

## DNS 委派

权威 DNS 运行后，还需在父区完成 NS 委派。生产建议给程序独立子区，例如 `lb.example.com`，使用 `ns1.lb.example.com`：

1. 在父区把 `lb.example.com` 的 NS 委派到 `ns1.lb.example.com`。
2. 在父区为 `ns1.lb.example.com` 配置 glue/A，指向落地机公网 IPv4。
3. 放通 UDP 和 TCP 53；管理面板不要直接暴露公网。
4. 程序区域填 `lb.example.com`，服务名填 `node.lb.example.com`，NS 与 SOA MNAME 填 `ns1.lb.example.com`；使用 `dig +trace node.lb.example.com A` 和 `dig @落地机IP node.lb.example.com A` 验证。仅在父区添加一个普通 A 记录不构成委派；Cloudflare 等服务的代理加速也需关闭。

单台落地机运行权威 DNS 是单点；正式生产应准备独立的第二权威节点或使用托管 DNS/GTM。低 TTL 只影响新的解析查询，已建立的 VLESS 连接仍需客户端重连。

## Ubuntu/Debian 安装

在项目目录执行（脚本会复制白名单源文件，不会把 `.venv`、Git 历史或数据目录复制到生产）：

```bash
sudo bash install.sh
```

脚本创建 `smartdns` 用户、虚拟环境和 systemd 服务，检查 UDP/TCP 53 绑定。存在冲突时退出并提示 `DNSStubListener=no` 的处理步骤，保留系统 DNS 配置。可用 `sudo DNS_HOST=本机网卡IP bash install.sh` 选择监听地址；NAT 环境填写机器实际拥有的地址。该值写入 systemd 的 `listen.conf`，优先于面板 DNS 监听地址。再次运行脚本即升级：自动停止已有服务、检查端口、安装后启动，保留 `/var/lib/smart-dns-lb` 的配置、令牌和原监听地址。

管理令牌：`sudo cat /var/lib/smart-dns-lb/admin.token`。SSH 隧道：`ssh -L 18080:127.0.0.1:8080 root@服务器IP`，本地打开 `http://127.0.0.1:18080`。对公网提供面板时使用 HTTPS 反向代理。默认仅在页面内存保存令牌；登录时可勾选“在此浏览器记住”存入 localStorage，退出登录或收到 401 时清除。SQLite 和生成的 Mihomo 配置含明文凭证，Linux 数据目录权限 0700、敏感文件 0600，请保护备份。

安装脚本默认从 [Mihomo 官方发行](https://github.com/MetaCubeX/mihomo/releases) 下载匹配 amd64/arm64/armv7 的内核，优先使用 GitHub API 资产的 `digest: sha256:<hex>`，缺少有效 digest 时查找发布校验文件。核对 SHA-256 后原子替换，并自动保存 `mihomo_path`；两类校验来源均缺失、下载失败或摘要不符时中止安装并保留已有内核。amd64 默认使用 compatible 构建。可用 `sudo MIHOMO_VERSION=vX.Y.Z bash install.sh` 固定版本，或 `sudo SKIP_MIHOMO=1 bash install.sh` 跳过下载并保留原路径；程序也会检测 PATH、`/usr/local/bin/mihomo` 和数据目录中的内核。每个节点运行一个隔离 Mihomo 辅助进程，内存占用随节点数增加。

卸载：`sudo bash install.sh uninstall` 删除服务和程序，保留数据；添加 `--purge` 删除数据。卸载保留 `smartdns` 系统用户以便重装。

令牌命令（已安装实例请使用 `/opt/smart-dns-lb/.venv/bin/python /opt/smart-dns-lb/app.py`）：

```bash
python app.py --data-dir ./data --show-token
python app.py --data-dir ./data --reset-token
```

重置后重启运行中的服务使新令牌生效：`sudo systemctl restart smart-dns-lb`。令牌命令直接操作数据目录，即使配置校验失败也可使用。

## Docker

Linux 上执行 `docker compose up -d --build`，使用 host 网络和持久数据卷，默认内置 Mihomo 并校验 SHA-256。执行 `MIHOMO_VERSION=vX.Y.Z docker compose up -d --build` 固定内核版本，省略版本使用 latest；`INSTALL_MIHOMO=0 docker compose up -d --build` 跳过内置下载，也可挂载已有内核。面板默认仅监听宿主机回环地址，宿主机需放通 UDP/TCP 53；端口冲突的处理方式同上。

使用 `docker compose exec smart-dns-lb python app.py --show-token` 查看令牌。容器中检测到 `/usr/local/bin/mihomo` 时自动保存路径。修改构建参数后需重新构建镜像。

依赖版本快照见 `requirements.lock.txt`（本地 Python 3.12 验证）；`requirements.txt` 保留兼容范围。

## 测试与接口

```bash
python -m unittest discover -s tests -v
python -m compileall -q app.py smart_dns tests
node --check templates/app.js   # 可选，仅检查前端 JS 语法
bash -n install.sh
```

API 均使用 `Authorization: Bearer TOKEN`。`GET /api/status` 查看脱敏状态；`PUT /api/config` 保存 `{version,settings,nodes}`；`POST /api/nodes` 新增；`PUT/DELETE /api/nodes/{id}` 编辑/删除；`POST /api/nodes/{id}/probe` 手动探测；`POST /api/probe-source/test` 列出可映射 ID；`POST /api/probe-source/sync` 立即同步。节点变更支持 `If-Match: 配置版本号`，过期编辑返回 409。省略 nodes 保留节点列表，UUID/探针令牌留空保留；编辑节点传 `clear_uuid:true` 清空 UUID，全局传 `clear_probe_token:true` 清空读令牌。

阶段 1 新增接口：

| 接口 | 用法 |
| --- | --- |
| `POST /api/nodes/parse-link` | `{ "links": "vless://…\nvless://…" }`，一次解析 1–64 条，返回 `nodes` 和 `warnings`；域名异步解析为 IPv4，三秒超时，SNI 留空时沿用域名。支持常见 3x-ui/v2rayN 的附加参数，Reality/xhttp 等不支持的协议会提示原因；整批校验，不直接保存节点。 |
| `GET /api/setup/status` | 返回五项配置检查、缺项、建议的 NS/glue 记录、公网 IPv4 探测和 Mihomo 检测结果；公网 IP 探测成功/失败均缓存 10 分钟，`?refresh_ip=1` 强制刷新；探测失败时手填。 |
| `POST /api/setup/check-delegation` | 空对象使用当前配置；也可传 `service_name`、`ns_ipv4` 诊断待保存配置。返回父区权威及两组公共递归的结果和中文建议；单独报告服务 A 记录健康。 |
| `GET /api/config/export` | 默认遮罩 UUID/探针令牌并标记 `redacted`；`?include_secrets=1` 返回完整备份。 |
| `POST /api/config/import` | 必须包含 `settings` 对象和 `nodes` 数组，校验后整体替换；省略的设置恢复默认值，完整备份的空凭据会清空旧值。脱敏备份仅按节点 ID 恢复已有 UUID 和探针令牌，缺少对应密钥时返回 422。 |

通过 `PUT /api/config` 传 `{ "settings": { "service_domain": "node.lb.example.com" } }` 自动推导区域、NS 和 SOA；为了保留主域的原 DNS 管理，填写 `dns.example.com` 或 `dns.example.co.uk` 等会推导主域的名称会报错，请使用独立子区。未传 `ns_ipv4` 时尝试公网探测，失败则保留原值以供手动设置。备份的 `version` 表示来源版本，跨实例导入时不作为目标版本；导入使用 `If-Match` 或 `expected_version` 检查目标版本。导入失败保留旧配置，返回 `restart_required` 时需重启服务使监听参数生效。

面板首次登录会在配置不完整时打开六步向导：服务域名 → 公网 IPv4 → 导入节点 → Mihomo → 可选探针 → DNS 委派。最后一步提供 NS/A-glue 记录表与复制按钮，标明 Cloudflare 的灰云要求，并可直接检查委派。设置页提供基础、调度策略、健康检查、探针、高级 JSON 与备份操作；未保存的设置草稿会保留，避免后台刷新覆盖。所有页面资源均为本地静态文件，无 CDN、远程字体或第三方前端库。

可选浏览器验收：在装有 Node.js 22+ 和 Edge/Chrome 的 Windows 上执行 `node tests/browser_smoke.cjs`，只访问本地 HTTP 测试夹具，截图保存在被忽略的 `.test-data/stage2-browser/`。可用 `BROWSER_PATH` 指定其他 Chromium 浏览器。

本地测试覆盖调度、DNS UDP/TCP、健康状态机、额度及 API；真实 TLS+TCP/WS/gRPC、供应商计费口径、Linux systemd、NS 公网委派和客户端故障恢复仍需按部署环境验收。测试通过不代表这些现场项目已完成。

## 现实边界

DNS 权重描述的是权威 A 查询的抽样比例，不是字节流量的严格比例。递归 DNS 缓存、Clash 缓存和已有长连接都会让调整逐步生效。程序用于降低长期线路使用偏差和提供故障转移；最终切换时间需要用你的 Clash/Mihomo 版本现场测量。

## 稳定性与运行边界

- 状态刷新、VLESS 观测、探针同步和 Mihomo 管理统一使用监督循环。异常只记录任务名和异常类型，避免 URL 或凭据进入日志；事件写库再次失败也会继续退避重试。
- Mihomo 管理与线路探测分开执行，最多同时启动 8 个内核。子进程崩溃后按 2、4、8 秒递增退避，最长 300 秒；稳定运行 60 秒后重置失败计数。正常退出会终止并等待子进程、清理生成的含凭据 YAML 和待启动端口保留句柄。
- 本地 Mihomo 端口使用 `20000–32767`，从选择到配置校验完成保持 socket 保留，创建子进程前释放。Mihomo 不支持接收继承的监听 fd，释放到实际绑定之间仍有很短的外部抢占窗口；启动失败或进程退出会退出就绪状态并重试。Linux 运维可根据机器实际临时端口范围，使用 `net.ipv4.ip_local_reserved_ports` 保留这段专用端口；避免覆盖已有保留列表。
- 有效观测过期阈值取 `max(stale_after_seconds, ceil(启用非维护节点数 / max_parallel_probes) × probe_timeout_seconds + probe_interval_seconds + 1)`。较大节点池会有更长的故障发现窗口，可增加探测并发或降低超时。排队不等于失败；不确定观测仍会随时间过期。
- 改探测目标、出口校验或 Mihomo 路径时，保留尚未过期的健康记录和认证成功时间，直到新观测更新；修改线路连接参数仍清空认证。CLI 与 `SMART_DNS_BIND_HOST` 的监听覆盖仅影响本次运行，保存设置不会把覆盖值写回数据库；覆盖仍生效的字段以重启参数为准。
- DNS TCP 最多 128 条连接、每 IP 最多 32 条（`TCP_PER_IP_LIMIT` 常量），为大型递归的并行连接保留余量；每条总寿命 30 秒、最多 100 次查询，单次读写 3 秒。退出先关闭连接并取消任务，再等待监听服务器关闭。EDNS 最大 UDP 响应为 1232 字节，未知 EDNS 版本返回 BADVERS；CD 位按请求原样返回。
- 写 API 先验证管理令牌，再接收请求体，最后拿配置锁。请求体上限 1 MiB，接收上限 5 秒；超限分别返回中文 413 / 408。未鉴权请求直接返回 401。仅显式标记的探测循环异常、STALE、探针同步失败等噪声事件按内容、级别和节点五分钟限频；状态迁移和配置变更始终记录。事件写库失败时记录错误日志并继续更新内存事件列表，不阻断健康状态和权重刷新；线路探测异常重试等待最长为 probe_interval。SQLite 每 100 次插入按 ID 清理到最近 1000 条，期间最多 1099 条。启动时也清理历史记录；WAL、RLock 和配置版本事务保护并发写入。
- `GET /healthz` 无需令牌，只返回 `{"ok":布尔,"dns":布尔}`。它表示 DNS 是否已启动；不表示公网委派或所有 VLESS 节点已健康。
- UUID、非 `none` 的 VLESS Encryption 和探针令牌均按凭据处理：公开状态与默认备份脱敏，编辑留空保留；显式 `include_secrets=1` 才导出完整备份。脱敏备份导入依靠相同节点 ID 恢复已有凭据，跨实例缺少密钥时报错。Encryption 填 `none` 可明确切回无额外加密模式。
- 新设置默认高峰为 `19:30–00:00`，结束填 `24:00` 会归一化为 `00:00`；开始与结束相同时表示全天高峰，包括 `00:00–24:00`。旧配置的 `23:59` 保留，避免升级静默改变时段。三个明确调度 bug 的前后行为见 [CHANGELOG](../CHANGELOG.md)。
- 观测过期阈值在初始化和 replace_config 时缓存，DNS 查询只做常数时间读取。更换 Mihomo 路径或配置目录时，对原先仍健康且连接参数未变化的节点给予有界启动宽限：`ceil(活动节点数 / 8) × (10 秒校验 + 8 秒启动) + observation_timeout`。出口就绪后宽限缩短至最多一个完整探测周期，实际成功或失败观测立即结束宽限。未就绪重试不续期，已过期节点不会因更换内核恢复健康。终止辅助进程时保留跟踪记录直至 wait 完成；等待被取消后仍能再次 terminate / kill 回收。

### Windows 数据权限与异常终止

Linux 数据目录使用 0700、敏感文件使用 0600。Windows 的 `chmod` 不等价于 NTFS ACL，程序继承数据目录的现有权限；请把 `--data-dir` 放在专用用户的私有目录，在“属性 → 安全 → 高级”中检查继承权限，仅保留运行账号、SYSTEM 和管理员。保护 `admin.token`、SQLite 数据库及 `-wal` / `-shm` 文件、Mihomo 配置和含密钥备份，避免放到共享、同步或多人可写目录。程序不会替用户重写 NTFS ACL。

Windows 正常关闭会回收 Mihomo；任务管理器强制结束父进程、断电等异常终止仍可能遗留子进程。审查第 19 项暂缓：本项目生产路径是 Linux systemd / Docker；Windows Job Object 的嵌套作业兼容和原生 API 生命周期需要独立验证，按进程名批量清理还会误伤其他 Mihomo。Windows 开发请正常退出，重启前核对命令行中的本实例配置目录后再结束残留进程。正式 Windows 后台托管需另行验收 Job Object 或具备进程树回收能力的服务包装器。
