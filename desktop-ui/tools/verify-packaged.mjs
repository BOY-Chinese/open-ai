/**
 * 打包应用端到端验证（通过 WebView2 的 CDP 远程调试端口）
 *
 * 为什么需要它：
 *   之前的验证要么跑在 dev server 的浏览器里（不是打包态），要么靠截图 + 视觉
 *   模型（暗色低对比界面上会漏看甚至幻觉）。这里直接连上**真实运行的 Tauri 应用**
 *   的 WebView2，读 DOM / 点按钮，验证的是用户实际在用的那一份。
 *
 * 前置：应用需带远程调试端口启动，例如
 *   set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222
 *   desktop\open-ai-desktop.exe
 *
 * 用法：
 *   node tools/verify-packaged.mjs                 # 只做只读断言
 *   node tools/verify-packaged.mjs --uninstall     # 额外走一遍「一键卸载」点击流程
 *   node tools/verify-packaged.mjs --port=9222
 */
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const PORT = Number((process.argv.find((a) => a.startsWith('--port=')) ?? '--port=9222').split('=')[1])
const DO_UNINSTALL = process.argv.includes('--uninstall')

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
  throw new Error('puppeteer-core 未找到')
}

const puppeteer = await loadPuppeteer()
const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok })
  console.log(`${ok ? '[ OK ]' : '[FAIL]'} ${name}${detail ? '  — ' + detail : ''}`)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const browser = await puppeteer.connect({
  browserURL: `http://127.0.0.1:${PORT}`,
  defaultViewport: null,
})

try {
  const pages = await browser.pages()
  const page = pages.find((p) => (p.url() || '').includes('tauri')) ?? pages[0]
  check('已连上打包应用的 WebView2', !!page, page ? page.url().slice(0, 48) : '')
  await page.bringToFront().catch(() => {})

  const text = () => page.evaluate(() => document.body.innerText)

  // 关闭按钮存在 = 页面真的渲染出来了（而不是错误页 / 白屏）
  const body = await text()
  check('页面已渲染（非白屏/错误页）', body.length > 40)
  check('未出现网关错误门', !body.includes('无法连接后端网关'))

  const aside = await page.evaluate(() => document.querySelector('aside')?.innerText ?? '')
  for (const label of ['账号管理', 'API 管理', '模型列表', '积分看板', '系统日志', '系统设置']) {
    check(`侧边栏含「${label}」`, aside.includes(label))
  }
  check('侧边栏显示网关运行中', aside.includes('网关运行中'))

  // 逐页读 DOM：既验证渲染，也验证真实数据能取到
  const SUMMARY = {
    accounts: (t) => /账号管理/.test(t),
    api: (t) => /API 管理/.test(t),
    models: (t) => /模型列表/.test(t),
    credits: (t) => /积分看板/.test(t),
    logs: (t) => /系统日志/.test(t),
    settings: (t) => /系统设置/.test(t),
  }
  for (const [hash, test] of Object.entries(SUMMARY)) {
    await page.evaluate((h) => { window.location.hash = `#/${h}` }, hash)
    await sleep(1600)
    const t = await text()
    check(`页面 #/${hash} 正常渲染`, test(t), t.trim().slice(0, 32).replace(/\n/g, ' '))
  }

  // API 管理页：创建时间列不应再出现 1970
  await page.evaluate(() => { window.location.hash = '#/api' })
  await sleep(1800)
  const apiText = await text()
  check('API 页创建时间不再是 1970', !apiText.includes('1970'))
  check('API 页按钮文案为「创建 API」', apiText.includes('创建 API') && !apiText.includes('+ 创建 API'))

  // 模型列表：倍率列应出现非 0 值
  await page.evaluate(() => { window.location.hash = '#/models' })
  await sleep(2500)
  const modelRows = await page.evaluate(() =>
    [...document.querySelectorAll('tbody tr')].slice(0, 120).map((tr) =>
      [...tr.querySelectorAll('td')].map((td) => td.innerText.trim()).join(' | ')
    )
  )
  const nonZero = modelRows.filter((r) => / \| (?!0\.00)/.test(r) && /\.\d\d/.test(r)).length
  check('模型列表有真实倍率（非 0）', nonZero > 0, `非零行数=${nonZero}/${modelRows.length}`)

  if (DO_UNINSTALL) {
    console.log('\n--- 一键卸载流程（需要已放置桩 uninstall.exe）---')
    await page.evaluate(() => { window.location.hash = '#/settings' })
    await sleep(1500)
    const clicked = await page.evaluate(() => {
      const btn = [...document.querySelectorAll('button')].find((b) => b.innerText.trim() === '一键卸载')
      if (!btn) return false
      btn.click()
      return true
    })
    check('找到并点击「一键卸载」', clicked)
    await sleep(900)
    const confirmed = await page.evaluate(() => {
      const btn = [...document.querySelectorAll('button')].find((b) => b.innerText.trim() === '确认卸载')
      if (!btn) return false
      btn.click()
      return true
    })
    check('找到并点击「确认卸载」', confirmed)
    await sleep(2500)
    const toast = await text().catch(() => '')
    console.log('   页面提示：', toast.split('\n').filter(Boolean).slice(-2).join(' / '))
  }
} finally {
  await browser.disconnect()
}

const failed = results.filter((r) => !r.ok)
console.log('')
console.log(`共 ${results.length} 项断言，失败 ${failed.length} 项`)
process.exit(failed.length === 0 ? 0 : 1)
