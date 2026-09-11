/**
 * UI 自检截图工具（Windows 侧运行）
 *
 * 用途：遍历六个页面 + 积分周视图并截图，用于视觉回归与交付验收。
 *
 * 前置条件：
 *   1. dev server 正在运行：`npm run dev`（默认 127.0.0.1:1420）
 *   2. 依赖已装：tools/node_modules_tools/ 内（Windows 侧 npm install）
 *   3. 使用系统 Edge —— 与 Tauri 的 WebView2 同内核，渲染结果最接近真实窗口
 *
 * 用法（在 desktop-ui 目录下，Windows 的 Node 执行）：
 *   node tools/screenshot.mjs
 * 产物：docs/screenshots/0X-*.png
 *
 * 设计要点：
 *   - setCacheEnabled(false)：避免命中旧模块缓存
 *   - 各页截图 SHA 应互不相同；若相同说明页面未真正切换（多为 dev server 未热更新）
 */
import { mkdirSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const OUT = join(HERE, '..', 'docs', 'screenshots')
const URL = process.env.DEV_URL ?? 'http://127.0.0.1:1420'
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
  throw new Error(
    'puppeteer-core 未找到。请在 tools/node_modules_tools 下执行 npm install。'
  )
}

const puppeteer = await loadPuppeteer()

/* 侧边栏导航项与其产物文件名 */
const PAGES = [
  { label: '账号管理', file: '01-accounts' },
  { label: 'API 管理', file: '02-api' },
  { label: '模型列表', file: '03-models' },
  { label: '积分看板', file: '04-credits' },
  { label: '系统日志', file: '05-logs' },
  { label: '系统设置', file: '06-settings' },
]

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

await page.goto(URL, { waitUntil: 'networkidle2', timeout: 60000 })
await new Promise((r) => setTimeout(r, 1200))

for (const p of PAGES) {
  const ok = await page.evaluate((label) => {
    const btns = [...document.querySelectorAll('aside nav button')]
    const target = btns.find((b) => b.textContent?.trim() === label)
    if (target) {
      target.click()
      return true
    }
    return false
  }, p.label)

  if (!ok) {
    console.log(`  [跳过] 未找到导航项: ${p.label}`)
    continue
  }

  // 等待 mock 数据加载完成（backend 有约 320ms 模拟延迟）
  await new Promise((r) => setTimeout(r, 1400))
  await page.screenshot({ path: join(OUT, `${p.file}.png`) })
  console.log(`  [OK] ${p.label} -> ${p.file}.png`)
}

/* 补充：积分看板的每周情况（含堆叠柱状图） */
await page.evaluate(() => {
  const btns = [...document.querySelectorAll('aside nav button')]
  btns.find((b) => b.textContent?.trim() === '积分看板')?.click()
})
await new Promise((r) => setTimeout(r, 1000))

const weekClicked = await page.evaluate(() => {
  const t = [...document.querySelectorAll('button')].find(
    (b) => b.textContent?.trim() === '每周情况'
  )
  if (t) {
    t.click()
    return true
  }
  return false
})

if (weekClicked) {
  await new Promise((r) => setTimeout(r, 1800))
  await page.screenshot({ path: join(OUT, '07-credits-week.png') })
  console.log('  [OK] 积分看板·每周情况 -> 07-credits-week.png')
}

console.log('\n=== 运行时错误 ===')
console.log(errors.length ? errors.slice(0, 10).join('\n') : '  无')

await browser.close()
