# open-ai 网关 · 关键事实记忆 (MEMORY)

> 本文件随代码打包一并保留。涉及设备标识、签到机制等关键约束。

## Trae 上游 llm_utils_chat 协议变更：模型名必须拆 config_name + model_name（2026-09-15）

**结论**：上游 `llm_utils_chat` 已改协议，模型名分两段传，且**必须带模式变体后缀**，否则一律失败。
本文结论全部由 curl 直连 `https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat` 逐条实测得到。

| 请求形态 | 实测结果 |
|---|---|
| `config_name="glm-5.3-flash"` + `model_name="glm-5.3-flash__dev"` | ✅ 正常出字 |
| `config_name="glm-5.3-flash"`（不给 model_name） | ⚠️ curl 能通；Node 侧实测 **"the model is unknown"** |
| `config_name="glm-5.3-flash__dev"`（整串塞 config_name） | ❌ `code=4001 the param is invalid` |
| `model_name="xxx__max"` | ✅ 仍可用（`__max` 没死，别信"上游只剩 __dev"） |

**关键规则**
1. `config_name` = 基础配置名（`get_detail_param` 返回的 `config_name`，**不带任何后缀**）。
2. `model_name` = 完整变体名（`<config_name>__dev` / `__max`）；**基础名直呼已不被接受**。
   变体语义来自 `get_detail_param` 每项的 `context_window_tokens: {dev, max}`。
3. `messages[].content` 必须是**内容对象数组**（`[{"type":"text","text":"..."}]`）；传字符串报
   `cannot unmarshal string into Go struct field LLMRawMessage.messages.content`。
4. 请求头需三头同 token：`Authorization: Cloud-IDE-JWT <jwt>` + `X-Cloudide-Token` + `X-Ide-Token`，
   外加 `X-Uid`；UA 用 `Trae/<ide_version>`。**纯 HTTP 即可，不再需要 TTNet/Cronet 签名**
   （ahaNet 仍可用，只是非必需）。
5. 无变体名一律自动补 `__dev`；对「该配置没有 dev 变体」的情况保留一次「不带 model_name」的
   回退重试（首字节前），两种情况都能出字。

**代码修复点**
- `trae/server.js`
  - `resolveModelNames()`：剥 `custom-local:`/`tr-`/`trae-` 前缀（循环）→ 动态表精确匹配 →
    配置别名 → 按 `__dev`/`__max` 拆成 `{configName, modelName}`。**查完整别名必须在拆后缀之前**，
    否则 `trae-v4-max` 会被截成 `-v4-max` 这类垃圾名。
  - `traeChat()` / `traeChatOnce()`：拆两层 —— 外层「无变体补 `__dev`」+ 首字节前一次性回退重试；
    内层真正发请求。
  - 三头 + `X-Uid` 补齐；`get_detail_param` 同口径；上游模型列表仍取 `config_name`（基础名）。
  - 上游偶发 Cronet 抖动（`code=3/-21` 网络切换、`code=8/-101` 连接重置）与 429/5xx →
    重试 3 次并轮换账号（仅首字节前，不会重复输出）。
- `providers/trae.py`
  - `normalize_model()`：**先查完整别名表（含带变体名的 key），再剥前缀**；
    `trae-` 前缀是 5 个字符（写成 4 会把 `trae-v4-max` 截成 `-v4-max`）。
  - 别名值可直接写变体名（如 `"deepseek-v4-flash": "DeepSeek-V4-Flash__dev"`）。
- `config.json`（`providers.trae`）
  - `default_model` → `DeepSeek-V4-Flash-Official__dev`；`models` 别名值统一指向 `__dev` 变体。
  - 新增 `deepseek-v4.1-flash` 映射；`function` 保持 `"chat_v3"`
    （实测 `solo_work_lite` 对多数 config 报 param invalid）。

**排查提示**
- `the param is invalid` → 模型名形态不对（多半是整串变体塞进了 config_name）。
- `the model is unknown` → 基础名没带 `__dev`/`__max` 变体。
- `Cronet Error: code=N` → 网络抖动/风控，**不是**模型名问题，等几秒重试即可。

## device_id 由 Trae 客户端生成（核心约束）

**结论**：open-ai 网关对接 Trae 所需的 `device_id`（HTTP 头 `x-device-id`）**必须由 Trae 桌面客户端生成**，网关自身不会、也不能生成合法值。

### 生成机制（逆向确认）
- Trae 桌面客户端（基于 VSCode/Electron）首次启动时，用 `crypto.randomUUID()` 生成一个 **UUID v4**，写入本地文件：
  `C:\Users\<用户>\AppData\Roaming\TRAE SOLO CN\machineid`
- 该值**不是**从系统 MachineGuid 或硬件派生的（与 `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` 不同），是纯粹随机、绑定本机安装的一次性指纹。
- 客户端后续请求（含 `checkin_credits/claim` 签到）通过 `guaranteedDeviceId` 读取该文件作为 `x-device-id`。

### 对 open-ai 代码的影响
- `scripts/signin_all.py` 的 `get_device_id()` 与 `tra  e/server.js` 的 `DEVICE_ID` **都只从 `config.json` 读取** `providers.trae.device_id` / `providers.trae.headers.x-device-id`，**自身没有获取或生成逻辑**。
- config 里原来填的是占位符 `CHANGE_ME_DEVICE_ID` → 所有 claim 请求带假 device_id → Trae 服务端统一返回 **`code:9074`「当前参与用户太多」**（实为风控拦截，非真繁忙）。
- **修复**：手动从客户端 `machineid` 文件提取真实值，写入 config 的两处（`device_id` 字段 + `headers.x-device-id`）。填真实值后 claim 返回 `code:0 success`。

### 注意事项（打包/部署时必读）
1. **device_id 绑定「机器 + 当前 Trae 安装」**：重装 Trae、清空 `AppData\TRAE SOLO CN`、或换机器，`machineid` 会变更，届时需重新从客户端目录提取。
2. 网关节码本身**不自动同步**客户端的 machineid，目前为静态写入 config。若需自愈，可在启动脚本里读取 `%APPDATA%/TRAE SOLO CN/machineid` 并覆盖 config（master 暂未采纳，保持手动）。
3. 服务端对 `device_id` 疑似按「已登记设备」校验：随机 UUID / 占位符均被 9074 拒绝，只有客户端生成并上报过的真值才放行。因此**不可用脚本自行 `randomUUID()` 伪造**——必须复用客户端文件里的真值。
4. 同机多 Trae 账号是否共用同一 device_id 额度（按设备限领 vs 按账号限领）尚未实测，待验证。

## WorkBuddy 国际版（www.workbuddy.ai）对接要点

**结论**：国际版与国内版（`copilot.tencent.com`）**接口路径完全相同**，只有 host 与产品标识不同，
因此 `providers/workbuddy_intl.py` 直接继承国内版 provider，只覆写差异部分。

## 国际版签到（2026-09-13 深扒定论，必读）

**结论：国际版没有签到渠道。** 服务端对国际账号恒报 `active=false`，
官方 web/IDE 此时连签到入口都不渲染（官方 bundle 逻辑：`!V.active → uiState=inactive → shouldShow=false`）。

1. **签到状态接口已切换**：官方只用 `POST /billing/meter/checkin-activity-status`。
   旧 `checkin-status` 是废弃接口、返回死数据（国内账号实测 active=false/streak=0，
   同一时刻新接口 active=true/streak=13/total=1300）。两版脚本/采集器均已切换。
2. **`daily-checkin` 返回 `10001「签到活动未开启或已过期」`是终态**：官方错误映射只有
   `1001=已领 / 1002=无资格 / 1003=活动结束`，其余一律 UnknownBizError；
   active=false 时打 daily-checkin 必 10001，**补领 100% 无效**（旧版每 10 秒补领一次纯刷日志）。
