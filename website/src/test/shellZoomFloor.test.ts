/**
 * Desktop shell floor under browser zoom (#11114). The content track's minimum
 * comes from `--mc-shell-chat-floor` in index.css, which is set only while a
 * BESIDE-mode Activity panel sits in the shell's actbar slot. Its value has to
 * equal the chat page's own CHAT_PANE_MIN_W, otherwise the shell's floor and
 * the panel's resize clamp would disagree about how narrow the chat pane can get.
 */
import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { CHAT_PANE_MIN_W } from '../pages/chat/SidePanel'

const css = async () => (await readFile(join(__dirname, '..', 'index.css'), 'utf8')).replace(/\/\*[\s\S]*?\*\//g, '')

describe('desktop shell zoom floor', () => {
  it('sets the chat floor to CHAT_PANE_MIN_W only for a beside-mode panel in the actbar slot', async () => {
    const text = await css()
    const rule = text.match(/([^{}]+)\{--mc-shell-chat-floor:(\d+)px\}/)
    expect(rule).not.toBeNull()
    const [, selector, px] = rule!
    expect(Number(px)).toBe(CHAT_PANE_MIN_W)
    expect(selector).toContain('#activity-bar-slot [data-testid="side-panel-root"]')
    // FILL mode covers the chat column by design; a floor there would only add dead scroll.
    expect(selector).toContain(':not([data-panel-fill])')
  })
})
