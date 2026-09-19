/**
 * 操作日志埋点 —— 包在数据源外面，凡是「会改状态」的调用自动记账
 *
 * 为什么放在数据源这一层，而不是在页面里逐个写 `log(...)`
 * ----------------------------------------------------
 * 页面里的写操作有十几处（添加账号、刷新积分、重连、删密钥、改模型可见性…），
 * 逐处手写日志一定会漏 —— 漏掉的那处恰好就是用户下次要查的那处，
 * 而且「后来新加的操作忘了记日志」是这种设计必然的退化方式。
 *
 * 把埋点做成一圈代理（Proxy）包在 `backend` 之外则相反：
 * **新增数据源方法时，要么忘了在 {@link MUTATIONS} 里登记而被这条规则挡住
 * （下面有开发期告警），要么一登记就自动获得完整记账**，不会静默漏记。
 *
 * 只读方法（listAccounts / getToday / …）刻意不记：日志页一打开就会调它们，
 * 记进去只会用噪声把真正的操作淹掉。这与 v2.3 的行为一致 —— 那里的
 * `_run()` 只包住「用户按下按钮」的动作。
 */
import type { AutoChain, Channel, ChannelFilter, ModelEntry } from '@/types/domain'
import { append, runOperation } from '@/lib/oplog'

/**
 * 会改状态的方法 → 操作标签 + 中文操作名 + 参数摘要
 *
 * 标签沿用 v2.3 GUI 的词汇（`[账号]` / `[API]` / `[模型]` / `[设置]` / `[更新]`）：
 * 日志一长，靠标签扫比靠读整句快得多，也是那一版用惯了的形状。
 */
const MUTATIONS: Record<
  string,
  { tag: string; title: string; detail?: (args: unknown[]) => string }
> = {
  addAccount: {
    tag: '账号',
    title: '添加账号',
    detail: (a) => {
      const ch = a[0] as Channel
      // 提示语沿用 v2.3 的 `>>> … <<<` 写法：这一步是「交给用户去浏览器操作」，
      // 需要一眼从连续输出里跳出来
      return `通道=${ch}   >>> 即将打开 ${ch} 网页登录，请在浏览器中完成登录 <<<`
    },
  },
  refreshAccounts: {
    tag: '账号',
    title: '刷新账号积分',
    detail: (a) => `通道=${(a[0] as ChannelFilter) ?? 'all'}`,
  },
  refreshSignin: {
    tag: '账号',
    title: '补签未签到账号',
    detail: (a) => `通道=${(a[0] as ChannelFilter) ?? 'all'}${a[1] ? '（强制重签）' : ''}`,
  },
  reconnectAccounts: {
    tag: '账号',
    title: '重新连接账号',
    detail: (a) => `通道=${(a[0] as ChannelFilter) ?? 'all'}`,
  },
  toggleAccount: {
    tag: '账号',
    title: '切换账号启用状态',
    detail: (a) => `账号=${String(a[0])}`,
  },
  deleteAccount: {
    tag: '账号',
    title: '删除账号',
    detail: (a) => `账号=${String(a[0])}`,
  },
  createApiKey: { tag: 'API', title: '新建密钥' },
  renameApiKey: {
    tag: 'API',
    title: '重命名密钥',
    detail: (a) => `新名称=${String(a[1])}`,
  },
  deleteApiKey: {
    tag: 'API',
    title: '删除密钥',
    detail: (a) => `密钥=${String(a[1] ?? a[0])}`,
  },
  refreshModels: { tag: '模型', title: '拉取最新模型列表及积分倍率' },
  refreshCredits: { tag: '积分', title: '采集最新积分流水' },
  updateModels: {
    tag: '模型',
    title: '修改模型可见性 / 置顶',
    detail: (a) => {
      const ids = (a[0] as string[]) ?? []
      const patch = (a[1] as Partial<ModelEntry>) ?? {}
      const what: string[] = []
      if (patch.hidden !== undefined) what.push(patch.hidden ? '隐藏' : '显示')
      if (patch.pinned !== undefined) what.push(patch.pinned ? '置顶' : '取消置顶')
      return `模型 ${ids.length} 个 → ${what.join(' / ') || '无字段变更'}`
    },
  },
  setAutostart: {
    tag: '设置',
    title: '设置开机自启动',
    detail: (a) => (a[0] ? '开启' : '关闭'),
  },
  saveAutoChains: {
    tag: 'Auto路由链',
    title: '保存路由链配置',
    detail: (a) => {
      const chains = (a[0] as AutoChain[] | undefined) ?? []
      const on = chains.filter((c) => c.enabled).length
      return `共 ${chains.length} 条（启用 ${on} / 关闭 ${chains.length - on}）`
    },
  },
  createAutoChain: {
    tag: 'Auto路由链',
    title: '添加新路由链',
  },
  checkAutoChain: {
    tag: 'Auto路由链',
    title: '检查路由链连通性',
    detail: (a) => `链=${String(a[0])}${a[1] ? ` 模型=${String(a[1])}` : '（整条链）'}`,
  },
  sendLoomyDesktopCode: {
    tag: '账号',
    title: '发送 Loomy 短信验证码',
    detail: (a) => `手机号=${String(a[0])}`,
  },
  loomyDesktopLogin: {
    tag: '账号',
    title: '添加 Loomy 账号（手机号验证码登录）',
    detail: (a) => `手机号=${String(a[0])}`,
  },
  sendLoomyCode: {
    tag: '账号',
    title: '发送 Loomy 网页版短信验证码',
    detail: (a) => `手机号=${String(a[0])}`,
  },
  loomyWebLogin: {
    tag: '账号',
    title: '添加 Loomy 网页版账号',
    detail: (a) => `手机号=${String(a[0])}`,
  },
  checkUpdate: { tag: '更新', title: '检查更新' },
  startUpdateDownload: { tag: '更新', title: '下载更新包' },
}