3. **请求头矩阵已穷举**：纯净 / X-Product-Code(workbuddy|workbuddy-ai) / IDE 身份头
   (X-IDE-Type=WorkBuddy 等) / platform 参数 / 跨 host —— `active` 纹丝不动，
   是服务端按账号记的参与状态，客户端伪装改变不了。
4. **旧账号 09-11 17:54 真实成功过一次**（HTTP 200、5s 返回，09-12 02:30 延迟到账 +30 Bonus Pack）
   —— 协议本身没问题，活动 09-12 起对国际账号关闭。
5. **官方前端对 `daily-checkin` 的 10001 按"已领过"处理**（置 claimed + today_checked_in=true），
   所以不要把 10001 当失败重试，也不能当成功记分（积分以资源包入账为准）。

### 连带修掉的 bug（同日）

- **假成功**：`post_json` 超时返回 `(0,"…")` → 旧版兜底 `{'code':0}` 记"签到成功"。
  实锤 09-12 16:22/16:28 两条假成功（耗时 15s=超时整）。现在仅 HTTP 200 + JSON + code==0 才算成功。
- **注册礼包误判**：`_wb_pack_credited_today` 把注册当天的 Free Plan Subscription/Bonus Pack
  当签到入账 → 新账号 09-11 全天被"今日已入账, 跳过"，**第一天 100 分没领**。该预检已退役，
  统一用新接口 `today_checked_in` 判定；采集器资源包兜底加 `_is_registration_pack` 排除。
- **调度**：`app_runtime.tick` 每日签到改为"启动成功才写状态"（旧版先写后启，0 点撞上 wb-only
  在跑会被防重入永久吞掉，实测 09-13 全天 TRAE 没跑、token 过期 401 到 12:54）；
  `--wb-only` 用独立状态键 `.daemon_last_wb_retry`（旧版借用 TRAE_RETRY_STATE 且不写回 →
  每 10 秒拉起一次）；`--trae-only` 现在也做 token 续期；`.trae_signin_state` 值改为
  `done:<YYYY-MM-DD>`（旧裸 done 会压制明天的补试）。
- **桌面端**：账号页"每日签到"列对 WorkBuddy_IE 无记录时显示「—」（无渠道）而非「未签到」。

### 存在感实验：每日 30 积分（2026-09-14 启动，9-15 凌晨出结果）

每日 +30 不是 billing 签到，是 `/activity/growth/buddy`「Buddy 成长计划」活动奖励
（TCACA_code_007，实测 09-14 03:12:12 入账）。验证"纯 API 心跳能否替代客户端"：

- `scripts/growth_presence.py`（daemon）：30s msg-summary + 60s growth/buddy/info + 10min
  get-user-resource + 09:05 幂等 claim。成功路径静默、失败才落日志（日志安静=无故障）。
- **⚠ get-user-resource 必须 POST**（GET 404）——探测/采集脚本都踩过，写死 POST。
- 09-15 00:35 起 `scripts/settlement_probe.py`（只读探测，POST 修正后终演通过）：
  判定 ★成功/★其他包/★到站未领/△已领未入账/✗失败/?网络异常需复核。
- **夜间值守（09-15）**：master 睡前要求 04:00 自动关机（shutdown.exe /s 已排）+
  03:20/03:45 两次自动探测（DSH 后台 job，结果追加 `logs/settlement_verdict.log`）。
  master 醒来读该日志即知结论。
- 判定时刻 ~03:12（批量结算），若今日新 code_007 包出现 → 心跳即活跃成立，
  无客户端方案可行；否则按 MEMORY 下文三情形（设备注册/websocket/真实使用）继续排查。
- **✗ 判决（09-15 03:07/03:30 双探）**：纯 HTTP 轮询心跳（msg 30s + buddy 60s，共 11+ 小时，
  daemon 全程零错误、buddy 恒 null）不足以被判活跃，03:12 无新 code_007 包、无到站消息。
  结论：HTTP 轮询 ≠ 活跃。剩余假设按优先级：① 真实使用行为（客户端 agent 会话，与
  09-13 晚用客户端→09-14 03:12 +30 的时间线吻合，最可能）；② 边缘 websocket 在线
  （channel-sdk 握手 + device-id）；③ buddy 的生成本身就需要使用行为触发。
  下一步方向：要么接受"客户端每日开一次"，要么逆向 websocket 在线协议再试。

### 深挖二期（2026-09-15）：信号定位 → 遥测复刻实验

- **消耗流水真相**：get-user-request-usage 必须 `X-Enterprise-Id=userId`（个人账号也要）；
  按天视图只汇总 credit>0。翻页后共 852 条流水（09-11:12 / 09-12:343 / 09-13:197 / 09-14:242），
  **网关每天几百次成功调用都有记录** —— "网关流量不算活跃"的旧结论成立且更扎实。
- **四天对照（关键）**：09-12(343请求/无客户端)/09-14(242请求/无客户端) 都没拿到 +30；
  唯一拿到 +30 的 09-13 是**客户端在跑**的那天。客户端 17:21-17:52 的调用同样走
  /v2/chat/completions —— 端点无差别，差别在客户端独有的后台信号。
- **客户端独有信号**（logs/2026-09-13 ProxyResolver 全量 URL）：`/v2/report` 113次(每~15s)、
  get-dosage-notify 16次、/v3/config 9次、Centrifugo WS 长连接。
- **/v2/report 格式已逆向并打通**（StandardEventService，packages/telemetry）：扁平结构
  `[{eventCode,timestamp,reportDelay,...事件字段}]`，事件码=Events 枚举（page_load/page_show/
  chat_message_send/user_auth_action...），Bearer token 鉴权，实测 HTTP 200 code=0。
  ⚠ 嵌套 {commonFields,payload} 会 400。
- **实验三**：`scripts/wb_report_heartbeat.py`（09-15 10:45 常驻）复刻客户端后台信号组合：
  每20s /v2/report page_show 轮换 + 120s get-dosage-notify + 60s buddy/msg 轮询 + 10min
  资源包监视 + 09:05 幂等 claim。判决：**09-16 03:12** 是否出现新 code_007 +30 包。
- 若成立 → 网关侧加遥测心跳即可无客户端领每日 30 分；若不成立 → 下一嫌疑是
  Centrifugo WS 长连接（cf-connect/websocket，centrifuge-js 协议，asar 内有 CentrifugoClient）。

### 国际版协议差异（旧表，仍适用）

### 协议差异（实测确认）
| 项 | 国内版 | 国际版 |
|---|---|---|
| host | `copilot.tencent.com` | `www.workbuddy.ai` |
| 产品标识 | `X-Product: SaaS` | `X-Product` + **`X-Product-Code: workbuddy-ai`** |
| 首条消息 | 无要求 | **必须 `system`**，否则 400 `code=11128` |
| 模型目录 | `GET /v3/config` | `GET /v2/enterprises/personal/models` |
| token 签发 | 腾讯侧 | **Keycloak**（realm=copilot，azp=console，`typ=Offline` 的 refreshToken，约 1 年有效） |

### 踩坑记录
1. **`wbie-`（及历史 `wbai-`）前缀必须在 `wb-` 之前路由**：`route_provider` 里
   `wbie-xxx` / `wbai-xxx` 不能被国内版 `wb-` 规则截走。
   国际版对外前缀由 `workbuddy_intl.INTL_PREFIX` 单点控制（v2.5 起为 `wbie-`）。
