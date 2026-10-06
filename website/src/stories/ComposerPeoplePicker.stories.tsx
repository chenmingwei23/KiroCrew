import { useLayoutEffect, useRef, useState } from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import PeoplePickerMenu from '../components/PeoplePickerMenu'
import { MEMBERS_ROSTER_QUERY_KEY } from '../api/membersQuery'
import type { MemberRosterRow } from '../api/client'

/** Visual fixture for the composer's #people mention picker (issue #10639).
 *  Mounts the REAL PeoplePickerMenu, anchored to a mock composer input, with
 *  the roster query pre-seeded so the menu renders without a network fetch. */
type State = 'populated' | 'filtered' | 'no-match' | 'loading'

const ROSTER: MemberRosterRow[] = [
  { name: 'Release Captain', slug: 'release-captain', slot_key: '', running: false, workspace: 'kirocrew/release' },
  { name: 'Ops Lead', slug: 'ops-lead', slot_key: '', running: false, kiro_agent: 'default' },
  { name: 'Rosa Manueor', slug: 'rmanueor', slot_key: '', running: false, workspace: 'kirocrew/dashboard' },
  { name: 'Riku Masuda', slug: 'rmasuda', slot_key: '', running: false, kiro_agent: 'reviewer' },
  { name: 'Security On-Call', slug: 'security-oncall', slot_key: '', running: false, workspace: 'kirocrew/security' },
]

function PeoplePickerEvidence({ state }: { state: State }) {
  const anchorRef = useRef<HTMLTextAreaElement>(null)
  const [, force] = useState(0)
  // The menu returns null until anchorRef.current exists; nudge one re-render
  // after the textarea mounts so it paints without needing a focus event.
  useLayoutEffect(() => { force(n => n + 1) }, [])
  const [queryClient] = useState(() => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // Pre-seed the roster cache for every state except 'loading', where we
    // leave it empty so useQuery stays in its pending state.
    if (state !== 'loading') qc.setQueryData(MEMBERS_ROSTER_QUERY_KEY, ROSTER)
    return qc
  })
  // The query (empty 'filter' lists all) is driven by the typed fragment.
  const query = state === 'filtered' ? 'rm' : state === 'no-match' ? 'zzz' : ''

  return (
    <QueryClientProvider client={queryClient}>
      <div style={{ position: 'absolute', left: 320, bottom: 72, width: 680 }}>
        <textarea
          ref={anchorRef}
          readOnly
          aria-label="Composer input (fixture)"
          value={state === 'populated' ? '#' : state === 'loading' ? '#' : `#${query}`}
          className="w-full rounded-lg border border-border bg-card px-3 py-2 text-text"
          style={{ height: 44 }}
        />
        <PeoplePickerMenu
          query={query}
          anchorRef={anchorRef}
          open
          onSelect={() => {}}
          onClose={() => {}}
        />
      </div>
    </QueryClientProvider>
  )
}

const meta = {
  title: 'Evidence/Composer people picker',
  component: PeoplePickerEvidence,
  args: { state: 'populated' },
} satisfies Meta<typeof PeoplePickerEvidence>

export default meta
type Story = StoryObj<typeof meta>

export const Populated: Story = {}
export const Filtered: Story = { args: { state: 'filtered' } }
export const NoMatch: Story = { args: { state: 'no-match' } }
export const Loading: Story = { args: { state: 'loading' } }
