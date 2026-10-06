/**
 * Isolated capture entry for #14675 -- editing a picked file mention drops the
 * file from the message.
 *
 * WHY ISOLATED: the fix is a keystroke transform on the composer draft plus the
 * file chip that tracks it. This mounts a real <textarea> composer and a chip
 * styled like the product's attachment chip, and wires Backspace/Delete through
 * one of two arms:
 *
 *   fix=on  -- the SHIPPED pure `mentionSpanAt` (src/utils/fileTokens.ts), the
 *              exact grammar `handleMentionKey` runs: a Backspace on/adjacent to
 *              the mention removes the WHOLE `@src/main.ts` and unstages the
 *              chip, so no half-reference survives.
 *   fix=off -- the pre-fix behavior: native per-character delete. One Backspace
 *              leaves `@src/main.t` in the text while the chip unstages (the
 *              reconciliation no longer recognizes the shortened spelling) --
 *              the message would be sent naming a file with no attachment.
 *
 * The before (fix=off) arm reproduces the bug so the before/after stills are
 * meaningful rather than two identical frames.
 *
 * window.__state() reports the composer value and whether the chip is staged,
 * for the capture script's assertions.
 *
 * Query string: ?theme=dark&fix=on
 */
import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import { mentionSpanAt } from '../src/utils/fileTokens'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const fixOn = params.get('fix') !== 'off'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// One picked file: its chip label and the `@rel` alias a pick recorded.
const ALIAS = '@src/main.ts'
const CHIP_LABEL = 'main.ts'
const INITIAL = `please review ${ALIAS} for the bug`

declare global {
  interface Window {
    __state: () => { value: string; staged: boolean; fix: boolean }
  }
}

function Scene() {
  const [value, setValue] = useState(INITIAL)
  // The chip is staged while a recorded alias is still present in the text --
  // the same "exact-or-nothing" rule the real reconciliation applies.
  const [staged, setStaged] = useState(true)
  const taRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    window.__state = () => ({ value, staged, fix: fixOn })
  }, [value, staged])

  // Reconcile the chip off the text: staged iff the recorded alias is still
  // present as a mention. In this single-file scene the alias has no longer
  // sibling to confuse it, so a boundary-correct presence test is exact --
  // and a shortened `@src/main.t` (the pre-fix arm's leftover) fails it, the
  // way the real "exact-or-nothing" reconciliation drops the chip.
  const mentions = (text: string) => {
    const i = text.indexOf(ALIAS)
    return i !== -1 && mentionSpanAt(text, i + ALIAS.length, false, [ALIAS], false) !== null
  }
  const reconcile = (text: string) => setStaged(mentions(text))

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Backspace' && e.key !== 'Delete') return
    const ta = e.currentTarget
    const ss = ta.selectionStart ?? 0
    const se = ta.selectionEnd ?? 0
    if (ss !== se) return
    if (!fixOn) return // pre-fix arm: let the browser delete one character
    const span = mentionSpanAt(value, ss, e.key === 'Delete', [ALIAS], false)
    if (!span) return
    e.preventDefault()
    let { start, end } = span
    if (value[end] === ' ' && (start === 0 || value[start - 1] === ' ')) end += 1
    else if (value[start - 1] === ' ' && (end >= value.length || value[end] === ' ')) start -= 1
    const next = value.slice(0, start) + value.slice(end)
    setValue(next)
    reconcile(next)
    requestAnimationFrame(() => ta.setSelectionRange(start, start))
  }

  return (
    <div className="bg-bg text-text flex flex-col justify-end min-h-screen">
      <div className="px-4 pb-3 pt-2 mx-auto w-full flex flex-col" style={{ maxWidth: 760 }}>
        <div className="mb-2 text-[12px] text-muted" data-mode>
          {fixOn
            ? 'AFTER \u2014 picked mention is atomic (this PR): one Backspace removes the whole @mention and its chip'
            : 'BEFORE \u2014 per-character delete: one Backspace leaves @src/main.t while the chip silently unstages'}
        </div>
        {/* The staged file chip, styled like the product's attachment chip. */}
        <div className="mb-2 flex gap-2" data-chip-row>
          {staged && (
            <span data-chip className="inline-flex items-center gap-1 rounded-lg border border-border bg-bg-elevated px-2 py-1 text-[12px] text-text">
              📎 {CHIP_LABEL}
              <button aria-label="Remove" className="text-muted">×</button>
            </span>
          )}
        </div>
        <textarea
          ref={taRef}
          aria-label="Message input"
          data-composer
          value={value}
          onKeyDown={onKeyDown}
          onChange={e => { setValue(e.target.value); reconcile(e.target.value) }}
          rows={2}
          className="mt-1 rounded-2xl border border-border bg-bg-elevated px-3 py-3 text-[14px] text-text font-mono w-full resize-none"
        />
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