2. **基类 `_to_upstream_body` 不认 `wbie-` / `wbai-` 前缀**：它用的是国内版 `_strip_prefix`（只剥
   `workbuddy-` / `custom-local:`）。若不额外归一化，`wbie-auto` 这类别名会被**原样发给上游**，
   返回 `400 code=11102 model [wbie-auto] service info not found`。
   → `WorkBuddyIntlProvider._to_body` 先过 `self.normalize_model`，并补齐两种前缀的别名键。
3. **叠加前缀要循环剥离**：`custom-local:wbie-xxx` 单遍循环只能剥掉一个前缀。
4. **国际版模型列表不能混入国内版模型**：基类 `_model_ids()` 会把 `MODEL_ALIASES`（国内版）
   的值也算进去，故国际版完全重写 `_model_ids()`。
5. 国际版计费接口（`get-user-resource` / `daily-checkin`）
   同样只差 host + `X-Product-Code`，已参数化复用。
   （签到状态接口差异见上节：官方已改用 `checkin-activity-status`。）

### 国际版登录链路（**以实测抓包为准**）

**结论：国际版换 token 就是 `POST /console/login/enterprise` →
返回 `{accessToken, refreshToken}`，与国内版脚本写法一致。**

**浏览器方案 v2.6 更换（登录失败根治）**：旧版 `p.chromium.launch(channel='msedge')`
拉起的系统 Edge 带自动化特征，走 X(Twitter) OAuth 登录时被 X 的反机器人挑战
（`onboarding/web#/s/knowledge_check`）卡成死循环，始终回不到 workbuddy.ai、
抓不到 token（`logs/login_WorkBuddy_IE.log` 多次复现）。现改为：
1. 默认 **Playwright 自带 Chromium**（版本与 playwright 包锁死，最稳）；
2. 反检测：去 `--enable-automation` + `--disable-blink-features=AutomationControlled`
   + init script 隐藏 `navigator.webdriver`（实测注入后 evaluate 返回 None）；
3. `launch_persistent_context` + **每次登录全新 profile**
   `data/pw_profiles/wbie/run-<时间戳>/`（Chromium 系与 Firefox profile 互不
   兼容，后者在 `.../firefox/` 子根）。⚠️ 固定 profile 是错误设计：上一次的
   SSO 登录态常驻会让登录页**自动顶号**，同一通道没法添加第二个账号
   （master 实测反馈后改掉）——现在默认全新干净会话，多账号互不干扰；
   上次没走完的登录用 `--reuse-profile` 复用最近一次；旧会话目录自动清理
   （只留「本次 + 最近一次」，同秒并发用 uuid 后缀防撞）；旧固定 profile
   （`data/pw_profile_*`）启动时顺手删除。
启动失败自动降级 chromium → chrome → msedge → firefox；`--browser` 可指定。
**v2.6 起 `login_workbuddy.py`（国内版, `data/pw_profiles/wb/`）与
`login_trae.py`（`data/pw_profiles/trae/`）已套用同一方案**，三个登录
脚本各自内嵌同款 `launch_browser()` + `_pick_profile_dir()`（不抽公共模块：
打包态按文件名路由到内嵌模块，新 helper 文件不会进 exe，自包含才安全）。
本装是**源码形态**（有 `.venv`、无 open-ai-*.exe），GUI 改源码即生效。

**TRAE 登录滑块验证报「网络环境较差 [5014][502]」（v2.6.2 排查结论）**：
不是断网——是字节风控对「设备指纹 + 出口 IP」的综合打分拒绝（滑块验证属
字节风控，该话术是其固定文案）。排查事实：出口 IP 112.49.6.101（中国移动，
ip-api 判定非代理非机房）干净；trae.cn 页面在 chromium/msedge 下加载均正常
（无头对照实验）；此前固定 profile 时代 17:0x 连续 3 次自动登录（见
login_Trae.log）抬高了该设备的风控评分。v2.6.2 对策：
① 三个登录脚本浏览器一律**原生 UA**——旧版让自带 Chromium 伪造 `Edg/143`
  UA，与 client-hints 品牌头（真实是 Chromium）自相矛盾，正是风控可疑点；
② 伪装脚本最小化：只藏 `navigator.webdriver`（半吊子伪造反而制造新不一致）；
③ 复用持久化 context 自带的空白初始页（不再多开一个 about:blank 标签）；
④ 三个脚本都挂 `requestfailed` 记录，风控端点被拒时日志有据可查；
⑤ trae 换 token 的 HTTP 调用改用浏览器实际 UA（与产 cookie 的会话一致）；
⑥ wb 国内版顺带补齐 goto 三次重试 + 手动关窗优雅退出（原来直接崩堆栈）。
仍被 [5014] 拒时：改用扫码登录绕开短信验证 / 换手机热点 / 等 30-60 分钟让
风控降权。

实测证据（`logs/login_workbuddy_intl.log`，2026-09-11 22:07:15 成功那次）：
```
[命中] /console/login/enterprise HTTP 200
[网络捕获] accessToken 来自 net:/console/login/enterprise
[成功] 获取 accessToken: userId=94211e45-... len=1302
[校验] ✓ token 可用 (18 个模型可见)
```

完整链路（同一次抓包的日志顺序）：
1. 打开 `/login/?platform=workbuddy&state=<uuid>` —— React SPA
   （`download.codebuddy.ai/web/login/<hash>/assets/`，CN 与 INTL **共用同一套 SPA**）
2. 跳 Keycloak OIDC：
   `/auth/realms/copilot/protocol/openid-connect/auth?client_id=console&response_type=code&redirect_uri=<origin>/login/select?...&product=workbuddy`
3. **支持第三方 IdP 登录**：本次实测走的是 **GitHub OAuth + 2FA**
   （日志里可见 `https://github.com/login?client_id=...` → `https://github.com/sessions/two-factor/app`）
4. 回到 SPA 后 `POST /console/login/enterprise` → 拿到 accessToken/refreshToken

⚠️ **`/v2/plugin/auth/token` 不是换 token 接口**：它是**登录状态轮询**接口
（`GET` 返回 `{"code":11217,"msg":"11217:login ing..."}`，`POST` 返回 `404 page not found`）。
用它换 token 是错的 —— 日志里从未从该端点抓到过 token（出现 0 次）。

#### 踩坑记录

1. **【已纠正的错误结论】曾误判"`/console/login/enterprise` 在国际版不存在"。**
   错因：只下载到登录 SPA 的 **17/27 个 JS chunk**（下载脚本被 60s 超时打断），
   在不完整的集合里 grep 不到 `login/enterprise`，就把「搜不到」当成了「不存在」，
   又拿 `/v2/plugin/auth/token`（其实是状态轮询接口）去"交叉验证"，越走越偏。
   **教训：验证接口是否存在，只能靠真实抓包/实测，不能靠对 JS 的否定式搜索。**
2. 「401 不能证明路径存在」这条是对的（`/console/definitely-not-exist-xyz` 也是 401），
   但当时没想到**它同样不能证明路径不存在** —— 这个判据是双向失效的，本就该弃用。
3. **登录页会另开窗口 / 在 iframe 里跳转**，所以网络监听要挂在 **context 级**
   （`ctx.on('response')` + `ctx.on('page')`），只挂 `page.on` 会漏响应。
4. **别只监听单一 URL**。正确做法是「通用兜底抓取」：任意响应的 JSON（递归）里
   出现 JWT 值就抓，按 `typ`（Bearer/Offline）区分访问/刷新令牌，再按来源打分排序
   （token 端点 > 其它接口 > localStorage/cookies 兜底）。接口一变仍能抓到。
5. 用户手动关浏览器会抛 `TargetClosedError`：必须在等待循环里捕获，
   并**每 5s 从 localStorage/sessionStorage/cookies 兜底扫一次**，否则关浏览器就丢 token。
6. 抓到的令牌**写入前先用只读接口验证**（`/v2/enterprises/personal/models`），
   被 401/403 拒就放弃写入，避免往 config 里塞废账号。
