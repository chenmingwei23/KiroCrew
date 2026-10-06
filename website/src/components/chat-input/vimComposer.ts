/**
 * A small, dependency-free Vim keymap for the plain-`<textarea>` composer.
 *
 * This is the deliberately-minimal, OPT-IN slice of issue #6321: Normal and
 * Insert modes with the core motions and edits a prose composer actually needs,
 * hand-rolled over the existing textarea rather than pulling an editor engine
 * (CodeMirror / monaco-vim, ~300 KB+) into the dashboard bundle. Visual mode,
 * registers, counts and operator-pending composition are intentionally OUT of
 * scope here and tracked as follow-ups — the point is a usable normal/insert
 * split that non-Vim users never see, not a complete Vim.
 *
 * ## Why a pure function
 *
 * `applyVimKey` takes the current editor state (text + selection) and a
 * normalized key, and returns the NEXT state plus whether it consumed the key.
 * It touches no DOM, no React, no clipboard API and no `window` — so the whole
 * keymap is unit-testable with plain objects, and the one impure concern (the
 * yank register) is passed in and out explicitly rather than hidden in a module
 * global. The caller (`ChatInput`) owns applying the returned state to the real
 * textarea and persisting the register across keystrokes.
 *
 * ## Accessibility invariant
 *
 * When the mode is Insert, EVERY key except `Escape` falls straight through
 * (`consumed: false`), so the composer behaves exactly like a normal textarea —
 * typing, Enter-to-send, Tab, arrow keys, shortcuts. The feature only ever
 * intercepts keys while in Normal mode, and the host only enters Normal mode
 * when the user has turned Vim mode on. With the setting off the engine is
 * never called at all, so Escape and every other key keep their standard
 * behaviour (the issue's fail-safe requirement).
 */

export type VimMode = 'normal' | 'insert'

/** The slice of textarea state the keymap reads and rewrites. */
export interface VimEditorState {
  /** Full composer text. */
  value: string
  /** Caret / selection anchor (0-based index into `value`). */
  selectionStart: number
  /** Caret / selection head. In Normal mode this equals `selectionStart`
   *  except for the one-cell cursor the host may draw; the engine always
   *  computes against a single caret position (`selectionStart`). */
  selectionEnd: number
  /** Current mode. */
  mode: VimMode
  /** The yank/delete register (what `p` pastes). Linewise when it ends in a
   *  newline, charwise otherwise — mirroring Vim's register semantics for the
   *  handful of commands implemented here. Empty string = nothing yanked. */
  register: string
}

export interface VimKeyResult {
  /** The state after the key, or the input unchanged when not consumed. */
  state: VimEditorState
  /** Whether the engine handled the key. `false` means the host must let the
   *  key reach the textarea's normal handling (the Insert-mode passthrough and
   *  any Normal-mode key the engine does not implement). */
  consumed: boolean
}

/** A normalized keydown, decoupled from React's SyntheticEvent so the engine
 *  stays testable with plain objects. `key` is the DOM `KeyboardEvent.key`. */
export interface VimKey {
  key: string
  shiftKey: boolean
  ctrlKey: boolean
  metaKey: boolean
  altKey: boolean
}

const LINE = '\n'

/** Index of the first character of the line containing `pos`. */
function lineStart(value: string, pos: number): number {
  const nl = value.lastIndexOf(LINE, pos - 1)
  return nl === -1 ? 0 : nl + 1
}

/** Index one past the last character of the line containing `pos` (the newline
 *  or end-of-string). */
function lineEnd(value: string, pos: number): number {
  const nl = value.indexOf(LINE, pos)
  return nl === -1 ? value.length : nl
}

/** Clamp a Normal-mode caret so it sits ON a character, never past the last
 *  one of its line — Vim's block cursor cannot rest on the newline. On an empty
 *  line the caret sits at the (zero-width) line start. */
function clampNormal(value: string, pos: number): number {
  const start = lineStart(value, pos)
  const end = lineEnd(value, pos)
  if (end <= start) return start
  return Math.min(Math.max(pos, start), end - 1)
}

