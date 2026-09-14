#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sanitize_check.py — 隐私 / 凭据泄漏检查 (推 GitHub 前必跑)
===========================================================
开发机上真实存在的 `config.json` / `logs/` / `data/` 里含有**可用的账号凭据**
(accessToken / refreshToken / 网关 api_key / cookie / device_id)。一次
`git add -A` 就能把它们全部公开, 而 GitHub 上改写历史极其麻烦。所以用机器查,
别靠记性。

扫描对象是「**会被提交的那批文件**」, 不是整个工作区 —— 有 git 时直接问
`git ls-files -c -o --exclude-standard` (已跟踪 + 未跟踪且未被忽略)。这样
构建产物 (installer/*.exe、dist/、resources.zip) 天然不在名单里, 不必为了
过检去删它们; 而"漏写一条 .gitignore" 的后果会被 C 项单独抓出来。

三件事:

  A. 危险文件是否还躺在工作区 (config.json / logs/ / data/ / *.log / 流水库)
  B. 敏感**值**是否被抄进会被提交的文件
       - 通用形态: JWT、`sk-` 风格密钥、48 位 hex、Windows 私有绝对路径
         (只写形态, 不含具体值, 所以本文件可以安全入库)
       - 本机已知真值: 从不入库的 `sanitize_known_values.txt` 逐行读
         (手机号 / 具体 uid / 具体 device_id / 发布账号名 …)
  C. `.gitignore` 是否**确实**忽略了这些路径 —— 问 `git check-ignore`,
     不读文本 (漏一条正则就等于没漏)

用法:
    python sanitize_check.py            # 检查
    python sanitize_check.py --fix      # 顺带删掉可再生的敏感文件

退出码: 0 = 干净, 1 = 有问题
"""
import argparse
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── A. 工作区里不该有的东西 (即便被 .gitignore 挡住也提醒一句) ──
FORBIDDEN_PATHS = ['config.json', 'logs', 'data', '.venv', 'runtime']
# 说明: MEMORY.md **允许**入库 —— 纯工程结论 (device_id 生成机制、WorkBuddy
# 接口差异、桌面端易踩约束), 不含真实凭据, 对后来者价值很高; 由 B 项继续看守。

FORBIDDEN_GLOBS = [
    r'^config\.json\.bak',
    r'^config\.json\.polluted',
    r'.*\.log$',          # 构建日志写进本机绝对路径, 运行日志直接落 token
    r'.*\.db$',
    r'.*\.sqlite3?$',
]

# ── B. 通用敏感**形态** (不含任何具体值, 故可安全入库) ──
SECRET_PATTERNS = [
    ('JWT / Bearer token', re.compile(r'eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{20,}')),
    ('OpenAI 风格密钥 (sk-)', re.compile(r'sk-[A-Za-z0-9_\-]{24,}')),
    # 网关 api_key 是 48 位 hex。**只查 48 位**: 64 位会命中 Cargo.lock /
    # package-lock 的包校验和 —— 那是公开的 crates.io 摘要, 不是机密。
    ('裸 hex 密钥 (48 位)', re.compile(r'(?<![\w\-])[0-9a-f]{48}(?![\w\-])')),
    # C:\Users\<name>\… / D:\app\… 这类开发机私有路径。
    # 写「形态」而不是写死某个用户名, 本文件才经得起公开。
    ('Windows 私有绝对路径', re.compile(
        r'[A-Za-z]:[\\/](Users|home|app)[\\/](?![<.\-])[A-Za-z0-9._\-]{2,}')),
]

# 本机专属的「已知真实值」清单: 每行一个字面量, # 开头注释。
# ★ 该文件**不入库** (.gitignore 已忽略)。检查器需要它们才能发现「某个真值被
#   抄进了源码/产物」, 但公开仓库里绝不该出现。缺它时只跑通用形态 —— 有意降级:
#   别人 clone 下来照样能查出 JWT / sk- 这类结构性泄漏。
LOCAL_VALUES_FILE = 'sanitize_known_values.txt'

# 命中了但**明确无害**的上下文。
#
# ★ 只列**依赖锁文件的元数据行**这类客观特征, 不要写"历史/曾经/实测"之类的
#   散文词 —— 那样等于给任意一行开免检: 本项目就发生过"解释注释里写了本机
#   桌面路径, 却因为注释含'实测踩过'而被放过"。真要有豁免, 走 SELF_EXEMPT
#   (整份文件) 或直接改代码, 别放宽这里。
BENIGN_LINE = re.compile(
    r'(^\s*(checksum|name|version|resolved)\s*=\s*"'
    r'|"integrity"|"sha512"|"sha256"'
    r'|\bSELF_EXEMPT\b|\bLOCAL_VALUES_FILE\b'
    r'|#\s*sanitize-benign\b)')

# 允许出现的「假值」—— 演示/占位标记。
ALLOWED_PLACEHOLDER = re.compile(
    r'(DEMO|NOT[_-]REAL|YOUR_|CHANGE_ME|PLACEHOLDER|EXAMPLE|'
    r'xxxx|X{4,}|<[^>]+>|\.\.\.|smoke)',
    re.IGNORECASE)

# 检测器自身豁免: 要识别某类凭据就得把那串**形态**写进正则, 不排除自己会永远报自己。
SELF_EXEMPT = {'sanitize_check.py', 'check_desktop_bundle.py'}

# 无 git 时兜底遍历要跳过的目录
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', 'node_modules_tools',
             'node_modules_store', 'target', 'dist', 'build', 'exes', 'logs',
             'data', 'runtime', '.venv', 'toolchain', 'desktop'}

BINARY_EXT = {'.exe', '.dll', '.ico', '.png', '.jpg', '.jpeg', '.webp', '.gif',
              '.zip', '.pyd', '.node', '.so', '.db', '.woff', '.woff2', '.ttf'}


def rel(path):
    return os.path.relpath(path, ROOT).replace('\\', '/')


def has_git():
    return os.path.isdir(os.path.join(ROOT, '.git'))


def committable_files():
    """会被提交的文件清单: 已跟踪 + 未跟踪且未被忽略。"""
    if has_git():
        # ★ -z: 文件名用 NUL 分隔、不做转义。默认文本模式下 git 会把中文文件名
        #   转义成带引号的八进制串 (core.quotePath), 拼出来的路径不存在,
        #   io.open 失败后被 except 静默跳过 —— CJK 文件名 (如 UI改进说明.md)
        #   整个成为检查盲区, 泄漏查不出来。本项目已踩过。
        r = subprocess.run(['git', 'ls-files', '-c', '-o', '--exclude-standard', '-z'],
                           cwd=ROOT, capture_output=True)
        if r.returncode == 0:
            out = []
            for raw in r.stdout.split(b'\0'):
                line = raw.decode('utf-8', 'replace').strip()
                if line:
                    out.append(os.path.join(ROOT, line.replace('/', os.sep)))
            return out
    # 兜底: 手工遍历 + SKIP_DIRS
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            out.append(os.path.join(dirpath, fn))
    return out


def load_local_patterns():
    """读不入库清单 → [(名称, 编译好的正则), ...]。"""
    out = []
    path = os.path.join(ROOT, LOCAL_VALUES_FILE)
    if not os.path.exists(path):
        return out
    with io.open(path, encoding='utf-8', errors='replace') as f:
        for line in f.read().splitlines():
            lit = line.strip()
            if not lit or lit.startswith('#'):
                continue
            out.append(('本机已知真值(…%s)' % lit[-6:], re.compile(re.escape(lit))))
    return out


def check_worktree(problems):
    """A. 工作区里是否还躺着危险文件。"""
    for name in FORBIDDEN_PATHS:
        if os.path.exists(os.path.join(ROOT, name)):
            problems.append(('工作区有敏感文件', name, None))
    for full in committable_files():
        base = os.path.basename(full)
        for pat in FORBIDDEN_GLOBS:
            if re.match(pat, base, re.IGNORECASE):
                problems.append(('工作区有敏感文件', rel(full), None))
                break


def check_gitignore(problems):
    """C. .gitignore 是否真的挡住它们 (问 git, 不读文本)。"""
    if not has_git():
        gi = os.path.join(ROOT, '.gitignore')
        if not os.path.exists(gi):
            problems.append(('.gitignore 缺失', '.gitignore', None))
            return
        text = io.open(gi, encoding='utf-8').read()
        for need in ('config.json', 'logs/', 'data/', LOCAL_VALUES_FILE):
            if need not in text:
                problems.append(('.gitignore 未覆盖', need, None))
        return
    samples = ['config.json', 'logs/x.log', 'data/usage_history.db',
               LOCAL_VALUES_FILE,
               'desktop/open-ai-desktop.exe', 'installer/resources.zip',
               'installer/open-ai-gateway.exe', 'installer/open-ai.exe',
               'installer/build_installer.log',
               'desktop-ui/node_modules/x', 'desktop-ui/src-tauri/target/x',
               'trae/lib/sscronet.dll', 'toolchain/bin/cargo.exe',
               'scripts/__pycache__/x.pyc']
    for s in samples:
        r = subprocess.run(['git', 'check-ignore', '-q', s],
                           cwd=ROOT, capture_output=True)
        if r.returncode != 0:
            problems.append(('.gitignore 未覆盖', s, None))


def check_secrets(problems):
    """B. 敏感值是否写进了会被提交的文件。"""
    patterns = SECRET_PATTERNS + load_local_patterns()
    for full in committable_files():
        base = os.path.basename(full)
        if base in SELF_EXEMPT:
            continue
        if os.path.splitext(base)[1].lower() in BINARY_EXT:
            continue
        r = rel(full)
        try:
            with io.open(full, encoding='utf-8', errors='ignore') as f:
                text = f.read()
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if BENIGN_LINE.search(line):
                continue
            for name, pat in patterns:
                for m in pat.finditer(line):
                    found = m.group(0)
                    if ALLOWED_PLACEHOLDER.search(found):
                        continue
                    if ALLOWED_PLACEHOLDER.search(line):
                        continue
                    problems.append((name, '%s:%d' % (r, lineno), found[:32]))


def do_fix():
    """删除**可再生**的敏感文件。

    config.json 只提示不删: 那是真实凭据, 误删不可恢复, 而且用户往往正用着它。
    """
    removed = []
    for name in ('logs', 'data', 'runtime'):
        p = os.path.join(ROOT, name)
        if os.path.isdir(p):
            import shutil
            shutil.rmtree(p, ignore_errors=True)
            removed.append(name + '/')
    for full in committable_files():
        base = os.path.basename(full)
        if (base.startswith('config.json.bak')
                or base.startswith('config.json.polluted')
                or base.lower().endswith('.log')):
            try:
                os.remove(full)
                removed.append(rel(full))
            except OSError:
                pass
    if os.path.exists(os.path.join(ROOT, 'config.json')):
        print('[!] config.json 含真实凭据 —— 已原样保留, 请确认它没被提交:')
        print('     git check-ignore -v config.json     # 应命中 .gitignore')
    return removed


def main():
    ap = argparse.ArgumentParser(description='open-ai 发布前隐私检查')
    ap.add_argument('--fix', action='store_true',
                    help='删除可再生的敏感文件 (logs/ data/ runtime/ *.log 及配置备份)')
    args = ap.parse_args()

    if args.fix:
        removed = do_fix()
        print('[已删除] ' + (', '.join(removed) if removed else '无'))
        print()

    problems = []
    check_worktree(problems)
    check_gitignore(problems)
    check_secrets(problems)

    if not problems:
        print('[OK] 未发现隐私 / 凭据泄漏 —— 可以提交')
        print('     (扫描 %d 个会被提交的文件; 本机真值清单: %s)'
              % (len(committable_files()),
                 '已加载' if os.path.exists(os.path.join(ROOT, LOCAL_VALUES_FILE))
                 else '缺失, 仅跑通用形态'))
        return 0

    seen, uniq = set(), []
    for kind, where, val in problems:
        if (kind, where) in seen:
            continue
        seen.add((kind, where))
        uniq.append((kind, where, val))

    print('[FAIL] 发现 %d 处问题:\n' % len(uniq))
    for kind, where, val in uniq:
        print('  - %-26s %s%s' % (kind, where, ('  →  %s' % val) if val else ''))
    print('\n处理建议:')
    print('  * 工作区敏感文件: python sanitize_check.py --fix')
    print('  * 敏感值:   换占位符 (YOUR_xxx / DEMO-KEY-NOT-REAL-…); 真值只留在本地 config.json')
    print('  * 未覆盖:   补 .gitignore 后重跑')
    print('  * 命中在检测器自己要查的字面量上: 加进 SELF_EXEMPT / BENIGN_LINE, '
          '别为了过检把检查削弱掉')
    return 1


if __name__ == '__main__':
    sys.exit(main())