7. **登录失败最可能的原因是网络抖动，不是代码**：实测过 TCP 连接就耗 **19.16 秒**后超时
   （HTTP 000），同一分钟另两次只要 0.2~1.5 秒。网络一抖，iframe 里的 OIDC 或最后的
   换 token 就超时，SPA 走不到最后一步 → 脚本永远等不到 token → 用户以为没反应就关浏览器。
   脚本已对 `goto` 做 3 次重试 + 60s 超时。**网络不稳时优先重试，别急着改代码。**


## WorkBuddy 系积分流水采集与前端接口

### 国际版流水/签到接口 = 国内版路径 + 换 host（已实测确认）

| 用途 | 国内版 | 国际版 |
|---|---|---|
| 逐笔消耗 | `POST copilot.tencent.com/billing/meter/get-user-request-usage` | 同路径，host 换 `www.workbuddy.ai` |
| 按天汇总 | `.../get-user-daily-usage` | 同上 |
| 签到状态 | `.../checkin-status` | 同上 |
| 积分资源包 | `.../get-user-resource` | 同上 |

差异只有两点：**host** 与 **`X-Product-Code: workbuddy-ai`**。
其余（请求体、响应结构、`X-Enterprise-Id` 的必要性）与国内版完全一致。

两个容易踩的坑（与国内版同源）：
1. `get-user-request-usage` **必须带 `X-Enterprise-Id`**（个人账号传自己的 userId），否则 400。
2. `get-user-resource` **恰恰不能带** `X-Enterprise-Id` —— 带了会切到企业视角，
   个人资源包被隐藏（返回 0 包），签到入账就统计不到。

实测样本（2026-09-11，国际版）：
```
POST https://www.workbuddy.ai/billing/meter/get-user-request-usage
{"code":0,"msg":"OK","data":{"total":12,"data":[
  {"requestId":"930f10a2...","credit":0,"model":"deepseek-v4.1-flash",
   "client":"WorkBuddy","requestTime":"2026-09-11 23:55:35", ...}]}}
```

### 平台枚举与扩展方式

`usage.platform` / `gain.platform` 取值：`trae` / `workbuddy` / `workbuddy_intl`。

**加 WorkBuddy 系新区域时的改动点（两处，都只加一条）**：
1. `scripts/usage_collector.py` → `WB_SPECS` 加一条规格
   （platform / provider / base / label / short / default_domain / default_product）。
   采集、入库、`collect_all_workbuddy`、`collect_gains`、CLI `--platform` 全自动覆盖。
2. `scripts/credits_api.py` → `PLATFORMS` 加一条（给前端中文名与配色）。

### 前端接口约定（重做 GUI 必读）

**不要再让 GUI 自己写 SQL 查 `data/usage_history.db`** —— 统一走
`scripts/credits_api.py`。原因：老 GUI 里到处是
`{'trae': 0.0, 'workbuddy': 0.0}` 这类两平台硬编码，加国际版时必漏
（本次已修一处：`--platform` 过滤只收窄了日序列、没收窄合计）。

约定要点：
- 返回纯 dict/list/基本类型，可直接 `json.dumps`。
- 平台中文名走 `platforms()`（`key`/`label`/`short`/`color`），前端别硬编码。
- 日期 `'YYYY-MM-DD'` 闭区间，本地时区。
- 库不存在返回空结果而非抛异常。
- `overview()` 是仪表盘首选入口；传 `platform=` 时数值全收窄，但 `platforms`/`accounts`
  仍返回全集（下拉与图例要稳定）。

## 桌面端（v3.0.1）四个易踩的约束

### 1. 「每日签到」= 今日是否签到成功（只读），不是开关

- 证据源：`data/usage_history.db` 的 **gain 表** —— 当日有入账记录即签到成功。
  TRAE 写 `kind='checkin'`；WorkBuddy 系写当日入账的资源包。
  **不要改用上游的 `checkin-status`**：WB 侧 `active=false` 的账号恒报未签，
  与实际入账脱节（`usage_collector.collect_gains` 注释里有实测结论）。
- 接口：`GET /v1/admin/accounts/signin`（只读、不联网），key 与 `/accounts` 的
  account id 同构（`通道:uid`）。
- **缺陷已修**：该列曾渲染成 Checkbox 且初值取 `a.enabled` —— 把「账号是否启用」
  当成「今日是否签到」，勾选既不落库也不触发签到。别再改回交互控件。
- **接口不存在时必须显示「—」而不是「未签到」**：签到表里「没这个账号」在当前实现里
  等于「未签到」，不区分会把「网关没重启」渲染成「今天所有账号都没签」。
- 采集器每 5 分钟跑一次，故刚签到完最多滞后 5 分钟才显示「已签到」。

### 2. 模型列表有本地缓存，筛选是本地做的

- `desktop-ui/src/lib/modelCache.ts`（键 `open-ai.models-cache.v1`）：缓存
  **三通道全量**列表（含隐藏项 + 已叠加视图偏好），页面首帧同步读缓存直接渲染。
- `httpBackend.listModels` **不再带 channel 参数**，一律拉全量、本地 `filterModelView`。
  切通道/显隐不再发请求 —— 改回「按 channel 请求」会让切筛选重新变慢。
- 缓存由**页面**在 `status === 'success'` 时统一写入。别在数据层也写一份：
  「首帧用缓存」会把缓存原样回写并刷新 `savedAt`，等于用旧数据冒充新数据。

### 3. 主题：默认白天，`.dark` 必须写在 `:root` 之后

- 三种模式 `light`(默认) / `dark` / `system`，存 `localStorage['open-ai.theme']`，
  由 `<html class="dark">` 切换。`:root`(浅色) 与 `.dark`(深色) **特异性相同**，
  顺序即优先级。
- **`index.html` 里有内联防闪脚本**，与 `src/lib/theme.ts` 是同一份约定的两个副本，
  改一处必须同步另一处（否则会闪一帧默认主题）。
- **`src-tauri/tauri.conf.json` 不能写 `"theme": "Dark"`**：它会强制 WebView2 报告
  `prefers-color-scheme: dark`，「跟随系统」将永远只能得到深色。
- 图表色**不在 CSS 变量里**（recharts 的 `fill` 要可直接解析的颜色），
  `config/chart.ts` 提供深浅两套色板。深色那套的亮绿/亮黄在白底上只有 2:1，不能复用。

### 4. 「操作日志」页 = 用户操作，不是网关运行日志

- v3.0 初版这一页读 `logs/gateway_out.log`（网关自己在说什么），已按 v2.3 语义改回
  **用户在界面上做了什么**（`desktop-ui/src/lib/oplog.ts`，落 `localStorage`）。
- 埋点是数据源外层的一个 Proxy（`lib/oplogBackend.ts`）：**新增写操作必须**在
  `MUTATIONS` 里登记，否则用户的操作不留痕（DEV 下有控制台告警）。
- 因为包了 Proxy，`dataSource.ts` 里判定演示模式已改为比较未包装的 `rawSource`。
- 网关自身的输出仍在 `logs/`：界面上的「日志目录」按钮、托盘菜单都可打开。

### 5. 登录脚本输出跟随（v2.3 `_subprocess_stream` 的 v3.0 等价物）

- 后端 `GET /v1/admin/accounts/login/log?channel=&offset=` 增量读
  `logs/login_<通道>.log`；`POST /accounts/login` 返回 **`logOffset`**（启动时的文件长度）。
- **`logOffset` 不能省**：该文件是**追加**写的，历次登录输出都在里面；不带起始偏移
  会把上一次登录的旧输出当成这一次的往下贴。
