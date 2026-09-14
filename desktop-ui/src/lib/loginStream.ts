/**
 * 登录脚本输出跟随 —— v2.3 `gui_account_manager._subprocess_stream()` 的 v3.0 等价物
 *
 * v2.3 的做法：把 `login_*.py` 用 `subprocess.Popen(stdout=PIPE)` 拉起来，
 * 逐行读出来塞进操作日志队列，于是「添加账号」那一段日志会实时滚出脚本的进度。
 *
 * v3.0 的界面与网关是两个进程，脚本由**网关**拉起（`POST /accounts/login`），
 * 输出重定向到 `logs/login_<通道>.log`。所以这里用「轮询增量读文件」等价实现：
 *   * 后端 `GET /accounts/login/log?channel=&offset=` 返回自 offset 起的新增内容
 *     与「脚本还在跑吗」（靠 Popen 句柄判断，不是猜 mtime）；
 *   * 前端从这里拿到分片，按行贴进操作日志。
 *
 * 为什么是轮询而不是 SSE/WebSocket：输出节奏是「人完成一次网页登录」，
 * 一秒一次足够；为一条低频链路引入长连接，反而要在网关侧管理连接生命周期
 * 与断线重连。增量偏移已经把轮询成本压到「读几十字节」。
 */
import { append } from '@/lib/oplog'
import type { LogLevel } from '@/types/domain'

/** 登录脚本输出分片（与后端 `/accounts/login/log` 返回一一对应） */
export interface LoginLogChunk {
  text: string
  offset: number
  running: boolean
  exists: boolean
  exitCode?: number | null
}

/** 轮询间隔 */
const POLL_MS = 1000

/**
 * 单次跟随的时长上限（30 分钟）。
 *
 * 必须有：用户在浏览器里把登录页晾着不动、或干脆去睡觉，脚本可能挂到天荒地老。
 * 没有上限就会变成一个永不停止的定时器，在界面背后一直发请求。
 */
const MAX_FOLLOW_MS = 30 * 60 * 1000

/**
 * 按内容猜日志级别（仅用于着色与「只看失败」筛选，不改变文本）。
 *
 * v2.3 的 ScrolledText 是单色的；这里给一点着色是纯增益 —— 登录脚本会打印
 * 大量中间步骤，失败那几行能一眼扫到，比通篇同色实用。
 *
 * 判据刻意偏保守：**不**单凭「完成」二字判成功 ——
 * 实测踩到过「等待用户在浏览器中完成登录 ...」被染成绿色，
 * 而那一行的意思恰恰是「还没完成」。失败侧则放宽（宁可错染红，不要漏染）。
 */
function levelOf(line: string): LogLevel {
  const s = line.toLowerCase()
  if (/\[错误\]|失败|出错|error|exception|traceback|timeout|超时/.test(s)) return 'error'
  if (/\[完成\]|成功|success|\[ok\]|已写入|已保存/.test(s)) return 'success'
  if (/警告|warn|重试|retry|降级/.test(s)) return 'warn'
  return 'info'
}

/** 把分片切成待写入的行：丢弃空行，保留行内缩进（脚本输出靠缩进表达层级） */
export function splitLogLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((l) => l.replace(/\s+$/, ''))
    .filter((l) => l.length > 0)
}

export interface FollowLoginLogOptions {
  /** 取一段新输出；由调用方闭包持有通道与鉴权细节 */
  fetchChunk: (offset: number) => Promise<LoginLogChunk>
  /** 起始偏移（POST /accounts/login 返回的 logOffset），保证只跟本次登录 */
  startOffset: number
  /** 脚本标识，用于提示文案，如 'Trae 登录助手 (login_trae.py)' */
  label: string
  /** 脚本结束（或跟随超时）后回调，用于刷新账号列表 */
  onFinished?: () => void
}

/**
 * 跟随一段登录脚本输出直到它结束。
 *
 * 始终 resolve（不抛）：调用方通常是 fire-and-forget 的后台跟随，
 * 抛出没人接只会变成 unhandledrejection；失败已经写进操作日志了，那里才是出口。
 */
export async function followLoginLog(opts: FollowLoginLogOptions): Promise<void> {
  const { fetchChunk, startOffset, label, onFinished } = opts
  const deadline = Date.now() + MAX_FOLLOW_MS

  append('info', label)
  let offset = startOffset
  let consecutiveErrors = 0

  for (;;) {
    if (Date.now() > deadline) {
      append('warn', `[提示] 已等待超过 ${MAX_FOLLOW_MS / 60000} 分钟，停止跟随登录输出（脚本可能仍在后台运行）`)
      break
    }

    let chunk: LoginLogChunk
    try {
      chunk = await fetchChunk(offset)
      consecutiveErrors = 0
    } catch (e) {
      consecutiveErrors += 1
      // 连续 5 次失败才放弃：网关刚被重启时会有短暂的连接失败，一次就退出太脆
      if (consecutiveErrors >= 5) {
        append('error', `[错误] 读取登录输出失败（已重试 5 次）：${e instanceof Error ? e.message : String(e)}`)
        break
      }
      await sleep(POLL_MS)
      continue
    }

    for (const line of splitLogLines(chunk.text)) append(levelOf(line), line)
    offset = chunk.offset

    if (!chunk.running) {
      // 收尾提示沿用 v2.3 的口径：登录成功与否由「账号列表里有没有新账号」回答
      append('info', '[提示] 登录成功后新账号即可参与轮询')
      const code = chunk.exitCode
      if (typeof code === 'number' && code !== 0) {
        append('warn', `[提示] 登录脚本退出码 ${code}（非 0 通常表示登录未完成或被取消）`)
      }
      break
    }

    await sleep(POLL_MS)
  }

  onFinished?.()
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}
