import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createVoiceEndpoints } from '../api/client/voice'
import { ApiError } from '../api/apiError'
import { clientSpeechAvailable, speakOnClient, stopClientSpeech } from '../lib/clientSpeech'

// ── clientSpeech helper ───────────────────────────────────────────────

class FakeUtterance {
  onend: (() => void) | null = null
  onerror: ((e: { error: string }) => void) | null = null
  constructor(public text: string) {}
}

function installSpeechSynthesis() {
  const spoken: FakeUtterance[] = []
  let cancelCount = 0
  const synth = {
    speak: (u: FakeUtterance) => { spoken.push(u) },
    cancel: () => { cancelCount++ },
  }
  ;(window as unknown as { speechSynthesis: unknown }).speechSynthesis = synth
  ;(window as unknown as { SpeechSynthesisUtterance: unknown }).SpeechSynthesisUtterance = FakeUtterance
  return { spoken, synth, getCancelCount: () => cancelCount }
}

function removeSpeechSynthesis() {
  delete (window as unknown as { speechSynthesis?: unknown }).speechSynthesis
  delete (window as unknown as { SpeechSynthesisUtterance?: unknown }).SpeechSynthesisUtterance
}

describe('clientSpeech helper', () => {
  afterEach(() => { removeSpeechSynthesis() })

  it('reports unavailable when the webview has no speech API', () => {
    removeSpeechSynthesis()
    expect(clientSpeechAvailable()).toBe(false)
    const onPlaying = vi.fn()
    const onError = vi.fn()
    expect(speakOnClient('hello', { onPlaying, onError })).toBe(false)
    expect(onPlaying).not.toHaveBeenCalled()
  })

  it('speaks on the client and brackets playback with onPlaying', () => {
    const { spoken } = installSpeechSynthesis()
    expect(clientSpeechAvailable()).toBe(true)
    const onPlaying = vi.fn()
    const onError = vi.fn()
    expect(speakOnClient('read this aloud', { onPlaying, onError })).toBe(true)
    expect(spoken).toHaveLength(1)
    expect(spoken[0].text).toBe('read this aloud')
    expect(onPlaying).toHaveBeenLastCalledWith(true)
    spoken[0].onend?.()
    expect(onPlaying).toHaveBeenLastCalledWith(false)
    expect(onError).not.toHaveBeenCalled()
  })

  it('does not report a user cancel as an error', () => {
    const { spoken } = installSpeechSynthesis()
    const onPlaying = vi.fn()
    const onError = vi.fn()
    speakOnClient('x', { onPlaying, onError })
    spoken[0].onerror?.({ error: 'canceled' })
    expect(onPlaying).toHaveBeenLastCalledWith(false)
    expect(onError).not.toHaveBeenCalled()
  })

  it('reports a genuine synthesis error', () => {
    const { spoken } = installSpeechSynthesis()
    const onPlaying = vi.fn()
    const onError = vi.fn()
    speakOnClient('x', { onPlaying, onError })
    spoken[0].onerror?.({ error: 'synthesis-failed' })
    expect(onError).toHaveBeenCalledWith('voice_playback_failed')
  })

  it('cancels the engine on stop', () => {
    const { getCancelCount } = installSpeechSynthesis()
    speakOnClient('x', { onPlaying: vi.fn(), onError: vi.fn() })
    stopClientSpeech()
    expect(getCancelCount()).toBeGreaterThan(0)
  })

  it('refuses to speak empty text', () => {
    installSpeechSynthesis()
    expect(speakOnClient('   ', { onPlaying: vi.fn(), onError: vi.fn() })).toBe(false)
  })
})

// ── voiceSynthesize fallback routing ──────────────────────────────────

function makeEndpoints(rejectCode: string | null) {
  // Mirror the real flow: post(...).then(j). `j` throws the ApiError, exactly
  // as the dashboard transport does for a non-2xx synthesis response.
  const post = vi.fn().mockResolvedValue({} as Response)
  const j = vi.fn().mockImplementation(() => {
    if (rejectCode === null) return Promise.resolve({ ok: true })
    return Promise.reject(new ApiError(502, 'no audio', JSON.stringify({ code: rejectCode })))
  })
  const transport = {
    post, put: vi.fn(), j, jfetch: vi.fn(),
  } as unknown as Parameters<typeof createVoiceEndpoints>[0]
  const { synthesis } = createVoiceEndpoints(transport)
  return { synthesis, post }
}

function captureEvents(types: string[]) {
  const seen: { type: string; detail: unknown }[] = []
  const handlers = types.map(type => {
    const h = (e: Event) => seen.push({ type, detail: (e as CustomEvent).detail })
    window.addEventListener(type, h)
    return { type, h }
  })
  return { seen, dispose: () => handlers.forEach(({ type, h }) => window.removeEventListener(type, h)) }
}

describe('voiceSynthesize client fallback routing', () => {
  beforeEach(() => { installSpeechSynthesis() })
  afterEach(() => { removeSpeechSynthesis() })

  it('routes a voice_unavailable manual failure to client speech, not a failure notice', async () => {
    const { synthesis } = makeEndpoints('voice_unavailable')
    const cap = captureEvents(['voice-client-speak', 'voice-synthesis-failed'])
    try {
      await synthesis.voiceSynthesize('chat-a', 'speak me', { clientFallback: true })
    } finally {
      cap.dispose()
    }
    const speak = cap.seen.find(e => e.type === 'voice-client-speak')
    expect(speak).toBeTruthy()
    expect((speak!.detail as { text: string }).text).toBe('speak me')
    // No failure notice when we fall back.
    expect(cap.seen.some(e => e.type === 'voice-synthesis-failed')).toBe(false)
  })

  it('keeps the failure notice for voice_unavailable when no client fallback was requested', async () => {
    const { synthesis } = makeEndpoints('voice_unavailable')
    const cap = captureEvents(['voice-client-speak', 'voice-synthesis-failed'])
    try {
      await expect(synthesis.voiceSynthesize('chat-a', 'x')).rejects.toBeInstanceOf(ApiError)
    } finally {
      cap.dispose()
    }
    expect(cap.seen.some(e => e.type === 'voice-client-speak')).toBe(false)
    expect(cap.seen.some(e => e.type === 'voice-synthesis-failed')).toBe(true)
  })

  it('does NOT fall back for a non-voice_unavailable failure even with clientFallback', async () => {
    const { synthesis } = makeEndpoints('voice_consent_required')
    const cap = captureEvents(['voice-client-speak', 'voice-synthesis-failed'])
    try {
      await expect(
        synthesis.voiceSynthesize('chat-a', 'x', { clientFallback: true }),
      ).rejects.toBeInstanceOf(ApiError)
    } finally {
      cap.dispose()
    }
    expect(cap.seen.some(e => e.type === 'voice-client-speak')).toBe(false)
    const failed = cap.seen.find(e => e.type === 'voice-synthesis-failed')
    expect((failed!.detail as { code: string }).code).toBe('voice_consent_required')
  })

  it('falls back to a failure notice when the webview cannot speak', async () => {
    removeSpeechSynthesis()
    const { synthesis } = makeEndpoints('voice_unavailable')
    const cap = captureEvents(['voice-client-speak', 'voice-synthesis-failed'])
    try {
      await expect(
        synthesis.voiceSynthesize('chat-a', 'x', { clientFallback: true }),
      ).rejects.toBeInstanceOf(ApiError)
    } finally {
      cap.dispose()
    }
    expect(cap.seen.some(e => e.type === 'voice-client-speak')).toBe(false)
    expect(cap.seen.some(e => e.type === 'voice-synthesis-failed')).toBe(true)
  })
})
