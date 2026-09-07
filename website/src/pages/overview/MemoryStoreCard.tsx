import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { AlertTriangle, Database, Plus } from 'lucide-react'
import { api } from '../../api/client'
import { retryPolicy } from '../../api/queryClient'
import { parseErrorCode } from '../../utils/errorReport'
import { Card, CardTitle, Btn, Badge, Input } from '../../components/ui'
import Modal from '../../components/Modal'
import SimpleSelect from '../../components/SimpleSelect'
import { fmtNumber, fmtDateTimeNumeric } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { MemoryStoreSummary } from '../../types'

/**
 * The Memory tab's scope header: which memory store the page is reading, what is
 * in it, and how to declare another one.
 *
 * Also the owner of the store-scope vocabulary the sibling cards share — the
 * query keys, the retry policy, and {@link MemoryScopeNotice}. One module holds
 * them so a refusal reads identically on every card that can hit it.
 */

/** React Query key for the store listing. Its own resource, with no store
 *  parameter: `GET /api/memory/stores` enumerates every silo and is owner-gated
 *  unconditionally. */
export const MEMORY_STORES_KEY = ['memory-stores'] as const

/** Every `/api/memory/*` query-key PREFIX this tab owns.
 *
 *  The page's global refresh invalidates the family through these rather than
 *  naming each store, because a prefix match also reaches the stores a user
 *  looked at earlier in the session and whose cached rows would otherwise
 *  survive the refresh. */
export const MEMORY_QUERY_PREFIXES: readonly string[][] = [
  ['memory-stores'],
  ['memory-doc'],
  ['memory-retired'],
  ['memory-backups'],
  ['memory-carve'],
]

/** Statuses whose refusal is settled: the owner gate, an undeclared store name,
 *  and a lineage that has no facets. A retry cannot change any of those answers,
 *  it only delays the sentence the card is about to render. Everything else keeps
 *  the client's default ladder, which is what still buys the 429 tunnel retries. */
const SETTLED_REFUSAL_STATUSES: ReadonlySet<number> = new Set([400, 403, 404, 409])

/** The example store name in the New-store field, deliberately NOT a catalog
 *  value. A store name is a directory name the gateway validates against
 *  `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`, so a translated example would suggest a
 *  name the create call then refuses — and for the six non-Latin locales it could
 *  not suggest a legal one at all. */
const EXAMPLE_STORE_NAME = 'finance'

/** Retry policy for every store-scoped memory query. */
export const memoryQueryRetry = (failureCount: number, error: unknown): boolean => {
  const status = (error as { status?: unknown } | null)?.status
  if (typeof status === 'number' && SETTLED_REFUSAL_STATUSES.has(status)) return false
  return retryPolicy(failureCount, error)
}

/** The machine-readable `code` a memory route put in its refusal body, if any.
 *  Read structurally rather than through `instanceof ApiError`, because the class
 *  identity does not survive a module mock that omits the export — and a helper
 *  that throws while rendering an error is worse than one that misses a code. */
export function memoryErrorCode(error: unknown): string | undefined {
  const body = (error as { body?: unknown } | null)?.body
  return parseErrorCode(typeof body === 'string' ? body : undefined)
}

/** The server's own sentence for a refusal. Backend strings have no catalog path,
 *  so this is rendered verbatim and is the fallback for a code this UI does not
 *  recognise — a blank card is the one outcome that must not happen. */
function serverMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '')
}

/**
 * A readable line for a store-scoped read that was refused.
 *
 * Renders nothing when there is no error. The recognised codes get a sentence
 * that says what to DO; anything else falls through to the server's message, so
 * an unrecognised refusal still explains itself.
 */
export function MemoryScopeNotice({ error }: { error: unknown }) {
  if (!error) return null
  const code = memoryErrorCode(error)
  return (
    <div className="flex items-start gap-1.5 text-[13px] text-danger" role="alert">
      <AlertTriangle className="lucide-inline mt-0.5 shrink-0" aria-hidden="true" />
      {code === 'owner_only' && (
        <span>{i18nT('pages.overview.memoryStoreCard.only_the_signed_in_dashboard_owner_can_address_a')}</span>
      )}
      {code === 'unknown_memory_store' && (
        <span>{i18nT('pages.overview.memoryStoreCard.this_install_does_not_declare_a_memory_store_by')}</span>
      )}
      {code === 'store_unavailable' && (
        <span>{i18nT('pages.overview.memoryStoreCard.this_store_is_declared_but_its_database_could_no')}</span>
      )}
      {code !== 'owner_only' && code !== 'unknown_memory_store' && code !== 'store_unavailable' && (
        <span>{serverMessage(error)}</span>
      )}
    </div>
  )
}

