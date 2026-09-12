/**
 * Screenshot harness for the Side-by-side diffs toggle in Settings -> Chat.
 *
 * The PR adds one Settings control bound to the existing shared `mc-diff-split`
 * preference. The evidence has to show the control in its home (beside Plain
 * diffs, the sibling client-local diff-rendering row) and that clicking it
 * writes the key every diff surface reads.
 *
 * Runs the REAL built SPA (website/dist) behind this folder's shared
 * `lib/serve-dist.mjs`, answering every /api/** call from fixtures. No gateway,
 * no dashboard auth, no kiro-cli spawn.
 *
 * Shots:
 *  1. Chat -> Messages, toggle ON  -- the shipped default (side-by-side), shown
 *     beside Plain diffs so the shared surface is visible in one frame.
 *  2. Chat -> Messages, toggle OFF -- after one click, plus an assertion that
 *     the click wrote `mc-diff-split=0`, the key DiffBlock / FileChangeChips /
 *     SidePanel read as their initial layout. The row is browser-local, so
 *     nothing on the wire would prove it persisted.
 *
 * Labels are read from the CATALOGS, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-settings-diff-layout.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/settings-diff-layout'
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const generated = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const DIFF_LAYOUT = manual.settings?.chat?.diffLayout?.label
const DIFF_LAYOUT_DESC = manual.settings?.chat?.diffLayout?.description
const PLAIN_DIFF = manual.settings?.chat?.plainDiff?.label
if (!DIFF_LAYOUT || !DIFF_LAYOUT_DESC || !PLAIN_DIFF) {
  throw new Error('catalog keys missing -- settings.chat.diffLayout.* or plainDiff.label renamed?')
}

const DIFF_SPLIT_KEY = 'mc-diff-split'

const PROJECT = '/home/user/project'
const fixedApi = makeFixedApi(PROJECT)

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1400, height: 900 },
  // Settings rows are 12-13px type; a 1x shot renders soft on GitHub.
  deviceScaleFactor: 2,
})
const page = await context.newPage()

await page.routeWebSocket(/\/api\/ws/, () => {})

await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/chat/slots') return json(route, [])
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
})

await page.goto(`${base}/settings?tab=chat`, { waitUntil: 'domcontentloaded' })

const toggle = page.getByRole('switch', { name: DIFF_LAYOUT })
await toggle.waitFor({ state: 'visible', timeout: 15_000 })
await page.waitForTimeout(600)

// Frame the pair: the claim is that the row sits with Plain diffs, so both
// have to be in the same shot.
await page.getByRole('switch', { name: PLAIN_DIFF }).scrollIntoViewIfNeeded()
await page.waitForTimeout(300)

// Side-by-side is the shipped default of the shared preference, so a fresh
// profile shows it ON.
if (await toggle.getAttribute('aria-checked') !== 'true') {
  throw new Error('expected the toggle on for a fresh profile -- side-by-side is the default')
}
await page.screenshot({ path: join(OUT, '01-chat-messages-side-by-side-on.png'), fullPage: false })
console.log('captured 01-chat-messages-side-by-side-on.png')

await toggle.click()
await page.waitForFunction(
  key => localStorage.getItem(key) === '0',
  DIFF_SPLIT_KEY,
  { timeout: 5_000 },
)
if (await toggle.getAttribute('aria-checked') !== 'false') {
  throw new Error('switch did not follow the stored value -- inverted checkbox?')
}
await page.screenshot({ path: join(OUT, '02-chat-messages-unified-off.png'), fullPage: false })
console.log(`captured 02-chat-messages-unified-off.png (${DIFF_SPLIT_KEY}=0 asserted)`)

// Frame 3: disabled while Plain diffs is on, with the explanatory hint.
// The init script above clears localStorage on every navigation, so seed the
// plain-diff preference through a fresh init script and reload for a clean read.
const PLAIN_DIFF_KEY = 'mc-diff-plain'
await page.addInitScript(k => { localStorage.setItem(k, '1') }, PLAIN_DIFF_KEY)
await page.goto(`${base}/settings?tab=chat`, { waitUntil: 'domcontentloaded' })
const toggle3 = page.getByRole('switch', { name: DIFF_LAYOUT })
await toggle3.waitFor({ state: 'visible', timeout: 15_000 })
await page.waitForTimeout(600)
await page.getByRole('switch', { name: PLAIN_DIFF }).scrollIntoViewIfNeeded()
await page.waitForTimeout(300)
if (await toggle3.getAttribute('aria-disabled') !== 'true') {
  throw new Error('expected the toggle disabled while plain diffs is on')
}
const hintId = await toggle3.getAttribute('aria-describedby')
if (hintId !== 'diff-layout-plain-hint') {
  throw new Error('expected the disabled hint tied to the switch via aria-describedby')
}
if (await page.locator('#diff-layout-plain-hint').count() !== 1) {
  throw new Error('expected the disabled hint text rendered')
}
await page.screenshot({ path: join(OUT, '03-chat-messages-disabled-hint.png'), fullPage: false })
console.log('captured 03-chat-messages-disabled-hint.png (aria-disabled + hint asserted)')

await browser.close()
srv.close()
