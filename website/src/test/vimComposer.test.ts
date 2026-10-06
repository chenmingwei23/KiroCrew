/**
 * Unit tests for the composer Vim keymap engine (#6321).
 *
 * The engine is a pure function over editor state, so every motion and edit is
 * tested with plain objects — no DOM, no React. These pin the behaviour the
 * ChatInput integration relies on, and the accessibility invariant that Insert
 * mode passes every key but Escape straight through.
 */
import { describe, it, expect } from 'vitest'
import {
  applyVimKey,
  applyVimPendingG,
  applyVimOperator,
  applyVimTextObject,
  type VimEditorState,
  type VimKey,
} from '../components/chat-input/vimComposer'

const key = (k: string, mods: Partial<VimKey> = {}): VimKey => ({
  key: k,
  shiftKey: false,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  ...mods,
})

const state = (value: string, pos: number, mode: 'normal' | 'insert' = 'normal', register = ''): VimEditorState => ({
  value,
  selectionStart: pos,
  selectionEnd: pos,
  mode,
  register,
})

describe('insert-mode passthrough (accessibility fail-safe)', () => {
  it('passes every ordinary key straight through', () => {
    for (const k of ['a', 'x', 'Enter', 'Tab', 'ArrowLeft', '1', ' ']) {
      const r = applyVimKey(state('hello', 2, 'insert'), key(k))
      expect(r.consumed, `key ${k}`).toBe(false)
    }
  })

  it('consumes Escape to leave Insert and steps the caret left', () => {
    const r = applyVimKey(state('hello', 3, 'insert'), key('Escape'))
    expect(r.consumed).toBe(true)
    expect(r.state.mode).toBe('normal')
    expect(r.state.selectionStart).toBe(2)
  })

  it('passes modified keys through in insert mode (shortcuts win)', () => {
    const r = applyVimKey(state('hi', 1, 'insert'), key('Enter', { metaKey: true }))
    expect(r.consumed).toBe(false)
  })
})

describe('normal-mode motions', () => {
  it('h / l move within the line and clamp at bounds', () => {
    expect(applyVimKey(state('abc', 1), key('h')).state.selectionStart).toBe(0)
    expect(applyVimKey(state('abc', 0), key('h')).state.selectionStart).toBe(0)
    expect(applyVimKey(state('abc', 1), key('l')).state.selectionStart).toBe(2)
    // l cannot pass the last char of the line
    expect(applyVimKey(state('abc', 2), key('l')).state.selectionStart).toBe(2)
  })

  it('0 and $ jump to line start / last char', () => {
    expect(applyVimKey(state('hello world', 5), key('0')).state.selectionStart).toBe(0)
    expect(applyVimKey(state('hello world', 2), key('$')).state.selectionStart).toBe(10)
  })

  it('w / b move by word', () => {
    expect(applyVimKey(state('foo bar baz', 0), key('w')).state.selectionStart).toBe(4)
    expect(applyVimKey(state('foo bar baz', 4), key('b')).state.selectionStart).toBe(0)
  })

  it('j / k move between lines preserving column', () => {
    const v = 'abcd\nef\nghij'
    // on line 0 col 3 → line 1 clamps to its last char (col 1)
    expect(applyVimKey(state(v, 3), key('j')).state.selectionStart).toBe(6)
    // from line 2 col 1 → line 1
    expect(applyVimKey(state(v, 9), key('k')).state.selectionStart).toBe(6)
  })

  it('gg goes to the top, G to the last line', () => {
    const v = 'one\ntwo\nthree'
    expect(applyVimPendingG(state(v, 9), key('g')).state.selectionStart).toBe(0)
    expect(applyVimKey(state(v, 0), key('G')).state.selectionStart).toBe(8)
  })

  it('swallows a bare printable key so it does not type a literal', () => {
    const r = applyVimKey(state('abc', 0), key('z'))
    expect(r.consumed).toBe(true)
    expect(r.state.value).toBe('abc')
  })
})

describe('entering insert mode', () => {
  it('i stays, a steps right, A goes to EOL, I to first non-blank', () => {
    expect(applyVimKey(state('abc', 1), key('i')).state).toMatchObject({ mode: 'insert', selectionStart: 1 })
    expect(applyVimKey(state('abc', 1), key('a')).state).toMatchObject({ mode: 'insert', selectionStart: 2 })
    expect(applyVimKey(state('abc', 0), key('A')).state).toMatchObject({ mode: 'insert', selectionStart: 3 })
    expect(applyVimKey(state('  ab', 3), key('I')).state).toMatchObject({ mode: 'insert', selectionStart: 2 })
  })

  it('o opens a line below and O above, both entering insert', () => {
    const below = applyVimKey(state('a\nb', 0), key('o')).state
    expect(below.value).toBe('a\n\nb')
    expect(below).toMatchObject({ mode: 'insert', selectionStart: 2 })
    const above = applyVimKey(state('a\nb', 2), key('O')).state
    expect(above.value).toBe('a\n\nb')
    expect(above).toMatchObject({ mode: 'insert', selectionStart: 2 })
  })
})

describe('edits and the register', () => {
  it('x deletes the char under the cursor and yanks it', () => {
    const r = applyVimKey(state('abc', 1), key('x'))
    expect(r.state.value).toBe('ac')
    expect(r.state.register).toBe('b')
  })

  it('D deletes to end of line', () => {
    const r = applyVimKey(state('hello world', 6), key('D'))
    expect(r.state.value).toBe('hello ')
    expect(r.state.register).toBe('world')
  })

  it('C changes to end of line and enters insert', () => {
    const r = applyVimKey(state('hello world', 6), key('C'))
    expect(r.state.value).toBe('hello ')
    expect(r.state.mode).toBe('insert')
  })

  it('p pastes charwise after the cursor', () => {
    const r = applyVimKey(state('ac', 0, 'normal', 'b'), key('p'))
    expect(r.state.value).toBe('abc')
  })

  it('dd deletes the whole line linewise', () => {
    const r = applyVimOperator(state('one\ntwo\nthree', 4), 'd', key('d'))
    expect(r.state.value).toBe('one\nthree')
    expect(r.state.register).toBe('two\n')
  })

  it('p pastes a linewise register on the line below', () => {
    const yanked = applyVimOperator(state('one\ntwo', 0), 'd', key('d')).state
    // yanked 'one\n', remaining 'two'
    const r = applyVimKey({ ...yanked, selectionStart: 0, selectionEnd: 0 }, key('p'))
    expect(r.state.value).toBe('two\none')
  })

  it('cc clears the line and enters insert', () => {
    const r = applyVimOperator(state('one\ntwo', 0), 'c', key('c'))
    expect(r.state.value).toBe('\ntwo')
    expect(r.state.mode).toBe('insert')
  })

  it('dw deletes to the next word', () => {
    const r = applyVimOperator(state('foo bar', 0), 'd', key('w'))
    expect(r.state.value).toBe('bar')
  })

  it('diw deletes the inner word under the cursor', () => {
    const r = applyVimTextObject(state('foo bar baz', 5), 'd', key('w'))
    expect(r.state.value).toBe('foo  baz')
    expect(r.state.register).toBe('bar')
  })

  it('ciw changes the inner word and enters insert', () => {
    const r = applyVimTextObject(state('foo bar baz', 5), 'c', key('w'))
    expect(r.state.value).toBe('foo  baz')
    expect(r.state.mode).toBe('insert')
  })

  it('p with an empty register is a no-op', () => {
    const r = applyVimKey(state('abc', 0, 'normal', ''), key('p'))
    expect(r.state.value).toBe('abc')
    expect(r.consumed).toBe(true)
  })
})
