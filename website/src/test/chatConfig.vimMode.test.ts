/**
 * `vimMode` chat-config field (#6321): default off, coerced from a bad stored value.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { loadChatConfig } from '../pages/chat/ChatSettings'

const KEY = 'mc-chat-config'
afterEach(() => localStorage.removeItem(KEY))

describe('vimMode config field', () => {
  it('defaults to false with no stored config', () => {
    expect(loadChatConfig().vimMode).toBe(false)
  })

  it('reads a stored boolean through', () => {
    localStorage.setItem(KEY, JSON.stringify({ vimMode: true }))
    expect(loadChatConfig().vimMode).toBe(true)
  })

  it('coerces a non-boolean stored value to false (never a truthy string)', () => {
    localStorage.setItem(KEY, JSON.stringify({ vimMode: 'yes' }))
    expect(loadChatConfig().vimMode).toBe(false)
  })
})