- **`running` 靠 Popen 句柄**（`admin_api._LOGIN_PROCS`）判断，**不要改成猜 mtime** ——
  用户把浏览器晾十分钟时 mtime 推断必然误判。网关重启丢句柄是已知且可接受的降级。
- `addAccount` **等脚本结束才 resolve**，这样操作日志里「一次添加账号」是**一个**
  完整操作块（`[完成]` 落在脚本输出之后）。因此**调用方不要 await 它** ——
  登录可能几分钟，界面会锁住。结束后的刷新走 `lib/accountEvents` 通知。
- 账号列表页的添加按钮在跟随期间进入「登录中…」并禁用：连点会顶掉网关侧的 Popen 句柄。
- 级别着色判据**不**单凭「完成」判成功（「等待用户在浏览器中完成登录」会被误染绿）。

## Loomy（讯飞）通道（2026-09-13 集成；2026-09-14 鉴权整改）

**结论**：Loomy 是 Electron 套壳 opencode，真实网关 `https://loomyad.xunfei.cn/api/v1`
（OpenAI 兼容）。**鉴权（2026-09-14 整改，必读）**：`Authorization: Bearer <本账号session>`
+ `token: <本账号session>` 双头，与官方客户端 `useSessionAuth: true` 一致 ——
**一人一号、积分各扣各的**。早期版本曾把反编译得到的 iModel apiKey
（`98950ac6...`）当共享凭据硬编码，实测该 key 恒定解析到**同一个陌生账号**
（userid `260913114245735244`, `182****4793`），所有人的积分都记在别人头上；
已全库清除（providers/loomy.py、config.json、loomy_client.py、usage_collector.py），
详见桌面《LOOMY_鉴权整改文档.md》。整改要点：
- **只有桌面通道（讯飞账号服务）的 `session` 能鉴权对话**；Web 通道
  （loomy.xunfei.cn）的 cookie 仅能访问 `/web/api/*`，打 chat 回
  HTTP 200 + 业务码 `100002`（带内错误）—— 流式场景必须先探测首个事件再提交，
  否则会把错误 body 当正常流消费（providers/loomy.py `_probe_first` 已处理）。
- 多账号轮询 + 失效自动切换（401/403 或 100002 换下一个）；无账号时明确报
  `loomy 无可用账号`，**不再回退共享 key**。
- 台账/积分查询：桌面账号用 session 打 `/api/v1/points/records`
  （余额字段 **`balance`**=永久钱包 / `dailyBalance`=今日额度 /
  **`availableBalance`**=总可用，⚠️ 与 first-login 接口的
  `permanentBalance` 不同名，认错字段会把永久钱包丢一半，踩过）；
  Web 账号用 cookies 打 `/web/api/auth/points-summary`
  （字段 `permanent`/`daily`）。两套形状不同。
  `usage_collector` 逐账号采集，uid = 账号 userid（不再混池）。
- 上游对**非流式**请求回完整 `chat.completion`（字段 `message`），
  流式回 `chunk`（字段 `delta`）——`_aggregate` 两种都要吃；现在内部
  统一强制 `stream:true` + `stream_options.include_usage` 上游请求。

1. **模型名 `lm-<上游id>`**（v3.1 改名，旧前缀 `loomy-` 仍兼容请求）：
   `route_provider` 判 `m.startswith("lm-") or "loomy" in m`，
   **必须放在其它前缀判断之前**（在 `providers/__init__.py` 的最前面）。
   **模型目录已动态化**：`LoomyProvider.sync_models()` 走上游
   `GET /models`（**token 头**鉴权），TTL 4h，
   已挂进 main.py 的每日刷新循环；失败回退静态表；**无账号时跳过**
   （整改后无共享 key 可用）。admin_api 的 Loomy 倍率是静态表（lm-/loomy-/裸名三种键都登记）。
2. **虚拟模型「Auto路由连」**（`auto_router.py`）：`model == "Auto路由连"`
   （历史别名 `Auto-mode` 仍兼容）时按 `config.json` 的 `auto_chain.models`
   顺序故障转移；与上游内置 `auto` 别名严格区分（is_auto_model 只做全等匹配）。
   流式有**空 chunk 缓冲**：未产出真实内容前不提交，失败可静默切换（勿删该逻辑）。
3. **Loomy 已接入流水库（2026-09-13 晚），积分看板四通道齐全**：
   - 消耗流水：`usage_collector.collect_loomy` 走 loomyad 台账
     `GET /api/v1/points/records`，只取 `direction=debit`（模型调用扣分）。
     **整改后逐账号用自己的 session 采集**（`loomy_client.ledger_all_accounts`，
     只遍历有 `session` 的桌面账号），uid = 账号 userid，ledgerId 作 entry_id 幂等。
   - 获取凭证：`loomy_gains` 两个来源 —— 台账 credit 方向（邀请/任务奖励，
     kind=description）+ 签到状态缓存 `data/loomy_signin_state.json`
     （kind=daily-login，每日登录发放不进台账）。
   - `credits_api.PLATFORMS` 有 'loomy' 键（紫色 #A78BFA）；
     `admin_api.CHANNEL_TO_PLATFORM` 已补 "Loomy"。
   - 每日领取由 `signin_all.loomy_daily_one` 执行并写签到状态缓存
     （Web 账号 = web_me 会话校验 + points-summary，桌面账号 = first-login；
     Web 端**没有**显式领取端点，积分随当日登录由服务端自动发放）。
     `GET /accounts/signin` 读缓存，账号页「每日签到」列正常渲染。
     **白天补签（2026-09-15 补）**：Loomy 原本只在 00:00 全量签到里跑，
     当天新登录的账号要干等到次日零点（实测 master 中午添加的新账号当天
     领不到）—— 已挂进 daemon 的 `--wb-only` 30 分钟补签循环：
     只补 `claimed != True` 的账号，领取幂等（已领过返回 alreadyProcessed）。
4. **Loomy 登录 = 手机号短信验证码**（HMAC-SHA1 ak/sk 签名，逆向自 Loomy 桌面端），
   **不是浏览器交互**：桌面端「添加 Loomy 账号」弹 `LoomyLoginDialog`
   图形化向导（默认走**桌面通道** `desktop-send-code`/`desktop-login`，
   因为只有它的 session 能鉴权对话），全程无命令行。
5. **图表紫色**：`config/chart.ts` 深色 `#A78BFA` / 浅色 `#7C3AED`（亮紫在白底
   只有 2.3:1，必须压暗）；CSS 令牌 `--channel-loomy` 同步双主题两套值。

### Loomy Web 端（loomy.xunfei.cn，2026-09-13 逆向，必读）

**结论：Web 端登录纯 API 即可完成（手机号+短信码），无需浏览器/Playwright。
凭据是 Cookie 会话，已实装在 `scripts/loomy_client.py` 的 `web-*` 系列命令。**

- **API 前缀**：`https://loomy.xunfei.cn/web/api/*`（SPA base path = `/web`，
  页面根 `https://loomy.xunfei.cn/web/`，`/web/login` 会 404，hash 路由 `/#/login`）。
- **登录流**（逆向自 bundle `index-p6xFTG72.js`）：
  1. `POST /web/api/auth/sms-code` body `{"phone"}` → 响应顶层 `messageId`
  2. `POST /web/api/auth/login` body `{phone, code, messageId, channel, visitedId, deviceId}`
     → `Set-Cookie` 下发会话（channel/visitedId 传空串即可）
  3. `GET /web/api/auth/me` 校验（401 `UNAUTHENTICATED` = 会话失效）
- **鉴权形态**：纯 Cookie（前端 fetch 恒 `credentials: "same-origin"`，无 token 头、
  无 CSRF、无签名）——与 loomyad.xunfei.cn 的 Bearer/token 双头体系**相互独立**。