const isWordChar = (ch: string): boolean => /[A-Za-z0-9_]/.test(ch)
const isSpace = (ch: string): boolean => /\s/.test(ch)

/** `w` — start of the next word (word = run of word-chars, or a run of
 *  punctuation; whitespace separates). Stops at end-of-text. */
function nextWord(value: string, pos: number): number {
  const n = value.length
  let i = pos
  if (i >= n) return n
  const startClass = classOf(value[i])
  // Skip the rest of the current token.
  while (i < n && classOf(value[i]) === startClass && startClass !== 'space') i++
  // Skip whitespace to the next token.
  while (i < n && isSpace(value[i])) i++
  return i
}

/** `b` — start of the current or previous word. */
function prevWord(value: string, pos: number): number {
  let i = pos
  if (i <= 0) return 0
  i--
  while (i > 0 && isSpace(value[i])) i--
  const cls = classOf(value[i])
  while (i > 0 && classOf(value[i - 1]) === cls && cls !== 'space') i--
  return i
}

type CharClass = 'word' | 'punct' | 'space'
function classOf(ch: string): CharClass {
  if (isSpace(ch)) return 'space'
  if (isWordChar(ch)) return 'word'
  return 'punct'
}

/** Caret column (chars from line start), for preserving column across j/k. */
function column(value: string, pos: number): number {
  return pos - lineStart(value, pos)
}

const caret = (state: VimEditorState, pos: number, mode: VimMode = state.mode): VimEditorState => ({
  ...state,
  selectionStart: pos,
  selectionEnd: pos,
  mode,
})

/**
 * Apply one keystroke to the editor state.
 *
 * The host calls this ONLY while Vim mode is enabled. In Insert mode every key
 * but Escape is a passthrough; in Normal mode the implemented motions and edits
 * are consumed and everything else passes through (so, e.g., Ctrl+shortcuts and
 * unimplemented keys keep working rather than being silently swallowed).
 */
