# open-ai 一键安装包 (installer)

把 open-ai 打包成**傻瓜式一键安装 exe**：用户双击（以管理员身份）→ 选安装目录（默认 C:\open-ai）→
自动部署环境（Python/Node/依赖/Playwright）→ 创建桌面快捷方式 → 询问开机自启。

## 目录结构

```
installer/
├── installer.py          # 安装器主程序 (tkinter GUI)
├── build_resources.py    # 把 open-ai 资源打包成 resources.zip
├── build_exe.bat         # Windows 上生成 exe 并自动放到桌面
├── config.shell.json     # 空壳 config 模板 (安装时生成)
├── ico/open-ai.ico       # 软件图标 (用于 exe / 快捷方式 / 安装器窗口)
└── resources.zip         # (构建产物) 打包好的资源
```

## 构建步骤 (Windows)

**最简单：双击项目根目录的 `一键构建.bat`**，自动完成：
1. 打包资源 → `resources.zip`（含 pic 图标）
2. 自动安装 pyinstaller（首次）
3. 生成 `open-ai-installer.exe`（含图标 + 请求管理员权限 `--uac-admin`）
4. 自动复制到桌面

或手动：
```bat
cd installer
python build_resources.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
    --name "open-ai-installer" ^
    --icon "ico\open-ai.ico" ^
    --add-data "resources.zip;." ^
    --add-data "config.shell.json;." ^
    --add-data "ico\open-ai.ico;ico" ^
    installer.py
```

## 安装器功能

| 步骤 | 说明 |
|---|---|
| 管理员提醒 | 页面红色高亮「注意：本安装程序需要以管理员身份运行」，exe 自带 UAC 请求 |
| 选目录 | 默认 `C:\open-ai`，可浏览选择 |
| 解压资源 | 解压全部代码 + trae/lib 依赖 + pic 图标 |
| 生成 config | 写入空壳 config.json（占位符，需用户自行填 api_key/device_id） |
| 自动部署 | 检测 Python/Node，缺失则自动下载静默安装 |
| 建 venv | 创建虚拟环境 + 装依赖（清华镜像） |
| Playwright | 自动安装浏览器（Trae 后端需要） |
| 桌面快捷方式 | 创建单个「open-ai」快捷方式（先启网关，再开账号管理），用软件图标 |
| 开机自启 | 询问用户，勾选则复制到启动文件夹 |

## 卸载修复

之前「一键卸载」无法删除自身所在目录。已改为：卸载脚本把删除操作写入 TEMP（目录外），
用从 System32 启动的分离进程删除整个 open-ai 目录，可彻底删除。

## 注意

- 安装器**不含**任何个人密钥/账号数据（空壳版）
- 安装后用户需编辑 `config.json` 填入 `api_key` 与 `device_id`（见 README §3）
- 打包需在 **Windows** 上执行（PyInstaller 平台绑定）
- 构建前先确认 `installer/ico/open-ai.ico` 存在（图标）
