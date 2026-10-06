/**
 * ChatInput Vim-mode integration (#6321).
 *
 * The engine itself is unit-tested in vimComposer.test.ts; this pins the WIRING:
 * the opt-in gate, the mode indicator, Escape entering Normal, a Normal-mode
 * edit flowing through `onChange`, and — the accessibility fail-safe — that with
 * the setting OFF the composer does not render the indicator and does not
 * intercept Escape.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent } from '@testing-library/react'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'

const CONFIG_KEY = 'mc-chat-config'
afterEach(() => localStorage.removeItem(CONFIG_KEY))

/** Controlled host so value edits from the composer round-trip, like the app. */
function Harness({ initial = '' }: { initial?: string }) {
  const [value, setValue] = useState(initial)
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} />
}

describe('composer vim mode (off by default)', () => {
  it('renders no mode indicator and leaves Escape alone when off', () => {
    renderWithProviders(<Harness />)
    expect(screen.queryByTestId('composer-vim-mode')).toBeNull()
    const textarea = screen.getByLabelText('Message input') as HTMLTextAreaElement
    // Escape is not consumed by Vim; it is a plain key the composer handles
    // normally. preventDefault must NOT have been called by a Vim interceptor.
    const ev = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    textarea.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(false)
  })
})

describe('composer vim mode (opted in)', () => {
  it('shows the Insert indicator by default', () => {
    localStorage.setItem(CONFIG_KEY, JSON.stringify({ vimMode: true }))
    renderWithProviders(<Harness />)
    const badge = screen.getByTestId('composer-vim-mode')
    expect(badge).toBeTruthy()
    expect(badge.textContent).toBe('Insert')
  })

  it('Escape switches to Normal and the indicator updates', () => {
    localStorage.setItem(CONFIG_KEY, JSON.stringify({ vimMode: true }))
    renderWithProviders(<Harness initial="hello" />)
    const textarea = screen.getByLabelText('Message input') as HTMLTextAreaElement
    textarea.focus()
    textarea.setSelectionRange(3, 3)
    fireEvent.keyDown(textarea, { key: 'Escape' })
    expect(screen.getByTestId('composer-vim-mode').textContent).toBe('Normal')
  })

  it('a Normal-mode edit (x) flows through onChange', () => {
    localStorage.setItem(CONFIG_KEY, JSON.stringify({ vimMode: true }))
    renderWithProviders(<Harness initial="abc" />)
    const textarea = screen.getByLabelText('Message input') as HTMLTextAreaElement
    textarea.focus()
    textarea.setSelectionRange(1, 1)
    fireEvent.keyDown(textarea, { key: 'Escape' }) // → Normal, caret at 0
    fireEvent.keyDown(textarea, { key: 'l' })       // → caret at 1 ('b')
    fireEvent.keyDown(textarea, { key: 'x' })       // delete 'b'
    expect(textarea.value).toBe('ac')
  })
})
