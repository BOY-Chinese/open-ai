/**
 * 外观（白天 / 夜间 / 跟随系统）+ 操作日志 自检截图工具（Windows 侧运行）
 *
 * 用途：v3.0 新增的外观设置与「操作日志」页做视觉回归 ——
 *   * 两套主题下六个页面各截一遍，确认浅色主题不是「深色令牌照搬过去」（那种页面会白底白字）
 *   * 外观设置对话框的三种模式各截一遍，确认选中态与「当前生效」回显正确
 *   * 触发一次真实操作（添加账号）后进操作日志，确认埋点确实落到了页面上
 *
 * 前置条件（与 screenshot.mjs 相同）：
 *   1. dev server 在跑：`npm run dev`（默认 127.0.0.1:1420）
 *   2. tools/node_modules_tools/ 内已装 puppeteer-core
 *   3. 使用系统 Edge —— 与 Tauri 的 WebView2 同内核，渲染结果最接近真实窗口
 *
 * 用法（在 desktop-ui 目录下，Windows 的 Node 执行）：
 *   node tools/verify-appearance.mjs
 * 产物：docs/screenshots/appearance/*.png + 控制台的主题状态断言
 *
 * 为什么默认加 `?mock=1`：本工具验证的是**渲染**，不该依赖网关在线，
 * 更不该因为点了「添加 Trae 账号」而真的拉起一次浏览器登录。
 */
import { mkdirSync, rmSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const OUT = join(HERE, '..', 'docs', 'screenshots', 'appearance')
const BASE = process.env.DEV_URL ?? 'http://127.0.0.1:1420'
const URL_MOCK = `${BASE}/?mock=1`
const EDGE =
  process.env.EDGE_PATH ??
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'

/* 依赖装在 tools/node_modules_tools/ 下（Windows 侧 npm install），
   它不在本文件的模块解析路径上，故显式按绝对路径导入。 */
async function loadPuppeteer() {
  const candidates = [
    process.env.PUPPETEER_CORE_PATH,
    join(HERE, 'node_modules_tools', 'node_modules', 'puppeteer-core', 'lib', 'esm', 'puppeteer', 'puppeteer-core.js'),
    'puppeteer-core',
  ].filter(Boolean)
  for (const c of candidates) {
    try {
      const spec = c.startsWith('puppeteer-core') ? c : pathToFileURL(c).href
      const mod = await import(spec)
      return mod.default ?? mod
    } catch {
      /* 尝试下一个候选路径 */
    }
  }
  throw new Error('puppeteer-core 未找到。请在 tools/node_modules_tools 下执行 npm install。')
}

const puppeteer = await loadPuppeteer()

/** 侧边栏导航项（label 必须与 config/navigation.ts 完全一致） */
const PAGES = [
  { label: '账号管理', slug: 'accounts' },
  { label: 'API 管理', slug: 'api' },
  { label: '模型列表', slug: 'models' },
  { label: '积分看板', slug: 'credits' },
  { label: '操作日志', slug: 'logs' },
  { label: '系统设置', slug: 'settings' },
]

rmSync(OUT, { recursive: true, force: true })
mkdirSync(OUT, { recursive: true })

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: true,
  args: ['--no-sandbox', '--disable-gpu'],
})

const page = await browser.newPage()
await page.setCacheEnabled(false)
await page.setViewport({ width: 1180, height: 780, deviceScaleFactor: 1 })

const errors = []
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(`[console] ${m.text()}`)
})
page.on('pageerror', (e) => errors.push(`[pageerror] ${e.message}`))

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

/** 当前主题的客观状态（不靠肉眼，靠计算样式） */
const readThemeState = () =>
  page.evaluate(() => {
    const cs = getComputedStyle(document.body)
    return {
      hasDarkClass: document.documentElement.classList.contains('dark'),
      bodyBg: cs.backgroundColor,
      bodyFg: cs.color,
      stored: localStorage.getItem('open-ai.theme'),
    }
  })

