/**
 * Real-behavior capture + assertion for #14675 -- editing a picked file mention
 * drops the file from the message.
 *
 * Drives the isolated capture entry (website/capture/mention-atomic-delete.html),
 * which mounts a real <textarea> composer and a file chip, wired to the SHIPPED
 * pure `mentionSpanAt` (fix=on) or to the pre-fix native per-character delete
 * (fix=off).
 *
 * Scenario (both arms):
 *   1. composer holds `please review @src/main.ts for the bug` with a staged
 *      `main.ts` chip.
 *   2. caret at the end of `@src/main.ts`, one Backspace.
 * Expected: fix=on removes the whole `@src/main.ts` and unstages the chip
 *   (`please review for the bug`, no chip); fix=off leaves `@src/main.t` in the
 *   text while the chip unstages -- a half-reference the send would carry.
 *
 * The before (fix=off) arm is ASSERTED to reproduce the half-reference, so the
 * before/after stills are meaningful rather than two identical frames.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort   # in another shell
 *   node scripts/capture-mention-atomic-delete.mjs http://127.0.0.1:6814 ../temp-screenshots/mention-atomic-delete
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/mention-atomic-delete'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 820, height: 300 }

// mise's node injects an older LD_LIBRARY_PATH; children inherit it, so scrub
// it here (mirrors capture-chip-suffix-untoggle). A PW_BROWSER_LDPATH override
// is honoured when set.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
if (process.env.PW_BROWSER_LDPATH) browserEnv.LD_LIBRARY_PATH = process.env.PW_BROWSER_LDPATH
const browser = await chromium.launch({ env: browserEnv, args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] })
let failures = 0

for (const fix of ['off', 'on']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(`${BASE}/capture/mention-atomic-delete.html?theme=dark&fix=${fix}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-composer]')
  await page.waitForSelector('[data-chip]') // the picked mention's chip is staged

  const composer = page.locator('[data-composer]')
  // Snapshot the starting state (identical in both arms): mention + chip.
  await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-1-picked.png` })

  // Put the caret at the end of `@src/main.ts`, then one Backspace.
  await composer.evaluate((el) => {
    const ta = el
    const end = ta.value.indexOf('@src/main.ts') + '@src/main.ts'.length
    ta.focus()
    ta.setSelectionRange(end, end)
  })
  await page.keyboard.press('Backspace')
  await page.waitForTimeout(150)

  const state = await page.evaluate(() => window.__state())
  await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-2-after-backspace.png` })

  console.log(`fix=${fix.padEnd(3)}: value=${JSON.stringify(state.value)} staged=${state.staged}`)

  if (fix === 'on') {
    if (state.value !== 'please review for the bug' || state.staged) {
      console.error(`FAIL(fix=on): expected whole mention removed + chip unstaged, got ${JSON.stringify(state.value)} staged=${state.staged}`)
      failures++
    }
  } else {
    // The before arm must reproduce the bug: a half-reference left behind,
    // chip unstaged -- otherwise the evidence is meaningless.
    if (!state.value.includes('@src/main.t') || state.value.includes('@src/main.ts') || state.staged) {
      console.error(`FAIL(fix=off): expected a half-reference '@src/main.t' with the chip unstaged, got ${JSON.stringify(state.value)} staged=${state.staged}`)
      failures++
    }
  }
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