export function applyVimKey(state: VimEditorState, key: VimKey): VimKeyResult {
  const pass: VimKeyResult = { state, consumed: false }

  // A key carrying a non-shift modifier is never a Vim motion here: it is a
  // browser/app shortcut (Cmd+Enter send, Ctrl+Z undo, …). Let it through in
  // both modes so Vim mode never shadows an existing chord.
  if (key.ctrlKey || key.metaKey || key.altKey) {
    // Escape is handled below even though it has no modifier; shortcuts fall
    // through here first.
    return pass
  }

  if (state.mode === 'insert') {
    if (key.key === 'Escape') {
      // Leave Insert → Normal, and step the caret one cell left (Vim behaviour),
      // clamped onto a character.
      const pos = clampNormal(state.value, Math.max(lineStart(state.value, state.selectionStart), state.selectionStart - 1))
      return { state: caret(state, pos, 'normal'), consumed: true }
    }
    return pass
  }

  // ── Normal mode ──
  const { value } = state
  const pos = clampNormal(value, state.selectionStart)
  const ls = lineStart(value, pos)
  const le = lineEnd(value, pos)

  switch (key.key) {
    // Motions ───────────────────────────────────────────────
    case 'h':
    case 'ArrowLeft':
      return { state: caret(state, Math.max(ls, pos - 1)), consumed: true }
    case 'l':
    case 'ArrowRight':
      return { state: caret(state, clampNormal(value, pos + 1)), consumed: true }
    case 'j':
    case 'ArrowDown': {
      const nextLs = le < value.length ? le + 1 : -1
      if (nextLs === -1) return { state: caret(state, pos), consumed: true }
      const col = column(value, pos)
      const nextLe = lineEnd(value, nextLs)
      const target = Math.min(nextLs + col, Math.max(nextLs, nextLe - 1))
      return { state: caret(state, target), consumed: true }
    }
    case 'k':
    case 'ArrowUp': {
      if (ls === 0) return { state: caret(state, pos), consumed: true }
      const prevLs = lineStart(value, ls - 1)
      const prevLe = ls - 1 // the newline terminating the previous line
      const col = column(value, pos)
      const target = Math.min(prevLs + col, Math.max(prevLs, prevLe - 1))
      return { state: caret(state, target), consumed: true }
    }
    case '0':
      return { state: caret(state, ls), consumed: true }
    case '$':
      return { state: caret(state, Math.max(ls, le - 1)), consumed: true }
    case '^': {
      let i = ls
      while (i < le && isSpace(value[i])) i++
      return { state: caret(state, Math.min(i, Math.max(ls, le - 1))), consumed: true }
    }
    case 'w':
      return { state: caret(state, clampNormal(value, nextWord(value, pos))), consumed: true }
    case 'b':
      return { state: caret(state, clampNormal(value, prevWord(value, pos))), consumed: true }
    case 'G': {
      // Last line, first non-blank-ish: Vim goes to first non-blank; keep it simple
      // and go to the start of the last line.
      const lastLs = lineStart(value, value.length)
      return { state: caret(state, clampNormal(value, lastLs)), consumed: true }
    }
    case 'g':
      // `gg` is a two-key sequence; the host tracks the pending `g` and calls
      // `applyVimPendingG` for the second key. A lone `g` is consumed (so it does
      // not type a literal 'g') and the host arms the pending state.
      return { state, consumed: true }

    // Enter Insert mode ─────────────────────────────────────
    case 'i':
      return { state: caret(state, pos, 'insert'), consumed: true }
    case 'a':
      return { state: caret(state, Math.min(pos + 1, le), 'insert'), consumed: true }
    case 'I': {
      let i = ls
      while (i < le && isSpace(value[i])) i++
      return { state: caret(state, i, 'insert'), consumed: true }
    }
    case 'A':
      return { state: caret(state, le, 'insert'), consumed: true }
    case 'o': {
      // Open a new line below and enter Insert.
      const next = value.slice(0, le) + LINE + value.slice(le)
      return { state: { ...state, value: next, selectionStart: le + 1, selectionEnd: le + 1, mode: 'insert' }, consumed: true }
    }
    case 'O': {
      // Open a new line above and enter Insert.
      const next = value.slice(0, ls) + LINE + value.slice(ls)
      return { state: { ...state, value: next, selectionStart: ls, selectionEnd: ls, mode: 'insert' }, consumed: true }
    }

    // Edits ─────────────────────────────────────────────────
    case 'x': {
      if (le <= ls) return { state: caret(state, pos), consumed: true }
      const removed = value.slice(pos, pos + 1)
      const next = value.slice(0, pos) + value.slice(pos + 1)
      return {
        state: { ...state, value: next, selectionStart: clampNormal(next, pos), selectionEnd: clampNormal(next, pos), register: removed, mode: 'normal' },
        consumed: true,
      }
    }
    case 'D': {
      // Delete to end of line.
      const removed = value.slice(pos, le)
      const next = value.slice(0, pos) + value.slice(le)
      return {
        state: { ...state, value: next, selectionStart: clampNormal(next, pos), selectionEnd: clampNormal(next, pos), register: removed, mode: 'normal' },
        consumed: true,
      }
    }
    case 'C': {
      // Change to end of line: delete to EOL and enter Insert.
      const removed = value.slice(pos, le)
      const next = value.slice(0, pos) + value.slice(le)
      return {
        state: { ...state, value: next, selectionStart: pos, selectionEnd: pos, register: removed, mode: 'insert' },
        consumed: true,
      }
    }
    case 'p': {
      if (!state.register) return { state: caret(state, pos), consumed: true }
      if (state.register.endsWith(LINE)) {
        // Linewise paste: insert on the line below.
        const insertAt = le < value.length ? le + 1 : value.length
        const text = le < value.length ? state.register : LINE + state.register.replace(/\n$/, '')
        const next = value.slice(0, insertAt) + text + value.slice(insertAt)
        const caretPos = le < value.length ? insertAt : insertAt + 1
        return { state: { ...state, value: next, selectionStart: caretPos, selectionEnd: caretPos, mode: 'normal' }, consumed: true }
      }
      // Charwise paste: after the cursor.
      const at = le > ls ? pos + 1 : pos
      const next = value.slice(0, at) + state.register + value.slice(at)
      const caretPos = clampNormal(next, at + state.register.length - 1)
      return { state: { ...state, value: next, selectionStart: caretPos, selectionEnd: caretPos, mode: 'normal' }, consumed: true }
    }
    case 'P': {
      if (!state.register) return { state: caret(state, pos), consumed: true }
      if (state.register.endsWith(LINE)) {
        const next = value.slice(0, ls) + state.register + value.slice(ls)
        return { state: { ...state, value: next, selectionStart: ls, selectionEnd: ls, mode: 'normal' }, consumed: true }
      }
      const next = value.slice(0, pos) + state.register + value.slice(pos)
      const caretPos = clampNormal(next, pos + state.register.length - 1)
      return { state: { ...state, value: next, selectionStart: caretPos, selectionEnd: caretPos, mode: 'normal' }, consumed: true }
    }
    case 'd':
    case 'c':
      // `dd`/`cc`/`dw`/`cw`/`ciw` are multi-key operators; the host tracks the
      // pending operator and calls `applyVimOperator` for the follow-up key. A
      // lone `d`/`c` is consumed and arms the pending state.
      return { state, consumed: true }

    case 'Escape':
      // Already Normal; swallow so it does not blur/clear via other handlers.
      return { state: caret(state, pos), consumed: true }

    default:
      // Any other printable key in Normal mode is swallowed (it must NOT type a
      // literal character), but unimplemented control-ish keys (Tab, Enter)
      // pass through so the composer's own handlers still run.
      if (key.key.length === 1) return { state: caret(state, pos), consumed: true }
      return pass
  }
}

