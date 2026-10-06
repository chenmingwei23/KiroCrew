import { useSyncExternalStore, useRef, useState, useCallback } from 'react'
import { loadChatConfig } from '../../pages/chat/ChatSettings'
import {
  applyVimKey,
  applyVimPendingG,
  applyVimOperator,
  applyVimTextObject,
  type VimEditorState,
  type VimMode,
  type VimKey,
} from './vimComposer'

/**
 * Composer Vim mode: the live-config read, the stateful keydown driver, and the
 * mode a host renders in its indicator.
 *
 * `useComposerVimEnabled` mirrors `useComposerSpellcheck` — one source for the
 * opt-in preference, kept live by the `mc-config-changed` event the Settings
 * row dispatches on save. When it returns false the composer never calls the
 * driver and behaves as a plain textarea (the issue's accessibility fail-safe:
 * Escape and every key keep standard behaviour when the mode is off).
 */
const sub = (cb: () => void) => {
  window.addEventListener('mc-config-changed', cb)
  return () => window.removeEventListener('mc-config-changed', cb)
}
const getVimEnabled = () => loadChatConfig().vimMode

export const useComposerVimEnabled = (): boolean => useSyncExternalStore(sub, getVimEnabled)

/** A pending multi-key sequence the next keystroke completes. */
type Pending =
  | { kind: 'g' }
  | { kind: 'operator'; operator: 'd' | 'c' }
  | { kind: 'textobject'; operator: 'd' | 'c' }
  | null

export interface VimTextareaDriver {
  /** Whether Vim mode is on (the host gates its indicator and interception on
   *  this). */
  enabled: boolean
  /** Current mode, for the indicator. */
  mode: VimMode
  /**
   * Handle a keydown on the textarea. Returns true when the engine consumed the
   * key — the host must then NOT run its ordinary composer keydown (send,
   * history, token keys) for this event, and should have called
   * `e.preventDefault()`. Returns false to let the key fall through unchanged.
   *
   * The host passes the live textarea so the driver can read the real selection
   * and write back the result (value via `onChange`, selection via the DOM).
   */
  handleKeyDown: (
    e: { key: string; shiftKey: boolean; ctrlKey: boolean; metaKey: boolean; altKey: boolean; preventDefault: () => void },
    textarea: HTMLTextAreaElement,
    onChange: (v: string) => void,
  ) => boolean
  /** Reset to Insert mode and clear any pending sequence. Called when the
   *  composer is cleared/sent or the setting flips, so a fresh draft always
   *  starts in a plain-typing state. */
  reset: () => void
}

/**
 * The stateful driver. Mode and the yank register live in refs (they must
 * survive keystrokes without forcing a render), while `mode` is also mirrored
 * into React state so the indicator re-renders when it changes.
 */
export function useComposerVim(): VimTextareaDriver {
  const enabled = useComposerVimEnabled()
  // Default Insert: a composer the user just focused should accept typing, and
  // non-Vim muscle memory is preserved until they press Escape.
  const [mode, setMode] = useState<VimMode>('insert')
  const modeRef = useRef<VimMode>('insert')
  const registerRef = useRef<string>('')
  const pendingRef = useRef<Pending>(null)

  const setModeBoth = useCallback((m: VimMode) => {
    modeRef.current = m
    setMode(prev => (prev === m ? prev : m))
  }, [])

  const reset = useCallback(() => {
    pendingRef.current = null
    setModeBoth('insert')
  }, [setModeBoth])

  const handleKeyDown = useCallback<VimTextareaDriver['handleKeyDown']>((e, textarea, onChange) => {
    if (!enabled) return false

    const key: VimKey = {
      key: e.key,
      shiftKey: e.shiftKey,
      ctrlKey: e.ctrlKey,
      metaKey: e.metaKey,
      altKey: e.altKey,
    }

    const before: VimEditorState = {
      value: textarea.value,
      selectionStart: textarea.selectionStart ?? 0,
      selectionEnd: textarea.selectionEnd ?? 0,
      mode: modeRef.current,
      register: registerRef.current,
    }

    // While a multi-key sequence is pending, the next key completes it.
    const pending = pendingRef.current
    let result
    if (pending?.kind === 'g') {
      pendingRef.current = null
      result = applyVimPendingG(before, key)
    } else if (pending?.kind === 'operator') {
      // A text-object trigger (`i` after `d`/`c`) arms a third-key sequence
      // (`diw`/`ciw`).
      if (key.key === 'i') {
        pendingRef.current = { kind: 'textobject', operator: pending.operator }
        e.preventDefault()
        return true
      }
      pendingRef.current = null
      result = applyVimOperator(before, pending.operator, key)
    } else if (pending?.kind === 'textobject') {
      pendingRef.current = null
      result = applyVimTextObject(before, pending.operator, key)
    } else if (modeRef.current === 'normal' && (key.key === 'g') && !key.ctrlKey && !key.metaKey && !key.altKey) {
      // Arm `gg`.
      pendingRef.current = { kind: 'g' }
      e.preventDefault()
      return true
    } else if (modeRef.current === 'normal' && (key.key === 'd' || key.key === 'c') && !key.ctrlKey && !key.metaKey && !key.altKey) {
      // Arm the operator.
      pendingRef.current = { kind: 'operator', operator: key.key }
      e.preventDefault()
      return true
    } else {
      result = applyVimKey(before, key)
    }

    if (!result.consumed) {
      // Insert-mode passthrough (and unimplemented Normal keys): let the
      // textarea's own handling run. Keep mode state in sync in case the engine
      // changed it (it does not when consumed=false, but be defensive).
      return false
    }

    e.preventDefault()
    const after = result.state
    registerRef.current = after.register
    setModeBoth(after.mode)

    if (after.value !== before.value) {
      // Push the new text through the host's change path (undo recorder, picker
      // triggers), then restore the caret the engine computed — React's
      // controlled value re-render would otherwise reset it to the end.
      onChange(after.value)
      requestAnimationFrame(() => {
        if (document.activeElement === textarea) {
          textarea.setSelectionRange(after.selectionStart, after.selectionEnd)
        }
      })
    } else if (
      after.selectionStart !== before.selectionStart ||
      after.selectionEnd !== before.selectionEnd
    ) {
      textarea.setSelectionRange(after.selectionStart, after.selectionEnd)
    }
    return true
  }, [enabled, setModeBoth])

  return { enabled, mode, handleKeyDown, reset }
}
