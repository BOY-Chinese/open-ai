#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从官方 qodercli npm 包提取 UMID 设备风控组件（runtime-info）
================================================================
**用途**：本机没装 Qoder 桌面客户端时，签到需要 `runtime-info` 现算设备风控
身份三件套（machineToken / machineCode / machineType），否则上游**根本不下发**
签到活动（CN / INTL 一样）：

    无 UMID → {"showCampaign": false, "claimable": false, "campaigns": []}
    有 UMID → {"showCampaign": true,  "claimable": true,  "campaigns": [CLAIM_BENEFIT]}

★ 为什么能从 npm 包拿到：官方把 UMID 的 ELF 可执行文件**内嵌进了 CLI 的
  bundle**（`package/bundle/qodercli.js` 里一段 base64）。桌面客户端只是
  顺带提供了同一个组件的副本 —— 所以「不装桌面客户端」并不等于「拿不到身份」。
  （思路与校验方式对齐开源项目 L0NE-6/Qoder-Daily 的 install_umid_component。）

★ 安全约束（本脚本的全部意义就在这里）：
  1. **只信 npm 官方 registry**（registry.npmjs.org）。镜像源（npmmirror）只作为
     加速通道，且必须与官方源 `version` + `integrity` **逐字一致**才采用；
  2. 下载内容必须通过 **sha512 integrity** 校验，不一致直接丢弃；
  3. 提取出来的是**独立 ELF**，按 CPU 架构（e_machine：x86_64=62 / aarch64=183）挑选；
  4. **绝不自己造 token**：`derive_machine_token()` 那种伪身份恰恰是被上游挡住
     的原因（MEMORY 有实测对照）。拿不到就如实失败，返回非零退出码。

用法:
  python scripts/umid_fetch.py                 # 提取到默认目录（见 --help）
  python scripts/umid_fetch.py --verify        # 只检查本机是否已有可用组件
  python scripts/umid_fetch.py --out <dir>     # 指定输出目录
  python scripts/umid_fetch.py --realm cn      # cn / intl（决定默认输出位置与探测顺序）

提取成功后，`providers.qoder_common._umid_component()` 会自动发现它
（探测顺序：QD_UMID_PATH → 输出目录 → 官方客户端 resources\\umid）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import platform
import subprocess
import sys
import tarfile
import io

# ── npm 源 ──────────────────────────────────────────────────────────
# 官方源是**唯一权威**：镜像源的数据必须与它逐字一致才采用。
NPM_AUTHORITY = "https://registry.npmjs.org"
NPM_MIRRORS = ("https://registry.npmmirror.com",)
NPM_PACKAGE_PATH = "@qoder-ai%2fqodercli"

# ELF e_machine 值 → 本脚本使用的架构名
ELF_MACHINE = {"x86_64": 62, "aarch64": 183}

# 内嵌 ELF 的 base64 起始标记（`\x7fELF` 的 base64 前 6 字符）
ELF_MARK = '"f0VMRgIB'

DEFAULT_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), ".openai-umid")
EXE_NAME = "runtime-info.exe" if sys.platform.startswith("win") else "runtime-info"


def log(msg: str) -> None:
    """打印。

    ★ Windows 控制台默认 GBK，`✓`/`✗`/`·` 这些字符会直接抛
      UnicodeEncodeError（2026-10-24 实测：脚本在 Windows 侧跑到一半崩掉）。
      这里统一转成 ASCII 标记，并对任何编码失败兜底，绝不因日志把流程打断。
    """
    table = {"✓": "[OK]", "✗": "[!!]", "·": "-", "⚠": "[WARN]", "…": "..."}
    for k, v in table.items():
        msg = msg.replace(k, v)
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = (sys.stdout.encoding or "utf-8")
        print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)