const gotoPage = async (label) => {
  const ok = await page.evaluate((l) => {
    const btns = [...document.querySelectorAll('aside nav button')]
    const target = btns.find((b) => b.textContent?.trim() === l)
    if (target) {
      target.click()
      return true
    }
    return false
  }, label)
  if (!ok) throw new Error(`未找到导航项：${label}`)
  await wait(1200)
}

const shoot = async (name) => {
  await page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log(`  [截图] ${name}.png`)
}

/**
 * 切到「积分看板 → 每周情况」并截图。
 *
 * 单独抽出来是因为堆叠柱状图的颜色**不走 CSS 变量**（recharts 的 SVG fill
 * 需要可直接解析的颜色，见 config/chart.ts），主题切换时必须靠 React 重渲染
 * 才会换成另一套色板 —— 这是唯一一处「主题换了但颜色不一定跟着换」的地方，
 * 必须两套主题各截一次人工比对。
 */
const captureWeekChart = async (theme) => {
  await gotoPage('积分看板')
  const clicked = await page.evaluate(() => {
    const t = [...document.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === '每周情况'
    )
    if (!t) return false
    t.click()
    return true
  })
  if (!clicked) throw new Error('未找到「每周情况」切换按钮')
  await wait(1500)
  const fills = await page.evaluate(() =>
    [...document.querySelectorAll('.recharts-bar-rectangle path')]
      .slice(0, 3)
      .map((p) => p.getAttribute('fill'))
  )
  await shoot(`${theme}-credits-week`)
  return fills
}

