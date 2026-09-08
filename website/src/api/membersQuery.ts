import { api, type MemberRosterRow } from './client'

/**
 * The Crew Members page's React Query definitions (issue #9418).
 *
 * One module so the page, its tests and any future consumer (the sidebar's
 * Crew section, a command-palette provider) spell each key exactly once —
 * two inline spellings with different options would diverge silently.
 *
 * Roster freshness follows the dashboard's sanctioned pattern (see
 * queryClient.ts): pushed invalidation first, a finite staleTime as the floor.
 * The roster is a projection of the crew registry, so its key lives UNDER
 * `['kirocrew-agents']` — the prefix useWebSocket invalidates on every
 * `refresh` frame and the crew editor invalidates after a save — and a crew
 * created, renamed or starred anywhere reaches this cache without the page
 * knowing who wrote. The second segment keeps it a distinct entry: the
 * registry query returns `{ agents, default_agent }`, this one returns rows,
 * and sharing one key would let whichever mounted first decide the shape.
 */
export const MEMBERS_ROSTER_QUERY_KEY = ['kirocrew-agents', 'members-roster'] as const

/** Finite so a return to the page after this long refetches in the
 *  background (and on focus) while the cached roster renders immediately —
 *  the list is never blank on a revisit, only silently refreshed. Matches
 *  the other registry projections (`default-agent`). */
export const MEMBERS_ROSTER_STALE_MS = 30_000

export const membersRosterQuery = {
  queryKey: MEMBERS_ROSTER_QUERY_KEY,
  queryFn: (): Promise<MemberRosterRow[]> => api.members().then((r) => r.members),
  staleTime: MEMBERS_ROSTER_STALE_MS,
}

/** Recent-activity pointers for one member's drawer. Keyed by the exact
 *  member NAME as well as the slug — slugs are lossy, and the backend's
 *  member filter exists precisely so two names sharing a slug keep distinct
 *  histories. */
export const memberActivityQueryKey = (slug: string, member: string) =>
  ['member-activity', slug, member] as const

/**
 * The outcome of the last thread open for one member — what
 * POST /api/members/{slug}/thread answered. Written by the open mutation
 * (`setQueryData`), never fetched by a queryFn: the endpoint is a write (the
 * idempotent creator/repairer of member slots, and the ONLY one), so it goes
 * through `useMutation` on every open, while its answer is cached here so a
 * return to a member mounts the thread at once and the re-POST repairs in
 * the background. Keyed by name, not slug: a lossy-slug collision is a
 * per-NAME fact (`Oncall` collides, `oncall` owns the thread).
 */
export const memberThreadQueryKey = (member: string) => ['member-thread', member] as const

export interface MemberThreadOutcome {
  /** The slot the thread mounts on; '' while no answer has confirmed one.
   *  A failed re-POST keeps the previous key (the cached thread stays up
   *  under the error line); a collision clears it (nothing here is safe to
   *  mount). */
  slot_key: string
  /** Set when the slug's thread belongs to another crew (first-bound-wins):
   *  the name of the crew that owns it. */
  collision?: string
  /** Set when the last POST failed. */
  failed?: boolean
}