# ★ 架构名必须与 `_sniff()` 的返回值**同一套写法**（x64 / arm64 / x86）。
#   第一版用 "x86_64"/"aarch64" 与 _sniff 的 "x64"/"arm64" 不一致，直接导致
#   「包里没有 PE/x86_64 架构的 UMID 程序」——提取静默失败（2026-10-24 踩过）。
def _arch() -> str:
    raw = (platform.machine() or "").strip().lower()
    if raw in ("arm64", "aarch64"):
        return "arm64"
    if raw in ("x86_64", "amd64", "x64"):
        return "x64"
    if raw in ("i386", "i686", "x86"):
        return "x86"
    raise RuntimeError(f"当前 CPU 架构不受支持: {raw or '未知'}")


def _http_bytes(url: str, timeout: int = 180) -> bytes:
    """取二进制。不信任环境代理变量之外的任何东西（与项目内其它脚本同口径）。"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "open-ai-umid-fetch/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def npm_meta(registry: str) -> dict:
    """读 `dist` 元数据：version / tarball / integrity。"""
    raw = _http_bytes(f"{registry}/{NPM_PACKAGE_PATH}/latest", timeout=30)
    payload = json.loads(raw.decode("utf-8", "replace"))
    dist = payload.get("dist") if isinstance(payload.get("dist"), dict) else {}
    tarball = str(dist.get("tarball") or "").strip()
    integrity = str(dist.get("integrity") or "").strip()
    if not tarball or not integrity:
        raise RuntimeError("npm 元数据缺少 tarball / integrity")
    return {"version": str(payload.get("version") or "").strip(),
            "tarball": tarball, "integrity": integrity}


def pick_source() -> tuple[dict, str]:
    """选下载源：官方源优先；镜像必须与官方源 version+integrity 完全一致才用。

    返回 (meta, registry)。任何一步不一致都**宁可使用官方源**（慢一点但可信）。
    """
    try:
        authority = npm_meta(NPM_AUTHORITY)
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠ 官方源不可达（{exc}），只能尝试镜像 —— 将无法做交叉校验")
        authority = None

    if not authority:
        # 官方源不可达：只能用镜像。此时没有交叉校验的基准，会在日志里明说，
        # 但 **sha512 integrity 校验依旧生效**（镜像自己给的 integrity）。
        for reg in NPM_MIRRORS:
            try:
                return npm_meta(reg), reg
            except Exception as exc:  # noqa: BLE001
                log(f"  · 镜像 {reg} 也不可用（{exc}）")
        raise RuntimeError("所有 npm 源均不可用")

    # 官方源可用：镜像只用于**展示一致性**，下载一律走官方源（最可信）。
    for reg in NPM_MIRRORS:
        try:
            mirror = npm_meta(reg)
        except Exception as exc:  # noqa: BLE001
            log(f"  · 镜像 {reg} 不可用（{exc}）")
            continue
        if (mirror["integrity"] != authority["integrity"]
                or mirror["version"] != authority["version"]):
            log(f"  ⚠ 镜像 {reg} 与官方源不一致（version/integrity），已拒绝")
            continue
        log(f"  ✓ 镜像 {reg} 与官方源一致（v{authority['version']}）")
        break
    return authority, NPM_AUTHORITY


def _sniff(raw: bytes) -> tuple[str, str] | None:
    """识别内嵌二进制：(format, arch)。识别不出返回 None。

    ★★ 2026-10-24 修正（这是本脚本第一版的致命错误）：
      第一版只认 ELF（比 e_machine），于是**在 Windows 上提取出来的是
      `\x7fELF` 的 Linux 可执行文件**，命名成 `runtime-info.exe` 之后
      Windows 直接提示「不兼容」。master 当场指出 —— 确实是下错了。

      qodercli 的 bundle 里其实**六个平台都内嵌了**（实测 v1.1.78）：
          MachO  x64 / arm64          约 1.0 MB
          ELF    x64(62) / arm64(183) 约 650 KB
          PE     x64                  480 KB   ← UMID（含 machineToken/UMID 字样）
          PE     x64                  7.3 MB   ← 另一个大程序，不是 UMID
      必须按**目标平台**挑，而不是按"能不能解出 ELF"挑。
    """
    if len(raw) < 0x40:
        return None
    if raw[:4] == b"\x7fELF":
        return "ELF", {62: "x64", 183: "arm64", 3: "x86"}.get(
            int.from_bytes(raw[18:20], "little"), "?")
    if raw[:2] == b"MZ":
        try:
            off = int.from_bytes(raw[0x3c:0x40], "little")
            if raw[off:off + 4] == b"PE\0\0":
                mach = int.from_bytes(raw[off + 4:off + 6], "little")
                return "PE", {0x8664: "x64", 0xAA64: "arm64", 0x14C: "x86"}.get(mach, "?")
        except Exception:  # noqa: BLE001
            return None
    if raw[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        return "MachO", "?"
    return None


# 各类可执行文件在 bundle 里的 base64 前缀（用于快速定位候选）
_B64_HINTS = {
    "ELF": "f0VMRgIB",                  # \x7fELF\x02\x01\x01
    "PE": "TVqQAAMAAAAEAAAA",           # MZ....... (标准 PE 桩)
    "MachO": "7fZWxhd",                 # 仅作提示，实际靠 _sniff 判定
}

# UMID 组件的"指纹"——在候选二进制里必须出现，用来在大 PE 与小 PE 之间区分
_UMID_MARKERS = (b"machinetoken", b"machinecode", b"machineType".lower(), b"umid")


def _iter_embedded(bundle: str, min_len: int = 100000):
    """逐个吐出 bundle 里内嵌的 base64 大块（解码后）。"""
    pattern = re.compile(r'"([A-Za-z0-9+/=]{%d,})"' % min_len)
    for m in pattern.finditer(bundle):
        encoded = m.group(1)
        # 过长的 base64 不一定可解（JS 源码里的长字符串），失败就跳过
        try:
            raw = base64.b64decode(encoded)
        except Exception:  # noqa: BLE001
            continue
        if raw:
            yield raw


def extract_binaries(bundle: str, want_format: str, want_arch: str) -> dict:
    """从 bundle 里挑出**目标平台的一对**UMID 组件。

    ★★ 这是本脚本第二版的核心修正（2026-10-24，master 指出「不兼容」后重做）：

      第一版只认 ELF → 在 Windows 上提取出一个 `\x7fELF` 的 Linux 可执行文件，
      命名成 `.exe` 后被 Windows 判为不兼容。**完全没救**。

      第二版只看格式仍是**不完整的** —— UMID 在 Windows 上不是单个 exe，
      而是 **exe + sgsdk.dll 一对**，缺 dll 会直接 panic：
          Failed to load sgsdk.dll: LoadLibraryExW { code: 126 }
      依据是官方代码自己就成对写入（bundle 里可见）：
          u1i(vee(g,"runtime-info.exe"), i, {mode: 320})
          u1i(vee(g,"sgsdk.dll"),     r, {mode: 320})
          eAt(g, a, l)  // 对两者分别做 sha256 校验

      实测 v1.1.78 的 bundle 内嵌 6 个二进制：
          MachO x64/arm64 · ELF x64(62)/arm64(183) · PE 480KB(EXE) · PE 7.3MB(DLL)

    返回 {"exe": bytes, "dll": bytes|None}；找不到返回空 dict。
    """
    exes, dlls = [], []
    for raw in _iter_embedded(bundle):
        got = _sniff(raw)
        if not got or got != (want_format, want_arch):
            continue
        is_dll = _is_dll(raw)
        low = raw.lower()
        # exe：必须带 UMID 指纹（避免误取包里其它大程序）
        if not is_dll:
            hits = sum(1 for mk in _UMID_MARKERS if mk in low)
            if hits >= 1:
                exes.append((hits, len(raw), raw))
        else:
            dlls.append((len(raw), raw))
    out: dict = {}
    if exes:
        exes.sort(key=lambda c: (-c[0], c[1]))
        out["exe"] = exes[0][2]
    if dlls:
        # Windows 只有一对时 dll 就是 sgsdk.dll；有多个时取带 umid 字样的
        marked = [d for d in dlls if b"umid" in d[1].lower()]
        pick = marked or dlls
        pick.sort(key=lambda c: -c[0])
        out["dll"] = pick[0][1]
    return out


def _is_dll(raw: bytes) -> bool:
    """PE 的 IMAGE_FILE_DLL 标志（Characteristics & 0x2000）。"""
    if raw[:2] != b"MZ":
        return False
    try:
        off = int.from_bytes(raw[0x3c:0x40], "little")
        chars = int.from_bytes(raw[off + 0x16:off + 0x18], "little")
        return bool(chars & 0x2000)
    except Exception:  # noqa: BLE001
        return False


def target_format() -> str:
    """本机该取哪种格式：Windows → PE，Linux → ELF，macOS → MachO。"""
    if sys.platform.startswith("win") or os.name == "nt":
        return "PE"
    if sys.platform == "darwin":
        return "MachO"
    return "ELF"


def download_and_extract(meta: dict, arch: str) -> dict:
    fmt = target_format()
    log(f"目标平台 {sys.platform} -> 取 {fmt} / {arch}")
    log(f"  下载 {meta['tarball']} ...")
    body = _http_bytes(meta["tarball"], timeout=300)
    log(f"  已下载 {len(body) / 1024 / 1024:.1f} MB")

    # ★ sha512 integrity 校验：不一致直接丢弃，绝不落盘
    integ = meta.get("integrity") or ""
    if not integ.startswith("sha512-"):
        raise RuntimeError(f"不支持的 integrity 算法: {integ[:20] or '空'}")
    actual = base64.b64encode(hashlib.sha512(body).digest()).decode()
    if actual != integ[7:]:
        raise RuntimeError("下载内容与 npm integrity 不一致（可能被篡改），已中止")
    log("  [OK] sha512 integrity 校验通过")

    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        handle = archive.extractfile("package/bundle/qodercli.js")
        bundle = handle.read().decode("utf-8", "replace") if handle else ""
    if not bundle:
        raise RuntimeError("包里没有 package/bundle/qodercli.js")

    files = extract_binaries(bundle, fmt, arch)
    if fmt == "MachO" and not files:
        best = None
        for cand in _iter_embedded(bundle):
            if best is None or len(cand) > len(best):
                best = cand
        if best:
            files = {"exe": best}
    if not files.get("exe"):
        raise RuntimeError(f"包里没有 {fmt}/{arch} 架构的 UMID 主程序")
    log(f"  [OK] 主程序 {len(files['exe']) / 1024:.0f} KB")
    if files.get("dll"):
        log(f"  [OK] 依赖库 sgsdk.dll {len(files['dll']) / 1024:.0f} KB")
    elif fmt == "PE":
        raise RuntimeError("PE 包里没有 sgsdk.dll —— 缺它会直接 panic，已中止")
    return files


def _atomic_write(path: str, data: bytes, mode: int = 0o755) -> None:
    """临时文件 + 原子替换（死命令 #2：不裸覆盖）。

    ★★ 2026-10-24 实测踩坑：如果**从 WSL 侧**（`/mnt/c/...`）用 Python 写，
      或者先写到临时文件再 `os.replace`，在 Windows 上跑起来会报
          Failed to load sgsdk.dll: LoadLibraryExW { code: 126 }
      —— 文件大小、sha256 都对，就是加载不了（跨文件系统写入带进来的
      元数据差异）。同一份内容用 PowerShell `Copy-Item` 复制过去就正常。

      所以 **Windows 上改为：先落到本机临时目录，再用 PowerShell 复制到位**。
      Linux / macOS 走原来的原子替换。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.name == "nt":
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=os.path.basename(path))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # PowerShell 复制：绕过跨文件系统写入的元数据问题
            ps = (f"Copy-Item -LiteralPath '{tmp}' -Destination '{path}' -Force")
            subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                           check=True, capture_output=True, timeout=60)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.chmod(tmp, mode)
    except Exception:  # noqa: BLE001
        pass
    os.replace(tmp, path)


