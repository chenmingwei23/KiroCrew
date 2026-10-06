import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { AtSign } from 'lucide-react'
import { membersRosterQuery } from '../api/membersQuery'
import type { MemberRosterRow } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { menuGeometry, bottomUpOrder } from '../lib/pickerMenu'
import type { SendMode } from '../pages/chat/ChatSettings'
import { i18nT } from '../i18n/t'

// — #people inline trigger autocomplete (issue #10639).
//
// Mirrors SkillPickerMenu but lists PEOPLE from the crew roster (GET
// /api/members, via the shared `membersRosterQuery`). The roster is the
// pluggable "directory" the issue asks for: a deployment backs /api/members
// however it identifies people, and the picker assumes nothing about the
// source beyond the ranked (slug, name) rows it returns. Selecting one inserts
// a `#slug` token — the full, well-formed alias — the same way the skill picker
// inserts a `$leaf` token: literal text the user still sees, resolved at
// selection time so a near-miss can never silently name nobody.

/** The person reference a selected row inserts, plus its display name. */
interface PersonMatch {
  /** The stable alias (member slug) that becomes the `#alias` token. */
  alias: string
  /** Display identity for the row (display_name overrides name). */
  label: string
  /** Optional disambiguator (the agent/workspace the member runs as). */
  detail: string
}

/** Collapse a roster row to the picker's (alias, label, detail) view. */
function toPersonMatch(row: MemberRosterRow): PersonMatch {
  return {
    alias: row.slug,
    label: row.display_name || row.name,
    // Workspace, then the agent template, is the cheapest disambiguator two
    // similarly-named crewmates differ on; empty when neither is set.
    detail: row.workspace || row.kiro_agent || '',
  }
}

interface Props {
  query: string
  anchorRef: React.RefObject<HTMLElement | null>
  open: boolean
  /** Receives the alias token to insert (e.g. "release-captain"). */
  onSelect: (info: { alias: string }) => void
  onClose: () => void
  /**
   * The composer's effective send binding (see ChatInput's SendMode). Only
   * read by the settled-empty copy: in 'ctrl-enter' mode a released bare
   * Enter is a newline, so the announcement must name Ctrl+Enter instead.
   */
  sendOnEnter?: SendMode
}