const results = []
const check = (name, pass, detail) => {
  results.push({ name, pass, detail })
  console.log(`  ${pass ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
}

/* ═══════════ 1. 首次打开：默认必须是「白天（浅色）」 ═══════════ */
console.log('\n=== 1. 首次打开（无 localStorage）默认主题 ===')
await page.goto(URL_MOCK, { waitUntil: 'networkidle2', timeout: 60000 })
await page.evaluate(() => localStorage.clear())
await page.reload({ waitUntil: 'networkidle2', timeout: 60000 })
await wait(1200)

let st = await readThemeState()
check('默认无 dark class（白天）', st.hasDarkClass === false, `stored=${st.stored}`)
check('body 背景是浅色', st.bodyBg === 'rgb(240, 242, 244)', `bg=${st.bodyBg} fg=${st.bodyFg}`)

/* ═══════════ 2. 浅色主题下六个页面 ═══════════ */
console.log('\n=== 2. 浅色主题：六页截图 ===')
for (const p of PAGES) {
  await gotoPage(p.label)
  await shoot(`light-${p.slug}`)
}
const lightFills = await captureWeekChart('light')

/* ═══════════ 3. 外观设置对话框 ═══════════ */
console.log('\n=== 3. 外观设置对话框 ===')
await gotoPage('系统设置')
await page.evaluate(() => {
  const btns = [...document.querySelectorAll('button')]
  btns.find((b) => b.textContent?.trim() === '外观设置')?.click()
})
await wait(600)
await shoot('dialog-appearance-light')

const dialogOk = await page.evaluate(() => {
  const rg = document.querySelector('[role="radiogroup"]')
  const checked = [...document.querySelectorAll('input[name="open-ai-theme-mode"]')].find(
    (i) => i.checked
  )
  return {
    hasDialog: !!document.querySelector('[role="dialog"]'),
    radioGroup: !!rg,
    checkedValue: checked?.id ?? null,
    options: [...document.querySelectorAll('input[name="open-ai-theme-mode"]')].map((i) => i.id),
    effectiveText: document.querySelector('[role="dialog"]')?.textContent ?? '',
  }
})
check('对话框已打开', dialogOk.hasDialog)
check('radiogroup 语义存在', dialogOk.radioGroup)
check('三项齐全', dialogOk.options.length === 3, dialogOk.options.join(', '))
check('默认选中 light', dialogOk.checkedValue === 'theme-mode-light', `checked=${dialogOk.checkedValue}`)
check('回显当前生效=白天', dialogOk.effectiveText.includes('当前生效：白天'))

/* ═══════════ 4. 切到「夜间」 ═══════════ */
console.log('\n=== 4. 切换到夜间 ===')
await page.evaluate(() => document.getElementById('theme-mode-dark')?.click())
await wait(500)
st = await readThemeState()
check('已加 dark class', st.hasDarkClass === true)
check('body 背景变深', st.bodyBg === 'rgb(15, 15, 15)', `bg=${st.bodyBg} fg=${st.bodyFg}`)
check('已持久化 dark', st.stored === 'dark', `stored=${st.stored}`)
await shoot('dialog-appearance-dark')
await page.keyboard.press('Escape')
await wait(400)
await shoot('dark-settings')

/* ═══════════ 5. 深色主题下六个页面 ═══════════ */
console.log('\n=== 5. 深色主题：六页截图 ===')
for (const p of PAGES) {
  await gotoPage(p.label)
  await shoot(`dark-${p.slug}`)
}
const darkFills = await captureWeekChart('dark')
check(
  '柱状图配色随主题切换',
  lightFills.length > 0 && darkFills.length > 0 && lightFills.join() !== darkFills.join(),
  `浅色 ${lightFills.join(' / ')} vs 深色 ${darkFills.join(' / ')}`
)

/* ═══════════ 6. 跟随系统 ═══════════ */
console.log('\n=== 6. 跟随系统 ===')
await gotoPage('系统设置')
await page.evaluate(() => {
  const btns = [...document.querySelectorAll('button')]
  btns.find((b) => b.textContent?.trim() === '外观设置')?.click()
})
await wait(500)
await page.evaluate(() => document.getElementById('theme-mode-system')?.click())
await wait(400)
st = await readThemeState()
const sysDark = await page.evaluate(() =>
  window.matchMedia('(prefers-color-scheme: dark)').matches
)
check(
  '跟随系统：dark class 与系统偏好一致',
  st.hasDarkClass === sysDark,
  `hasDarkClass=${st.hasDarkClass} systemDark=${sysDark}`
)
check('已持久化 system', st.stored === 'system', `stored=${st.stored}`)

/* EOF 验证「跟随系统」的实时性：模拟系统偏好翻转 */
await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }])
await wait(400)
const afterLight = await readThemeState()
await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }])
await wait(400)
const afterDark = await readThemeState()
check(
  '系统偏好翻转时实时跟随',
  afterLight.hasDarkClass === false && afterDark.hasDarkClass === true,
  `light→${afterLight.hasDarkClass} dark→${afterDark.hasDarkClass}`
)
await page.emulateMediaFeatures([])

/* 回到白天，便于接下来的操作日志在浅色下截图 */
await page.evaluate(() => document.getElementById('theme-mode-light')?.click())
await wait(400)
await page.keyboard.press('Escape')
await wait(400)

/* ═══════════ 7. 操作日志埋点 ═══════════ */
console.log('\n=== 7a. 操作日志：普通写操作（代理埋点）===')
await gotoPage('操作日志')
const beforeCount = await page.evaluate(
  () => document.querySelector('[role="log"]')?.textContent?.length ?? 0
)

// 用「刷新账号」做样本：它是同步完成的一次写操作，能干净地验证代理埋点
await gotoPage('账号管理')
await page.evaluate(() => {
  const btns = [...document.querySelectorAll('button')]
  btns.find((b) => b.textContent?.trim() === '刷新账号')?.click()
})
await wait(2000)

await gotoPage('操作日志')
const logText = await page.evaluate(
  () => document.querySelector('[role="log"]')?.textContent ?? ''
)
check('操作日志非空（埋点已生效）', logText.length > beforeCount, `len ${beforeCount} → ${logText.length}`)
check('含分隔线', logText.includes('─────'))
check('操作标题带领域标签', /\[账号\]|\[API\]|\[模型\]|\[设置\]|\[更新\]/.test(logText),
  (logText.match(/\[(账号|API|模型|设置|更新)\][^\n]{0,30}/) ?? [])[0] ?? '')
check('含参数摘要', /\[参数\]/.test(logText))
check('含完成标记', /\[完成\] \d{2}:\d{2}:\d{2}（耗时/.test(logText))
await shoot('light-logs')

/* ═══════════ 7b. 登录脚本输出实时跟随（v2.3 _subprocess_stream 等价物）═══════════ */
console.log('\n=== 7b. 登录脚本输出跟随 ===')
await gotoPage('账号管理')
const rowsBeforeLogin = await page.evaluate(() => document.querySelectorAll('tbody tr').length)
await page.evaluate(() => {
  const btns = [...document.querySelectorAll('button')]
  btns.find((b) => b.textContent?.trim().startsWith('添加 WorkBuddy 账号'))?.click()
})
await wait(700)
// 按钮应进入「登录中…」并禁用：防连点（连点会顶掉网关侧 Popen 句柄）
const runningBtn = await page.evaluate(() => {
  const b = [...document.querySelectorAll('button')].find((x) =>
    x.textContent?.includes('登录中')
  )
  return b ? { label: b.textContent.trim(), disabled: b.disabled } : null
})
check('添加按钮进入「登录中…」且禁用', !!runningBtn && runningBtn.disabled === true,
  runningBtn ? runningBtn.label : '未找到')

// 趁脚本还在跑，进日志页看「进行中」的样子
await gotoPage('操作日志')
const midText = await page.evaluate(
  () => document.querySelector('[role="log"]')?.textContent ?? ''
)
await shoot('light-logs-login-running')
check('跟随中已出现脚本输出', /登录助手（login_workbuddy\.py）/.test(midText))
// 形态判据：最后一次「添加账号」之后还没写 [完成] —— 说明这个操作块仍是打开的
const midOpen = midText.slice(midText.lastIndexOf('[账号] 添加账号'))
check('跟随中该操作块尚未收尾（无 [完成]）', !midOpen.includes('[完成]'),
  `该块长度 ${midOpen.length}`)

// 等脚本跑完（演示脚本 5 片 × 1s）
await wait(9000)
const doneText = await page.evaluate(
  () => document.querySelector('[role="log"]')?.textContent ?? ''
)
await shoot('light-logs-login-done')

// 顺序断言：这是本次改动的核心 —— 一个操作块，[完成] 必须在脚本输出**之后**
const iTitle = doneText.lastIndexOf('[账号] 添加账号')
const iHelper = doneText.lastIndexOf('登录助手（login_workbuddy.py）')
const iTail = doneText.lastIndexOf('账号已写入 config.json')
const iHint = doneText.lastIndexOf('[提示] 登录成功后新账号即可参与轮询')
const iDone = doneText.lastIndexOf('[完成]')
check(
  '登录跟随：标题 → 助手 → 脚本输出 → 收尾提示 → [完成] 的顺序正确',
  iTitle >= 0 && iTitle < iHelper && iHelper < iTail && iTail < iHint && iHint < iDone,
  `idx 标题=${iTitle} 助手=${iHelper} 输出=${iTail} 提示=${iHint} 完成=${iDone}`
)
check('脚本输出带演示内容', doneText.includes('[演示') )
check('登录开始提示为 v2.3 风格', />>> 即将打开 WorkBuddy 网页登录/.test(doneText))

// 脚本结束后应自动刷新账号列表（v2.3 的 refresh_account_list 等价物）
await gotoPage('账号管理')
await wait(1200)
const rowsAfterLogin = await page.evaluate(() => document.querySelectorAll('tbody tr').length)
check(
  '登录结束后账号列表自动刷新（多出新账号）',
  rowsAfterLogin > rowsBeforeLogin,
  `${rowsBeforeLogin} → ${rowsAfterLogin} 行`
)
await shoot('light-accounts-after-login')
check('底部按钮已从「登录中…」恢复', await page.evaluate(() =>
  [...document.querySelectorAll('button')].some((b) =>
    b.textContent?.trim().startsWith('添加 WorkBuddy 账号')
  )
))

/* 关键词搜索 + 级别筛选各截一张，确认工具栏仍然可用 */
await gotoPage('操作日志')
await page.keyboard.down('Control')
await page.keyboard.press('KeyF')
await page.keyboard.up('Control')
await wait(400)
await page.type('input[aria-label="搜索操作日志内容"]', '登录助手')
await wait(600)
const matchBadge = await page.evaluate(() => ({
  hasBadge: /\d+ \/ \d+/.test(document.body.textContent ?? ''),
}))
check('搜索命中并显示计数', matchBadge.hasBadge)
await shoot('light-logs-search')

await page.keyboard.press('Escape')
await wait(300)

/* ═══════════ 8. 模型列表本地缓存 ═══════════ */
console.log('\n=== 8. 模型列表本地缓存 ===')
const cacheProbe = await page.evaluate(() => {
  const raw = localStorage.getItem('open-ai.models-cache.v1')
  if (!raw) return { cached: false }
  const parsed = JSON.parse(raw)
  return {
    cached: true,
    total: parsed.list?.length ?? 0,
    savedAt: parsed.savedAt,
    hasHidden: (parsed.list ?? []).some((m) => m.hidden),
    channels: [...new Set((parsed.list ?? []).map((m) => m.channel))].sort(),
  }
})
check('已写入模型缓存', cacheProbe.cached, `共 ${cacheProbe.total} 条`)
check('缓存含隐藏项（全量而非当前视图）', cacheProbe.hasHidden === true)
check('缓存覆盖三通道', (cacheProbe.channels ?? []).length === 3, (cacheProbe.channels ?? []).join(','))

/* 重新进入模型列表：应立刻有行，且不再出现骨架屏 */
await gotoPage('账号管理')
await wait(300)
await gotoPage('模型列表')
const instantRows = await page.evaluate(() => ({
  rows: document.querySelectorAll('tbody tr').length,
  skeleton: document.querySelectorAll('.skeleton').length,
}))
check('二次进入模型列表首帧即有数据', instantRows.rows > 0, `rows=${instantRows.rows} skeleton=${instantRows.skeleton}`)
await shoot('light-models-cached')

/* ═══════════ 9. 每日签到列 ═══════════ */
console.log('\n=== 9. 每日签到列（只读状态）===')
await gotoPage('账号管理')
const signinProbe = await page.evaluate(() => {
  const text = document.querySelector('[role="table"]')?.textContent ?? document.body.textContent ?? ''
  return {
    hasChecked: text.includes('已签到'),
    hasUnchecked: text.includes('未签到'),
    checkboxes: document.querySelectorAll('[role="checkbox"], input[type="checkbox"]').length,
  }
})
check('出现「已签到」', signinProbe.hasChecked)
check('出现「未签到」（未签到态也有渲染路径）', signinProbe.hasUnchecked)
check('表格内不再有签到勾选框', signinProbe.checkboxes === 0, `checkbox=${signinProbe.checkboxes}`)

/* ═══════════ 汇总 ═══════════ */
console.log('\n=== 运行时错误 ===')
console.log(errors.length ? errors.slice(0, 12).join('\n') : '  无')

const failed = results.filter((r) => !r.pass)
console.log(`\n=== 断言汇总：${results.length - failed.length}/${results.length} 通过 ===`)
for (const f of failed) console.log(`  ✗ ${f.name}${f.detail ? ` — ${f.detail}` : ''}`)

await browser.close()
process.exit(failed.length > 0 || errors.length > 0 ? 1 : 0)