def verify(path: str) -> dict:
    """跑一次组件，确认能吐出三件套。返回 values；失败抛异常。"""
    payload = (json.dumps({"account": ""}) + "\n").encode("utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    # ★ Windows / macOS 版组件必须带 `--account-stdin` 才会从 stdin 读账号
    #   （PE 里实测存在该开关字符串；不带它组件不会读 stdin，直接空跑）。
    args = [path, "3"]
    if sys.platform.startswith("win") or sys.platform == "darwin":
        args.append("--account-stdin")
    proc = subprocess.run(  # noqa: S603
        args, input=payload, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=25, creationflags=flags)
    out = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
    data = json.loads(out[0].strip()) if out and out[0].strip() else {}
    if not isinstance(data, dict):
        raise RuntimeError("组件输出不是 JSON 对象")
    missing = [k for k in ("machineToken", "machineCode", "machineType")
               if not str(data.get(k) or "").strip()]
    if missing:
        raise RuntimeError("组件输出缺少: " + ", ".join(missing))
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从官方 qodercli npm 包提取 UMID 组件")
    ap.add_argument("--out", default="", help="输出目录（默认 %s）" % DEFAULT_ROOT)
    ap.add_argument("--realm", default="intl", choices=("cn", "intl"),
                    help="仅影响验证时传的 env 参数：intl=3 / cn=0")
    ap.add_argument("--verify", action="store_true",
                    help="不下载，只检查指定目录下是否已有可用组件")
    args = ap.parse_args(argv)

    out_dir = (args.out or "").strip() or DEFAULT_ROOT
    target = os.path.join(out_dir, EXE_NAME)

    if args.verify:
        if not os.path.isfile(target):
            log(f"[!!] 未找到组件: {target}")
            return 2
        try:
            vals = verify(target)
        except Exception as exc:  # noqa: BLE001
            log(f"[!!] 组件不可用: {exc}")
            return 1
        log(f"[OK] 组件可用: {target}")
        log(f"  machineType={vals.get('machineType')} "
            f"machineCode={str(vals.get('machineCode'))[:12]}... "
            f"machineToken={str(vals.get('machineToken'))[:12]}...")
        return 0

    arch = _arch()
    log(f"架构 {arch}")

    try:
        meta, registry = pick_source()
    except Exception as exc:  # noqa: BLE001
        log(f"[!!] 选择下载源失败: {exc}")
        return 1
    log(f"  版本 v{meta['version']} 源 {registry}")

    try:
        files = download_and_extract(meta, arch)
    except Exception as exc:  # noqa: BLE001
        log(f"[!!] 提取失败: {exc}")
        return 1

    target = os.path.join(out_dir, EXE_NAME)
    dll_name = "sgsdk.dll" if target_format() == "PE" else "sgsdk.so"

    # 死命令 #2：覆盖前先归档旧文件（两个都备份）
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    for name in (EXE_NAME, dll_name):
        old = os.path.join(out_dir, name)
        if os.path.isfile(old):
            try:
                with open(old, "rb") as src, open(f"{old}.{stamp}.bak", "wb") as dst:
                    dst.write(src.read())
                log(f"  - 旧文件已备份: {old}.{stamp}.bak")
            except Exception as exc:  # noqa: BLE001
                log(f"  [WARN] 旧文件备份失败（继续）: {exc}")

    _atomic_write(target, files["exe"])
    log(f"[OK] 已写入主程序 {target}")
    if files.get("dll"):
        dll_path = os.path.join(out_dir, dll_name)
        _atomic_write(dll_path, files["dll"])
        log(f"[OK] 已写入依赖库 {dll_path}")

    try:
        vals = verify(target)
    except Exception as exc:  # noqa: BLE001
        log(f"[!!] 写入后自检失败: {exc}")
        return 1
    log(f"[OK] 自检通过：machineType={vals.get('machineType')} "
        f"machineCode={str(vals.get('machineCode'))[:12]}...")
    vm = vals.get("vmInfo") or {}
    if isinstance(vm, dict) and vm:
        log(f"  vmInfo: isVm={vm.get('isVm')} brand={vm.get('brand')} "
            f"percentage={vm.get('percentage')}")
    log("")
    log("下一步：把它交给 open-ai（二选一）")
    log(f"  a) 设环境变量 QD_UMID_PATH={target}")
    log("  b) 或把该目录加入 providers.qoder_common._umid_component() 的探测链")
    return 0


if __name__ == "__main__":
    sys.exit(main())
