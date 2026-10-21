/** Real-browser evidence for the Side Chat reload rehydrate (#22869).
 *
 * Drives website/capture/side-chat-reload-22869.html: the real SideChat over an
 * EMPTY store (a just-reloaded page), with the mount read route mocked. Asserted,
 * then shot:
 *
 *   before          the read answers no sidecar: the panel shows only its empty
 *                   placeholder ("Ask a side question…"), no transcript rows —
 *                   the bug, where a reload loses the held conversation.
 *   after           the read answers the held conversation: the panel shows the
 *                   remembered question and its answer after the reload.
 *   after-light     the same, light theme.
 *
 * Usage (website/ is the vite root):
 *   npx vite --host 127.0.0.1 --port 6875 --strictPort
 *   node scripts/capture-22869-side-reload.mjs http://127.0.0.1:6875 ../temp-screenshots/side-reload-22869
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6875'
const OUT = resolve(process.argv[3] || '../temp-screenshots/side-reload-22869')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

async function open(query) {
  const ctx = await browser.newContext({ viewport: { width: 460, height: 600 }, deviceScaleFactor: 2 })
  ctx.setDefaultTimeout(120000)
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/side-chat-reload-22869.html?${query}`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.waitForSelector('[data-capture-root]')
  await page.waitForTimeout(600)
  return { ctx, page, errors }
}

async function shot(page, name) {
  await page.locator('[data-capture-root]').screenshot({ path: resolve(OUT, `${name}.png`) })
}

{
  const { ctx, page, errors } = await open('scene=before&theme=dark')
  const body = await page.locator('[data-capture-root]').innerText()
  check('before: no transcript', !body.includes('pineapple'))
  check('before: shows empty placeholder', /side question/i.test(body))
  check('before: no page errors', errors.length === 0)
  await shot(page, 'before')
  await ctx.close()
}

{
  const { ctx, page, errors } = await open('scene=after&theme=dark')
  const body = await page.locator('[data-capture-root]').innerText()
  check('after: shows the remembered question', body.includes('Remember the word pineapple'))
  check('after: shows the answer', body.includes('remember the word pineapple'))
  check('after: no page errors', errors.length === 0)
  await shot(page, 'after')
  await ctx.close()
}

{
  const { ctx, page } = await open('scene=after&theme=light')
  await shot(page, 'after-light')
  await ctx.close()
}

await browser.close()
console.log(failures === 0 ? 'ALL OK' : `${failures} FAILURE(S)`)
process.exit(failures === 0 ? 0 : 1)
