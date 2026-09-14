import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'
const HERE = dirname(fileURLToPath(import.meta.url))
const mod = await import(pathToFileURL(join(HERE,'node_modules_tools','node_modules','puppeteer-core','lib','esm','puppeteer','puppeteer-core.js')).href)
const puppeteer = mod.default ?? mod
const b = await puppeteer.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: 'new', args:['--no-sandbox'] })
const p = await b.newPage()
await p.setCacheEnabled(false)
await p.goto('http://127.0.0.1:1420/#/models', { waitUntil: 'networkidle2', timeout: 30000 })
await new Promise(r => setTimeout(r, 3000))
const out = await p.evaluate(() => {
  const rows = [...document.querySelectorAll('tbody tr')]
  const prefs = localStorage.getItem('open-ai.model-prefs') || '(空)'
  return {
    prefsHead: prefs.slice(0, 400),
    rows: rows.slice(0, 40).map(tr => {
      const td = [...tr.querySelectorAll('td')].map(x => x.innerText.trim())
      return td.join(' | ')
    }),
  }
})
console.log('--- localStorage prefs ---'); console.log(out.prefsHead)
console.log('--- 模型表格（前 40 行）---'); out.rows.forEach(r => console.log(' ', r))
await b.close()