/** Second key of a `g`-prefixed sequence. Only `gg` (go to first line) is
 *  implemented; any other key cancels the pending `g` without typing it. */
export function applyVimPendingG(state: VimEditorState, key: VimKey): VimKeyResult {
  if (key.key === 'g') {
    return { state: caret(state, 0), consumed: true }
  }
  // Re-dispatch the key as a fresh Normal-mode key so e.g. `g$` still moves to
  // EOL rather than being lost.
  return applyVimKey(state, key)
}

/**
 * Second key of a `d`/`c` operator. Implements the line and word objects a
 * prose composer uses: `dd`/`cc` (whole line), `dw`/`cw` (to next word), and
 * `diw`/`ciw` (inner word) via the three-key path in `applyVimTextObject`.
 */
export function applyVimOperator(state: VimEditorState, operator: 'd' | 'c', key: VimKey): VimKeyResult {
  const { value } = state
  const pos = clampNormal(value, state.selectionStart)
  const ls = lineStart(value, pos)
  const le = lineEnd(value, pos)
  const enterInsert = operator === 'c'

  // `dd` / `cc` — whole line (linewise).
  if (key.key === operator) {
    if (enterInsert) {
      // `cc`: clear the line's text, stay on the (now empty) line, enter Insert.
      const removed = value.slice(ls, le)
      const next = value.slice(0, ls) + value.slice(le)
      return { state: { ...state, value: next, selectionStart: ls, selectionEnd: ls, register: removed + LINE, mode: 'insert' }, consumed: true }
    }
    // `dd`: remove the line including its trailing newline (or the leading one
    // for the last line), yank linewise.
    const delStart = ls
    const delEnd = le < value.length ? le + 1 : ls > 0 ? ls - 0 : le
    let next: string
    let removed: string
    if (le < value.length) {
      removed = value.slice(ls, le) + LINE
      next = value.slice(0, ls) + value.slice(le + 1)
    } else {
      // Last line: also drop the preceding newline if any.
      const prevNl = ls > 0 ? ls - 1 : 0
      removed = value.slice(ls, le) + LINE
      next = value.slice(0, prevNl) + value.slice(le)
    }
    void delStart; void delEnd
    const caretPos = clampNormal(next, Math.min(ls, next.length))
    return { state: { ...state, value: next, selectionStart: caretPos, selectionEnd: caretPos, register: removed, mode: 'normal' }, consumed: true }
  }

  // `dw` / `cw` — to the next word boundary (charwise). `cw` acts like `ce`:
  // it changes to the end of the current word, not the start of the next.
  if (key.key === 'w') {
    const to = enterInsert ? wordEndExclusive(value, pos) : nextWord(value, pos)
    const removed = value.slice(pos, to)
    const next = value.slice(0, pos) + value.slice(to)
    return {
      state: { ...state, value: next, selectionStart: enterInsert ? pos : clampNormal(next, pos), selectionEnd: enterInsert ? pos : clampNormal(next, pos), register: removed, mode: enterInsert ? 'insert' : 'normal' },
      consumed: true,
    }
  }

  // `b` — to the previous word boundary.
  if (key.key === 'b') {
    const to = prevWord(value, pos)
    const removed = value.slice(to, pos)
    const next = value.slice(0, to) + value.slice(pos)
    return {
      state: { ...state, value: next, selectionStart: to, selectionEnd: to, register: removed, mode: enterInsert ? 'insert' : 'normal' },
      consumed: true,
    }
  }

  // `$` — to end of line.
  if (key.key === '$') {
    const removed = value.slice(pos, le)
    const next = value.slice(0, pos) + value.slice(le)
    return {
      state: { ...state, value: next, selectionStart: enterInsert ? pos : clampNormal(next, pos), selectionEnd: enterInsert ? pos : clampNormal(next, pos), register: removed, mode: enterInsert ? 'insert' : 'normal' },
      consumed: true,
    }
  }

  // `i` begins a text object (`diw`/`ciw`); the host arms a third-key pending
  // state and calls `applyVimTextObject`. A lone `i` here is consumed.
  if (key.key === 'i') {
    return { state, consumed: true }
  }

  // Any other follow-up cancels the operator without editing.
  return { state: caret(state, pos), consumed: true }
}