/** The store listing, shared by every caller through React Query's key dedupe so
 *  the tab issues one request however many cards ask for the list. */
export function useMemoryStores() {
  return useQuery({
    queryKey: MEMORY_STORES_KEY,
    queryFn: () => api.memoryStores(),
    retry: memoryQueryRetry,
  })
}

/** Stable empty listing, so a pending query does not hand consumers a fresh array
 *  identity on every render and re-run their effects. */
export const NO_MEMORY_STORES: readonly MemoryStoreSummary[] = []

/** A count the listing could not read comes back as `null`, which means "not
 *  known" and must never render as a zero — that would report an unreadable silo
 *  as an empty one. */
function CountValue({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) {
    return <span className="text-muted">{i18nT('pages.overview.memoryStoreCard.unknown')}</span>
  }
  return <>{fmtNumber(value)}</>
}

export default function MemoryStoreCard({
  store,
  onStoreChange,
}: {
  /** The store every card on the page is reading. `''` means no store has been
   *  named, so the requests carry no `store=` and the gateway serves the caller's
   *  own binding. */
  store: string
  onStoreChange: (store: string) => void
}) {
  const queryClient = useQueryClient()
  const stores = useMemoryStores()
  const rows = stores.data?.stores ?? NO_MEMORY_STORES
  // The store the caller ALREADY reads with no `store=` on the wire, named by the
  // gateway rather than guessed here: a dashboard session bound to a crew reads
  // that crew's silo, so assuming the default store would label the page with a
  // store it is not showing.
  const active = stores.data?.active ?? ''
  // What the picker DISPLAYS is `store` when one is named and the active store
  // otherwise. The two are deliberately not the same value; see `pick` below.
  const shown = store || active
  const selected = rows.find(s => s.name === shown)

  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')

  /** Translate a picked store into what goes ON THE WIRE.
   *
   * Picking the store the caller already reads sends NOTHING, and that is the
   * whole point of keeping the two apart. Sending `store=<active>` would be
   * semantically identical and behaviourally worse in two ways: the parameter's
   * presence takes the owner gate, so an install with no configured owner would
   * meet a refusal on a page that worked before; and it changes the value the
   * document cards are keyed on, which REMOUNTS them and silently discards a draft
   * the user had already typed — the save that follows then writes the stale
   * server copy back and reports success.
   *
   * Naming a DIFFERENT store still sends it, including the default store when the
   * caller's binding is a silo: that genuinely addresses another store's memory and
   * is exactly what the gate is for.
   */
  const pick = (name: string) => onStoreChange(name === active ? '' : name)

  const createStore = useMutation({
    mutationFn: (name: string) => api.createMemoryStore(name),
    onSuccess: res => {
      setCreating(false)
      setNewName('')
      queryClient.invalidateQueries({ queryKey: MEMORY_STORES_KEY })
      // Select it: a store declared and then not shown reads as a failed create.
      if (res.name) onStoreChange(res.name)
    },
  })
  const createCode = memoryErrorCode(createStore.error)

  const openCreate = (open: boolean) => {
    setCreating(open)
    if (!open) setNewName('')
    createStore.reset()
  }

  return (
    <Card>
      <CardTitle>
        <Database className="lucide-inline" aria-hidden="true" /> {i18nT('pages.overview.memoryStoreCard.memory_store')}
      </CardTitle>
      <div className="flex gap-2 items-center flex-wrap mb-2">
        <SimpleSelect
          aria-label={i18nT('pages.overview.memoryStoreCard.memory_store')}
          style={{ flex: '0 0 220px' }}
          options={rows.map(s => s.name)}
          value={shown}
          onChange={pick}
          disabled={rows.length === 0}
        />
        {selected?.is_default && (
          <Badge variant="muted">{i18nT('pages.overview.memoryStoreCard.shared_default_store')}</Badge>
        )}
        {selected && !selected.is_default && (
          <Badge variant="ok">{i18nT('pages.overview.memoryStoreCard.crew_silo')}</Badge>
        )}
        <Btn onClick={() => openCreate(true)}>
          <Plus className="lucide-inline" aria-hidden="true" /> {i18nT('pages.overview.memoryStoreCard.new_store')}
        </Btn>
      </div>
      <MemoryScopeNotice error={stores.error} />
      {/* The scope this picker actually reaches. Said here rather than left to the
          reader, because the cards below it are split: some take the store name on
          the wire and some have no parameter for it at all. */}
      <p className="text-[12px] leading-relaxed text-muted">
        {i18nT('pages.overview.memoryStoreCard.the_picker_drives_this_card_the_preferences_proj')}
      </p>
      {/* Each stat is ONE catalog key carrying its own `<v/>` slot, not a label key
          beside a value: a label ending in a colon pins every language to English
          word order, and several put the count first. */}
      {selected && (
        <div className="flex gap-4 items-center flex-wrap text-[13px] mt-2">
          <span className="text-muted">
            <Trans
              i18nKey="pages.overview.memoryStoreCard.schema_lineage"
              components={{ v: <span className="text-text">{selected.lineage}</span> }}
            />
          </span>
          <span className="text-muted">
            <Trans
              i18nKey="pages.overview.memoryStoreCard.facts_and_directives"
              components={{ v: <span className="text-text"><CountValue value={selected.semantic_count} /></span> }}
            />
          </span>
          <span className="text-muted">
            <Trans
              i18nKey="pages.overview.memoryStoreCard.episodes"
              components={{ v: <span className="text-text"><CountValue value={selected.episodic_count} /></span> }}
            />
          </span>
          <span className="text-muted">
            <Trans
              i18nKey="pages.overview.memoryStoreCard.lessons"
              components={{ v: <span className="text-text"><CountValue value={selected.lessons_count} /></span> }}
            />
          </span>
          <span className="text-muted">
            <Trans
              i18nKey="pages.overview.memoryStoreCard.backups"
              components={{ v: <span className="text-text"><CountValue value={selected.backup_count} /></span> }}
            />
          </span>
          {selected.newest_backup && (
            <span className="text-muted">
              <Trans
                i18nKey="pages.overview.memoryStoreCard.newest_backup"
                components={{ v: <span className="text-text">{fmtDateTimeNumeric(selected.newest_backup)}</span> }}
              />
            </span>
          )}
        </div>
      )}
      {selected && !selected.facets_supported && (
        <p className="text-[12px] leading-relaxed text-muted mt-1">
          {i18nT('pages.overview.memoryStoreCard.this_store_uses_the_shared_schema_so_carve_facet')}
        </p>
      )}
      {selected && !selected.exists && (
        <p className="text-[12px] leading-relaxed text-warn mt-1">
          {i18nT('pages.overview.memoryStoreCard.this_store_has_no_database_file_that_can_be_read')}
        </p>
      )}
      <Modal
        open={creating}
        onClose={() => openCreate(false)}
        title={i18nT('pages.overview.memoryStoreCard.new_memory_store')}
        maxWidth={480}
        guardAccidentalDismiss
        footer={
          <div className="flex gap-2 justify-end">
            <Btn onClick={() => openCreate(false)}>{i18nT('pages.overview.memoryStoreCard.cancel')}</Btn>
            <Btn
              primary
              disabled={!newName.trim() || createStore.isPending}
              onClick={() => createStore.mutate(newName.trim())}
            >
              {i18nT('pages.overview.memoryStoreCard.declare_store')}
            </Btn>
          </div>
        }
      >
        <p className="text-[13px] leading-relaxed text-muted mb-3">
          {i18nT('pages.overview.memoryStoreCard.a_new_store_starts_empty_and_keeps_its_memory_in')}
        </p>
        <Input
          aria-label={i18nT('pages.overview.memoryStoreCard.store_name')}
          placeholder={EXAMPLE_STORE_NAME}
          value={newName}
          onChange={e => setNewName(e.target.value)}
        />
        {createCode === 'invalid_memory_store_name' && (
          <p className="text-[13px] text-danger mt-2" role="alert">
            {i18nT('pages.overview.memoryStoreCard.that_name_cannot_be_used_for_a_store_directory_u')}
          </p>
        )}
        {createCode === 'memory_store_exists' && (
          <p className="text-[13px] text-danger mt-2" role="alert">
            {i18nT('pages.overview.memoryStoreCard.a_store_with_that_name_is_already_declared_pick')}
          </p>
        )}
        {createStore.error && createCode !== 'invalid_memory_store_name' && createCode !== 'memory_store_exists' && (
          <p className="text-[13px] text-danger mt-2" role="alert">{serverMessage(createStore.error)}</p>
        )}
      </Modal>
    </Card>
  )
}