- **deviceId**：本地指纹 `web_fp_` + FNV-1a 32bit（bundle 的 `Dw()`），
  可伪造；`web_fingerprint(seed=手机号)` 保证同账号恒定，避免设备风控。
- **金矿端点**（cookie 鉴权）：`/web/api/models`（模型目录含倍率/上下文长度）、
  `/web/api/team/points/balance`、`/web/api/auth/points-summary`。
  Web 对话走高层协议 `POST /web/api/chat/completions`
  （body 是 `{conversationId, messageId, model, content}`，由 Web 后端托管会话），
  **不是** OpenAI 原生格式——网关的 chat 仍走 loomyad Bearer 通道。
- 响应统一外壳 `{"success":bool, "code", "error"|"data"...}`；错误码如
  `INVALID_PHONE` / `UNAUTHENTICATED`。
- CLI：`web-send-code` / `web-login` / `web-me` / `web-models` / `web-points`；
  Web 会话存 `providers.loomy.accounts[]`（`kind:"web"`, `userid:"web:<phone>"`,
  `cookies:{...}`），与桌面通道账号（有 `session` 字段）同列共存。
- **图形化登录（面向小白，2026-09-13 实装）**：账号页「添加 Loomy 账号」按钮
  → `LoomyLoginDialog` 弹窗（手机号 → 60s 倒计时验证码 → 完成），后端端点
  `POST /v1/admin/accounts/loomy/send-code` / `web-login`、
  `GET /accounts/loomy/session?phone=`。用户全程不接触命令行；
  Loomy 行的右键菜单**没有**「重新添加该账号」（无浏览器脚本可重放，
  走向导重新登录即可）。
- **账号行合并（2026-09-14，master 指定）**：同一手机号的桌面+Web 两条账号
  在账号管理页只显示**一行**（`Loomy(手机号)`，桌面账号出 id/名字），
  「Loomy Web(...)」不再单独出现。实现要点：
  - `admin_api._loomy_groups` 按手机号分组，`_load_accounts_raw` /
    `toggle_account` / `delete_account` / `accounts_signin` /
    `refresh_account_credits` 全部走合并语义（toggle/delete 同时作用两条；
    删一半会留孤儿行，必须成对）。
  - **积分/签到的数据源走 Web 通道**（master 指定）：余额优先
    `/web/api/auth/points-summary`（桌面 `availableBalance` 兜底），
    签到凭证优先 Web 缓存条目（桌面兜底）；缓存键统一挂合并行 id。
  - `usage_collector.loomy_gains` 的 daily-login **按手机号归组**：
    uid 一律取桌面 userid（与消耗归属一致），**任一半边**有领取凭证就记一行、
    金额取各凭证 dailyBalance 最大值 —— 绝不写两行；⚠️ 也不能「有桌面账号就
    跳过 Web 凭证」：定时签到跑得早，凭证可能只在 Web 半边（桌面账号当天
    下午才登录），整组跳过会让看板丢当日获取（2026-09-14 踩过，表现为
    「签到分没进积分看板」）。`credits_api.accounts()` 的 loomy 列表也只出
    桌面半边（uid 对齐），gain 表里的 `web:%` 旧行已清理。
  - `providers/loomy.load_config_accounts` 把纯 cookie 账号排除出对话轮询
    （cookie 打 chat 恒 100002，轮到它只会白打一发再切换）。
  - 前端 Loomy 行右键「重新添加该账号」恢复显示（= 重新走向导登录，
    覆盖同手机号两条记录）。
- **「断连」排查实录（同日）**：Web 账号登录成功却显示断连 ——
  `_account_status` 漏了 `cookies` 字段（只认 session/token/accessToken）。
  **Loomy Web 账号凭据字段是 `cookies`（dict），不是 `session`**，判定与
  积分刷新（`POST /accounts/credits/refresh` 对 cookies 账号走
  `/web/api/auth/points-summary`，积分 = permanent + daily）都要认这个字段。
  改完必须 `bootstrap.py restart` —— 网关 shim 直接跑源码 main.py，
  改 .py 重启即生效，无需重新打包。

## 已删除的旧组件（别再去调用它们）

`launcher_main.py` 及其产物 `open-ai-launcher.exe` + `launcher_internal/` 已于清理中**整体删除**
（master 确认不再需要，安装包链路后续重建）。删除前实测确认本机启动链无一处引用：

| 入口 | 实际指向 |
|---|---|
| 桌面快捷方式 `open-ai.lnk` | `desktop\open-ai-desktop.exe`（Tauri 桌面端） |
| 开机自启 | `open-ai-autostart.vbs` → `open-ai-autostart.bat` → `start_hidden.ps1` |
| 生命周期控制 | `runtime\Scripts\open-ai.exe bootstrap.py start/stop/restart/status` |

**注意**：那份 exe 打包于 09-02，而 `launcher_main.py` 09-12 才修（加 `_find_desktop()`），
所以它内部仍是「写死拉起已删除的 tkinter GUI」的旧代码 —— 即便留着也是坏的。日后若要重建
「一键启动」入口，只需三步：建 runtime（`procname.py`）→ `bootstrap.py start` → 拉起桌面端。

**保留**：`uninstall.exe` + `uninstall_internal/`（设置页「一键卸载」与 Rust `uninstall_app` 仍在调用）。

## 关联文件
- 配置：`config.json` → `providers.trae.device_id` / `providers.trae.headers.x-device-id`
- 签到：`scripts/signin_all.py`（`trae_checkin_one`）、`trae/server.js`
- 实测日志：`logs/signin.log`（TRAE 段）、`logs/daemon.log`
- 国际版：`providers/workbuddy_intl.py`、`scripts/login_workbuddy_intl.py`、
  `config.json` → `providers.workbuddy_intl`
- 国际版登录排障日志：`logs/login_workbuddy_intl.log`（每次登录的全量网络流水）
- 流水采集：`scripts/usage_collector.py`（`WB_SPECS`）→ `data/usage_history.db`
- 前端接口：`scripts/credits_api.py`（只读查询 API）
- 签到状态查询：`scripts/credits_api.py` → `gains_accounts(day)`（"今日谁签到了"的唯一真源）
- 桌面端外观/日志自检：`desktop-ui/tools/verify-appearance.mjs`（27 条断言）
- 国际版测试：`tests/test_workbuddy_intl.py`、`tests/test_gui_layout.py`、
  `tests/test_usage_collector.py`、`tests/test_credits_api.py`
</content>


## 演示 mock 不得携带主机同款数据（2026-09-13 事故）

**结论**：`desktop-ui/src/lib/backend.ts` 是演示数据源，它的种子账号/模型/流水会原样
打进前端 bundle → 桌面端 exe → 安装包。v3.0-portable 首版把 Loomy 通道按主机实时
账号形态写进了 mock（`Loomy(13800000000)`、lm-* 模型表、流水种子），发布前被 master
拦下 —— 真值闸门（sanitize_check / check_desktop_bundle）抓不住这类泄漏，因为 mock
里没有已知真值，只有"主机同款形态"。

规则：
1. mock 账号一律用「示例账号(…)/随机数」命名，**禁止**复刻主机真实账号名格式与数值
   （通道数据一律由真实网关提供，演示包不造 Loomy 数据，相关种子恒为空/0）。
2. 每次给 mock 加新通道数据时，先问一句：这段数据是不是照着主机运行态抄的？是就不进库。

## 全新安装不得展示未登录通道的模型目录（2026-09-14 VM 事故）