/** `diw` / `ciw` — inner word (the word under the cursor, no surrounding
 *  whitespace). Only the `w` object is implemented. */
export function applyVimTextObject(state: VimEditorState, operator: 'd' | 'c', key: VimKey): VimKeyResult {
  const { value } = state
  const pos = clampNormal(value, state.selectionStart)
  const enterInsert = operator === 'c'
  if (key.key !== 'w') {
    return { state: caret(state, pos), consumed: true }
  }
  const [start, end] = innerWord(value, pos)
  const removed = value.slice(start, end)
  const next = value.slice(0, start) + value.slice(end)
  return {
    state: { ...state, value: next, selectionStart: enterInsert ? start : clampNormal(next, start), selectionEnd: enterInsert ? start : clampNormal(next, start), register: removed, mode: enterInsert ? 'insert' : 'normal' },
    consumed: true,
  }
}

/** Exclusive end index of the word under / after the cursor, for `cw`/`ce`. */
function wordEndExclusive(value: string, pos: number): number {
  const n = value.length
  if (pos >= n) return n
  let i = pos
  const cls = classOf(value[i])
  if (cls === 'space') {
    while (i < n && isSpace(value[i])) i++
    return i
  }
  while (i < n && classOf(value[i]) === cls) i++
  return i
}

/** [start, end) of the inner word under the cursor. */
function innerWord(value: string, pos: number): [number, number] {
  const n = value.length
  if (n === 0) return [0, 0]
  const i = Math.min(pos, n - 1)
  const cls = classOf(value[i])
  let start = i
  let end = i + 1
  while (start > 0 && classOf(value[start - 1]) === cls) start--
  while (end < n && classOf(value[end]) === cls) end++
  return [start, end]
}
