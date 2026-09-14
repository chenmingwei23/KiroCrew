/**
 * Screenshot evidence + assertion for #10774 -- the notification bell rendered
 * under the Windows caption close button in an embedded remote-instance pane.
 *
 * Drives the ISOLATED capture entry (capture/embedded-win-caption-inset.html),
 * which mounts the REAL shipped `.tb-right` bell group under the real index.css
 * and toggles `.embedded-win-inset` exactly as EmbeddedHostBridge does. A 138px
 * caption band is painted at the header's top-right as a stand-in for the native
 * overlay, so the overlap (before) and the clearance (after) are both visible.
 *
 * This is the light path for the Screenshot Evidence gate on a host that cannot
 * run the Windows desktop app: the fix is a CSS inset driven by a class, and this
 * exercises the same class + the same stylesheet the app drives. The unit test
 * (src/test/EmbeddedSwitcher.test.tsx) pins the relay + class toggle; this pins
 * the rendered geometry.
 *
 * Assertions:
 *  - inset=off (before): the bell's right edge is INSIDE the caption band --
 *    the bug reproduces, or the before/after evidence would be meaningless.
 *  - inset=on  (after):  the bell clears the caption band, and the header
 *    carries the 142px right padding.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-embedded-win-caption-inset.mjs http://127.0.0.1:6821 ../temp-screenshots/embedded-win-caption-inset
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/embedded-win-caption-inset'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 760, height: 120 }

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

for (const inset of ['off', 'on']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(
    `${BASE}/capture/embedded-win-caption-inset.html?inset=${inset}`,
    { waitUntil: 'networkidle' },
  )
  await page.waitForSelector('[data-testid="bell"]')
  await page.waitForTimeout(150)
  const m = await page.evaluate(() => window.__measure())
  console.log(
    `inset=${inset.padEnd(3)}: bellRight=${m.bellRight} bandLeft=${m.bandLeft} ` +
    `clearsCaption=${m.clearsCaption} paddingRight=${m.paddingRight}`,
  )
  if (!m.ok) {
    console.error(`FAIL: inset=${inset} -- harness did not render the bell/header`)
    failures++
  } else if (inset === 'off' && m.clearsCaption) {
    console.error('FAIL: before state did not reproduce the overlap -- the bell already clears the caption band without the fix, so the evidence is meaningless')
    failures++
  } else if (inset === 'on' && !m.clearsCaption) {
    console.error('FAIL: after state still overlaps -- the .embedded-win-inset reserve did not clear the bell')
    failures++
  }
  await page.screenshot({ path: `${OUT}/${inset === 'off' ? 'before' : 'after'}.png` })
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
