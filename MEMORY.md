# open-ai 网关 · 关键事实记忆 (MEMORY)

> 本文件随代码打包一并保留。涉及设备标识、签到机制等关键约束。

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

## 关联文件
- 配置：`config.json` → `providers.trae.device_id` / `providers.trae.headers.x-device-id`
- 签到：`scripts/signin_all.py`（`trae_checkin_one`）、`trae/server.js`
- 实测日志：`logs/signin.log`（TRAE 段）、`logs/daemon.log`
</content>