/** 包装后已登记的方法名，用于开发期自检（见下方 withOpLog 末尾） */
const LOGGED = new Set(Object.keys(MUTATIONS))

/**
 * 给数据源套上操作日志代理。
 *
 * `this` 绑定说明：调用时用 `fn.apply(target, args)` 而不是 `proxy[prop](...)`，
 * 让方法内部的 `this.listAccounts(...)` 落到**原始对象**上 —— 否则
 * `refreshAccounts` 里那句内部调用会被再记一笔「刷新账号积分」，
 * 同一次点击在日志里出现两遍。
 */
export function withOpLog<T extends object>(impl: T): T {
  return new Proxy(impl, {
    get(target, prop, receiver) {
      const value = Reflect.get(target, prop, receiver)
      if (typeof value !== 'function') return value

      const name = String(prop)
      const meta = MUTATIONS[name]
      if (!meta) return value.bind(target)

      return (...args: unknown[]) => {
        const detail = meta.detail?.(args)
        // 记账要包住「调用 + 打印摘要」两步：先打印摘要再执行，日志顺序才符合
        // 「我做了什么 → 结果如何」的阅读顺序（v2.3 的 _run 也是先打分隔线再跑）
        return runOperation(`[${meta.tag}] ${meta.title}`, async () => {
          if (detail) append('info', `[参数] ${detail}`)
          const fn = Reflect.get(target, prop) as (...a: unknown[]) => unknown
          return await fn.apply(target, args)
        })
      }
    },
  })
}

/**
 * 开发期自检：数据源上出现了「看起来会改状态、却没登记」的新方法时告警。
 *
 * 只在 DEV 下跑，且只报不拦 —— 误报（例如某个只读方法恰好叫 setXxx）
 * 不该让页面起不来，但值得让开发者在控制台看见。
 */
export function warnUnloggedMutations(impl: object): void {
  if (!import.meta.env.DEV) return
  const suspicious = /^(add|create|update|delete|remove|set|toggle|refresh|reconnect|rename|reset|clear|run|start|stop)/
  const missing = Object.keys(impl).filter(
    (k) => suspicious.test(k) && !LOGGED.has(k)
  )
  if (missing.length > 0) {
    console.warn(
      '[oplog] 以下数据源方法看起来会改状态，但未登记操作日志，用户的操作将不留痕：',
      missing
    )
  }
}