**现象**：用户版安装包装进全新虚拟机，未登录任何 Loomy 账号，`/v1/models` 却列出
12 个 `lm-*` 模型 —— `build_providers()` 对 loomy 用 `enabled 缺省 True` 无条件注册，
`LoomyProvider.list_models()` 随即回落到硬编码 `DEFAULT_MODELS`（自 Loomy 客户端
opencode 配置提取的静态目录）。workbuddy/intl 都要求「有 token/账号才注册」，loomy 是唯一例外。

**修复**：`providers/__init__.py` 里 loomy 与 workbuddy 同规则 —— 有 `accounts`/`session` 才注册；
登录链路走 `admin_api._loomy_client()` 不依赖 provider 实例，登录写入账号后
`/v1/admin/reload-providers` 热重建，模型目录由上游实拉，静态目录只剩断网兜底作用。

## scripts 的 data 路径必须钉死安装根（2026-09-14 VM 事故 · 积分流水的真因）

**现象**：VM 全新安装后积分看板出现「9/14 凌晨」的真实流水, 而安装目录里根本没有
`data/usage_history.db`。真身藏在 `C:\Users\<user>\AppData\Local\Temp\data\usage_history.db`。

**根因**：`credits_api.py` / `usage_collector.py` / `loomy_client.py` 用
`BASE = dirname(abspath(__file__))` 定位 data/ 与 config.json —— **打包态里 PyInstaller
给模块的 `__file__` 是相对路径, `abspath()` 跟着进程 CWD 走**。凌晨那次使用把真实流水
写进了 `%TEMP%\data\`; 重装后网关 CWD 依旧指向 %TEMP%, 于是把陈旧库当现役库读,
"全新安装" 就 displays 昨天的账号流水 (uid `98950ac6…` = master 自己的 Loomy 账号标识)。

**修复**：credits_api / usage_collector / loomy_client / growth_presence / auto_router
一律优先 `import app_paths` 钉死安装根 (CONFIG_PATH / DATA_DIR / LOGS_DIR),
源码态单独运行时才兜底回 `__file__` (源码态 __file__ 是绝对路径, 本来就安全)。
app_paths.py 的 docstring 早就警告过这类错误 —— scripts 是漏网之鱼。

## 交互式登录在打包态没接上 task 路由（2026-09-14 VM 反馈）

**现象**：用户版里「添加账号」(Trae/WorkBuddy/WorkBuddy_IE) 报
`login_trae.py 不存在（该通道未提供登录脚本）`——Loomy 的桌面通道登录正常,
唯独走 playwright 网页登录的三个通道全挂。

**根因**：`admin_api.launch_login` 是纯源码态实现 —— 检查磁盘上
`<根>\scripts\login_*.py` 并用 `.venv\Scripts\python.exe` 拉起。打包态两者都不存在。
而 `task_main` 本来就支持按文件名路由到内置 login_* 模块
(`open-ai-task.exe <任意路径>\login_trae.py`), playwright 驱动也早已随包,
浏览器走系统 msedge —— 就差 admin_api 这一步没接上。

**修复**：launch_login 按 `sys.frozen` 分流 —— 打包态 spawn
`open-ai-task.exe <脚本路径标记>` (路径仅作路由标记, 不做 exists 检查);
源码态保持原样。三个 login 脚本的 OPENAI_CFG/LOG_PATH 同时钉死安装根
(同一 CWD 依赖 bug 的最后一处)。

## `__file__` 家族 #5 复发修复同步（2026-09-15，源自 portable 用户机事故）

portable 版装机后签到弹 FileNotFoundError（signin_all.py:92 load_json 读到 %TEMP%\_MEI
临时目录）。本仓库同步修复时 AST 全仓扫描发现：除 #5 的 signin_all / usage_history /
wb_usage_history / settlement_probe / task_main / watchdog_boot 六处外，**#4 批次
（account_manager 的 OPENAI_CFG/BASE、api_store、providers/loomy、providers/workbuddy）
也从未回流本仓库** —— 同一家族同一规则，本次一并钉死：app_paths + except 源码态兜底；
account_manager 保留 BASE 变量名（= OPENAI_ROOT/scripts），源码态行为比特级不变。

规则重申：运行时模块**禁止**用 `__file__` 拼 config/data/logs —— 一律 `import app_paths`，
仅 except 兜底分支可保留 `__file__`。本仓库无 installer/ 构建物料，门禁暂无挂载点；
未来若引入打包流程，先从 portable 仓库复制 installer/check_frozen_paths.py 挂入构建脚本。

## 未定义名（拼写错变量）只在用户机上炸（2026-09-16 portable 用户机事故）

**现象**：portable 版装机后签到进程直接崩，任务日志只有一段 traceback：

```
File "task_main.py", line 92, in <module>
File "task_main.py", line 84, in main
File "task_main.py", line 43, in _route
File "signin_all.py", line 529, in main
NameError: name 'loom_state' is not defined. Did you mean: 'loomy_state'?
```

**根因**：`scripts/signin_all.py` 的 Loomy 白天补签分支里，变量定义是
`loomy_state`，使用处却敲成了 `loom_state`（少一个 y）。这个名字**全仓从未定义**，
执行到那一行必抛 `NameError`。

**为什么本地测不出来**（这条比 bug 本身更重要）：
1. Python 只在**真正执行到该行**时才抛 NameError，而该行被
   `[a for a in lc.load_accounts() if ... not (loomy_state.get(...))...]`
   这个推导式包着 —— 开发机没登录 Loomy 账号 / `load_accounts()` 返回空时，
   推导式不进循环体，**这一行根本不执行**，全量测试照样全绿。
2. 三仓同源（本机版 / dev / portable 是同一份代码的副本）：改一处忘回流，
   另外两仓就带着同一个坑各自发版。本次 v3.1 的**两个安装包都已中招**
   （dev 13:14 的 resources.zip、portable 13:0x 的品牌 exe），发布后才由
   master 在用户机上撞到。

**修复**：`signin_all.py` 改回 `loomy_state`（三仓同步，md5 一致）。

**防复发（本次同时落地，别只改那一行）**：
- 新增 `tests/test_static_names.py` —— 全仓 AST 扫描「用了但没定义」的名字。
  判定：函数自身绑定 + 闭包外层 + 模块级定义 + 内建 + 推导式/lambda 变量，
  剩下的 Load 名才报。**嵌套函数只产出节点本身、不展开其内部** —— 否则
  嵌套函数的形参（`def chunk(delta, finish=None)` 的 `finish`）会被算到外层
  头上，实测多出十几条假阳性，那种门禁上线当天就会被当噪声关掉。
- 新增构建门禁 `installer/check_undefined_names.py`，已挂进
  `installer/build_exe.bat` 的 [0d] 步（与 [0c] frozen-path gate 并列）：
  未定义名 → `goto :err`，不产出安装包。两个发布仓都已挂载。

**规则重申**：
- 运行时模块改完变量名，**必须**跑一次 `check_undefined_names.py`
  （或 `python -m unittest tests.test_static_names`）；
  "测试全绿" 不等于 "没有未定义名" —— 条件分支里的错拼测试碰不到。
- 同一改动三仓同步后，用 md5 核对关键文件一致，别靠记忆。
- 这类「静态可查却没人查」的错已出现三次（`_ = rid` 的 UnboundLocalError、
  `__file__` 家族 #4/#5、本次 typo）。新增门禁挂在**构建脚本**上，
  而不是只写单测 —— 单测可以被跳过，构建门禁不行。

## 门禁只挂在一个渠道的构建脚本上 = 等于没有门禁（2026-09-16 补挂）

**发现经过**：修完「重新连接账号」装机必失败之后核对出包流程，发现两个渠道
用的**不是同一个构建脚本**：

| 渠道 | 出包脚本 | 静态门禁 |
| --- | --- | --- |
| portable（用户版，冻结 exe） | `installer/build_exe.bat` | [0c] frozen-path、[0d] undefined-name、[0e] script-launch |
| dev（开源版，源码分装） | `installer/build_all.py` | **一道都没有** |

两个渠道装的是**同一份运行时代码**（dev 把 `scripts/`、`providers/` 源码整包
装过去，portable 装冻结 exe）。门禁只挂在 portable 那条链上，dev 渠道就能带着
同一批 bug 出包 —— 而 `build_all.py` 是 Python 驱动的（自带说明：规避
PowerShell `-File` 对中文路径的拒绝），谁也没想到它漏了检查。

**修复**：`build_all.py` 补挂 [0c]/[0d]/[0e] 三步，与 `build_exe.bat` 对齐；
`run()` 增加 `cwd` 参数（门禁脚本在 `installer/` 下，而 `unittest` 必须从仓库根跑）。

**规则重申**：
- 新增门禁时，**必须把两条出包链都过一遍**（`grep -rn "check_" installer/`），
  只挂一条 = 另一条渠道裸奔。本项目已有两个渠道，将来加渠道同理。
- 「本地测试全绿」永远不能替代构建门禁：本文件记录的三次事故
  （`__file__` 家族、拼错变量名、打包态脚本拉起）全部满足「本地全绿、装机才炸」。

## portable 版登录助手仍拉系统 Edge：打包侧改了、脚本侧没同步（2026-09-16 虚拟机事故）

**现象**：master 在虚拟机装 portable 版，「添加账号」拉起的还是 Edge；
本机版 / dev 版拉的是 Chromium（v2.6 起就该如此）。

**根因**：三仓同源，但同步只做了一半 ——

| 部分 | 状态 |
| --- | --- |
| `installer/build_resources.py`（打包侧） | 已按 v2.6 约定，把 `ms-playwright/chromium-1234` 打进安装包（+190MB），注释里甚至写明「旧脚本会让本次修复在便携版上完全没生效，而且不报错」 |
| `scripts/login_*.py`（脚本侧） | **仍是 v2.6 之前的写法**：`p.chromium.launch(headless=False, channel='msedge')` |
| `app_paths.ensure_playwright_browsers_path()` | portable 已有且被调用 —— 指路了也没用，因为 `channel='msedge'` 是**强制**走系统浏览器 |
| portable 的 `MEMORY.md` | 抄了 dev 的 v2.6 段落，读起来像已完成 |

`channel='msedge'` 与「包内有没有 Chromium」无关：它直接点名系统 Edge。
于是安装包白涨 190MB、行为照旧，且**不报错** —— 属于最难查的一类。

**为什么 grep 查不出来**：三个脚本的文档头都写着
「旧版直接 `p.chromium.launch(channel='msedge')` 拉起系统 Edge —— …」，
那是在解释历史。文本检索必然命中，无法区分「注释里的旧代码」与「真的还在这么调」。
故门禁一律用 **AST 判代码**。

**修复**：portable 的三个 `login_*.py` 整份同步 dev 仓版本（改后 md5 三仓一致），获得
v2.6 的全部行为：`BROWSER_FALLBACK = chromium → chrome → msedge → firefox`
（自带优先）、`launch_persistent_context` + 每次全新 profile
（`data/pw_profiles/<通道>/run-<时间戳>`，多账号不互相顶号）、
反自动化检测（去 `--enable-automation`、隐藏 `navigator.webdriver`）、
原生 UA（不再伪造 `Edg/143`）、`--browser` / `--reuse-profile` 开关。

**防复发（本次同时落地）**：
- 新增构建门禁 `installer/check_login_browser.py`，挂进 `build_exe.bat` 的
  **[0f/7]** 步与 `build_all.py` 的 **[0f/5]** 步（两条出包链都挂，见上一节教训）；
- 新增 `tests/test_login_browser.py`（同规则的单元测试）：
  - R1 每个 `login_*.py` 必须有模块级 `BROWSER_FALLBACK` 且首元素是 `chromium`；
  - R2 `p.chromium.launch*` 的 `channel` 不许是字面量（必须由降级链决定，
    chromium 档传 `None` = 用包内自带的那个）；
  - R3 必须调 `ensure_playwright_browsers_path()`，否则打包态认不出
    `<root>\ms-playwright`，判定「浏览器未安装」→ 悄悄降级回系统 Edge。
- 有效性已实测：把门禁/测试指向修复前的脚本，会精确报出
  `login_trae.py:113`、`login_workbuddy.py:92`、`login_workbuddy_intl.py:468`
  三处写死的 `channel='msedge'`；对修复后的脚本全绿。

**规则重申**：
- 「包装备好了」不等于「代码用上了」：凡是「安装包内嵌了某资源」的改动，
  必须同时验证**代码真的按预期路径去用它**（本次体积涨了 190MB 却毫无效果）。
- 同源仓库的同步要**成对检查**：改了 A 侧（打包/安装器）就必须确认 B 侧
  （运行时脚本）也到位；只有单侧更新的提交，等于埋一个静默失效。
- 判断「代码里还在不在用旧写法」用 AST，不要用 grep —— 文档头里的历史说明
  会稳定误命中（本项目已因此差点漏判）。

## v3.1 Release 资产已被覆盖（2026-09-16 晚，master 指示）

**背景**：v3.1 的 release 标题写着「Chromium 登录」，但其中 **portable 资产**
（`open-ai-installer-portable.exe`，407,056,104 字节）实际是登录助手仍拉系统
Edge 的那版 —— 标题为真、产物为假（原因见上一节：打包侧改了、脚本侧没同步）。

**操作**：用本机重出的两个包原地覆盖 v3.1 的两个 asset（**未新建 tag / 未改
release 元数据**，只有二进制换了）：

| 资产 | 覆盖前 | 覆盖后 |
| --- | --- | --- |
| `open-ai-installer-portable.exe` | 407,056,104 字节（asset id 567763550） | **419,092,059** 字节（asset id 568268612）<br>sha256 `1418dd28…9333c` |
| `open-ai-installer-dev.exe` | 34,606,607 字节（asset id 567762807） | **34,612,819** 字节（asset id 568275512）<br>sha256 `6deeb204…fa30f` |

- 覆盖**前**的两个旧 asset 已整份留档到
  `/mnt/d/dsh_归档区/20260916-233000-github-v3.1-assets-before-overwrite/`
  （含 `归档记录.tsv`），可随时回退。
- 上传后**回读校验**：GitHub 返回的 sha256 与本机文件逐字节一致；
  公开下载 URL（`https://github.com/BOY-Chinese/open-ai/releases/download/v3.1/…`）
  均 HTTP 200 且字节数正确。

**⚠️ 覆盖已知副作用（下次发版务必记住）**：
`admin_api.check_update` 是拿 release 的 **tag** 与 `version.APP_VERSION`
（`portable-v3.1`）**比相等**来判断「有没有更新」。资产原地覆盖、tag 不变，
所以**任何已装 v3.1 的机器都会显示「已是最新」**，不会自动拉到修复版，
必须重新下载安装包。下次同类修复建议**新打 tag**（如 `v3.1.1`）并同步改
`version.py` 的 `APP_VERSION`，而不是原地覆盖资产。

**上传踩坑**：本机（WSL + Watt 加速）到 `uploads.github.com` 约 **1.9 MB/s**，
419MB 资产需 ~3.5 分钟。用 Python `urllib` 上传会撞上默认 180s 的 socket 写超时
（`TimeoutError: The write operation timed out`），**且此时旧 asset 已被删掉、
新 asset 没传上去 —— 中间态等于把资产弄丢**。正确姿势：`curl --retry 3
--retry-all-errors --connect-timeout 30 --speed-time 120 --speed-limit 10240
--data-binary @file`（无总时限，只在链路真的停住时才放弃），先删后传要经得起重试。
