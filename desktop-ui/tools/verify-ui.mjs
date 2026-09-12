/**
 * UI 断言自检（Windows 侧运行）—— 用 DOM 文本代替「看图猜」
 *
 * 为什么需要它：
 *   截图只能靠人眼/视觉模型判断，暗色低对比界面上极易漏看或看出幻觉
 *   （本项目已发生：视觉模型把 5 个账号表格读对了，却把侧边栏底部
 *     「系统日志 / 系统设置 / 网关运行中 / 版本号」整段漏报为空白）。
 *   DOM 断言是确定性的：有没有这段文字，一问便知。
 *
 * 前置：dev server 运行中（npm run dev，127.0.0.1:1420）
 * 用法：node tools/verify-ui.mjs
 * 退出码：0 = 全部通过；1 = 有断言失败
 */
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const URL_BASE = process.env.DEV_URL ?? 'http://127.0.0.1:1420'
const EDGE =
  process.env.EDGE_PATH ??
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'

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
      /* 尝试下一个候选 */
    }
  }
  throw new Error('puppeteer-core 未找到（tools/node_modules_tools 下需 npm install）')
}

const puppeteer = await loadPuppeteer()

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: 'new',
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
})

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail })
  console.log(`${ok ? '[ OK ]' : '[FAIL]'} ${name}${detail ? '  — ' + detail : ''}`)
}

try {
  const page = await browser.newPage()
  await page.setCacheEnabled(false)
  await page.setViewport({ width: 1180, height: 780 })

  const errors = []
  page.on('pageerror', (e) => errors.push('pageerror: ' + e))
  page.on('console', (m) => {
    // "Failed to load resource" 由下面的 response 钩子带 URL 精确记录，
    // 这里不重复收集（否则 favicon 这类无关 404 会以无信息量的文案混进来）
    if (m.type() === 'error' && !m.text().includes('Failed to load resource')) {
      errors.push('console: ' + m.text())
    }
  })
  // 记录 404/500 的具体 URL：只报「有一个资源失败」等于没报
  page.on('response', (r) => {
    if (r.status() >= 400 && !r.url().includes('favicon')) {
      errors.push(`http ${r.status()}: ${r.url()}`)
    }
  })

  await page.goto(`${URL_BASE}/#/accounts`, { waitUntil: 'networkidle2', timeout: 30000 })
  // 等页面挂载（网关门会在就绪后才渲染侧边栏内容）
  await page.waitForSelector('aside', { timeout: 15000 })
  await new Promise((r) => setTimeout(r, 2500))

  const snap = await page.evaluate(() => {
    const aside = document.querySelector('aside')
    const main = document.querySelector('main')
    const rows = document.querySelectorAll('tbody tr')
    return {
      asideText: aside ? aside.innerText : '',
      mainText: main ? main.innerText : '',
      rowCount: rows.length,
      bodyText: document.body.innerText,
    }
  })

  for (const label of ['账号管理', 'API 管理', '模型列表', '积分看板', '系统日志', '系统设置']) {
    check(`侧边栏含导航「${label}」`, snap.asideText.includes(label))
  }
  check('侧边栏含网关状态「网关运行中」', snap.asideText.includes('网关运行中'),
    snap.asideText.includes('网关已断开') ? '实际显示「网关已断开」' : '')
  check('侧边栏含版本号 v3.0-dev', snap.asideText.includes('v3.0-dev'))

  check('未出现网关错误门', !snap.bodyText.includes('无法连接后端网关'))
  check('未出现网关连接中', !snap.bodyText.includes('正在连接后端网关'))

  check('账号管理页有数据行', snap.rowCount > 0, `行数=${snap.rowCount}`)
  check('账号管理页标题正确', snap.mainText.includes('账号管理'))

  // 逐页切换，确认每个页面都渲染出自身标题（同时暴露白屏/报错）
  for (const [hash, title] of [
    ['api', 'API 管理'],
    ['models', '模型列表'],
    ['credits', '积分看板'],
    ['logs', '系统日志'],
    ['settings', '系统设置'],
  ]) {
    await page.goto(`${URL_BASE}/#/${hash}`, { waitUntil: 'networkidle2', timeout: 30000 })
    await new Promise((r) => setTimeout(r, 1500))
    const t = await page.evaluate(() => document.querySelector('main')?.innerText ?? '')
    check(`页面 #/${hash} 渲染「${title}」`, t.includes(title),
      t.trim().slice(0, 40).replace(/\n/g, ' '))
  }

  check('无控制台错误', errors.length === 0, errors.slice(0, 2).join(' | '))
} finally {
  await browser.close()
}

const failed = results.filter((r) => !r.ok)
console.log('')
console.log(`共 ${results.length} 项断言，失败 ${failed.length} 项`)
process.exit(failed.length === 0 ? 0 : 1)