export default function PeoplePickerMenu({
  query, anchorRef, open, onSelect, onClose, sendOnEnter = 'enter',
}: Props) {
  const [results, setResults] = useState<PersonMatch[]>([])
  const resultsRef = useRef<PersonMatch[]>([])

  // Reuse the Crew Members page's roster query verbatim: same key, same fetch,
  // same staleTime, so this picker and that page share one cache entry and a
  // crew created/renamed anywhere reaches both. `enabled: open` keeps the menu
  // lazy — the fetch is the only latency, and it is cheap on a warm cache.
  const { data, isLoading, isFetching, isError } = useQuery<MemberRosterRow[]>({
    ...membersRosterQuery,
    enabled: open,
  })
  const loading = isLoading && open

  const rows = useMemo(() => (data ?? []).map(toPersonMatch), [data])

  // Filter by alias OR label substring (case-insensitive). Empty query lists
  // all, capped for menu height. Dedupe by alias so the same `#token` is never
  // ambiguous (mirrors the skill picker's leaf dedupe). Memoized so the
  // keyboard-release gate reads the SAME render's match set.
  const matched = useMemo(() => {
    const q = query.toLowerCase()
    const seen = new Set<string>()
    const out: PersonMatch[] = []
    for (const p of rows) {
      if (q && !p.alias.toLowerCase().includes(q) && !p.label.toLowerCase().includes(q)) continue
      if (seen.has(p.alias)) continue
      seen.add(p.alias)
      out.push(p)
    }
    return out.slice(0, 50)
  }, [rows, query])

  const choose = useCallback((idx: number) => {
    const r = resultsRef.current
    const p = r[idx >= r.length ? 0 : idx]
    if (!p) return
    onSelect({ alias: p.alias })
  }, [onSelect])

  // "Settled and genuinely empty": only then does the menu release Enter/Tab so
  // the composer's own Enter still works. A settled ERROR counts as empty — the
  // list has nothing to offer either way. Same gate shape as SkillPickerMenu's.
  const releaseKeysWhenEmpty = !isFetching && (data !== undefined || isError) && matched.length === 0

  const { selected, setSelected, selectedRef, itemRefs } = useListKeyboardNav({
    open,
    count: results.length,
    onChoose: choose,
    onClose,
    releaseKeysWhenEmpty,
  })

  // Populate bottom-up when the menu opens above the input (shared helper), so
  // the first row sits at the bottom nearest the cursor with the initial
  // selection on it; when it flips below, keep it at the top.
  useEffect(() => {
    if (!open) return
    const above = anchorRef.current
      ? menuGeometry(anchorRef.current, matched.length, 48, 0).above
      : false
    const { ordered, initialIndex } = bottomUpOrder(matched, above)
    setResults(ordered); resultsRef.current = ordered
    setSelected(initialIndex)
  }, [matched, open, anchorRef, setSelected])

  // Scroll the selected row into view once results render (the filter effect
  // selects before the rows mount). Keyed on [results] so per-keystroke arrow
  // nav — which the hook already scrolls — is not fought here.
  useEffect(() => {
    if (!open) return
    itemRefs.current[selectedRef.current]?.scrollIntoView({ block: 'nearest' })
  }, [results, open, selectedRef, itemRefs])

  if (!open || !anchorRef.current) return null

  const { above, top, bottom, left, width, maxHeight } = menuGeometry(
    anchorRef.current, results.length, 48, 0,
  )

  const emptyKey = loading
    ? (sendOnEnter === 'ctrl-enter'
        ? 'components.peoplePickerMenu.loading_ctrl_enter_held'
        : 'components.peoplePickerMenu.loading_enter_held')
    : isError
    ? (!releaseKeysWhenEmpty
        ? 'components.peoplePickerMenu.load_failed'
        : sendOnEnter === 'ctrl-enter'
          ? 'components.peoplePickerMenu.load_failed_ctrl_enter_sends'
          : 'components.peoplePickerMenu.load_failed_enter_sends')
    : (!releaseKeysWhenEmpty
        ? 'components.peoplePickerMenu.no_matching_people'
        : sendOnEnter === 'ctrl-enter'
          ? 'components.peoplePickerMenu.no_matching_people_ctrl_enter_sends'
          : 'components.peoplePickerMenu.no_matching_people_enter_sends')

  const empty = (
    // role="status": the Enter-meaning flip (pick → send) is otherwise
    // announced only visually, and a screen-reader user would hit the very
    // silent-send surprise this copy exists to prevent.
    <div role="status" className="px-3 py-3 text-[12px] text-muted">{i18nT(emptyKey)}</div>
  )

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg py-1 animate-slide-up flex flex-col"
      style={{ ...(above ? { bottom } : { top }), left, width: Math.min(width, 460), maxHeight }}
    >
      <div role="listbox" className="overflow-y-auto flex-1 min-h-0">
        {results.length === 0 ? empty : results.map((p, i) => (
          <div
            role="option"
            aria-selected={i === selected}
            tabIndex={-1}
            key={p.alias}
            ref={el => { itemRefs.current[i] = el }}
            className={`w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle text-text' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
            title={p.alias}
            onMouseEnter={() => setSelected(i)}
            onMouseDown={e => { e.preventDefault(); onSelect({ alias: p.alias }) }}
          >
            <AtSign size={14} className="shrink-0 lucide-inline" />
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-mono font-semibold truncate">#{p.alias}</div>
              <div className="text-[11px] text-muted truncate">{p.detail ? `${p.label} · ${p.detail}` : p.label}</div>
            </div>
          </div>
        ))}
      </div>
    </div>,
    document.body,
  )
}
