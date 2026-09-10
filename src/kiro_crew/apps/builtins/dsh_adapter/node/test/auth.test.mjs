/**
 * Auth tests: how the credential is obtained and where it must ride.
 *
 * Both rules here cost a round trip to learn and fail as a flat 403 with no hint
 * about which one you got wrong, so they are pinned: the token comes from
 * exchanging the app's own secret, and it rides the QUERY STRING. An
 * `Authorization` header is ignored, which means a client that only sets one
 * looks correctly written and is refused everywhere.
 *
 * @module dsh_adapter/test/auth
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { FakeGateway } from '../fake/gateway.mjs'
import { ContractClient, exchangeToken } from '../lib/contract.mjs'
import { APP_NAME, UNIT_KIND, readConfig, resolveToken } from '../server.mjs'

const CONTRIBUTIONS = { events: [`${APP_NAME}/*`], projections: [`${APP_NAME}/*`], units: [UNIT_KIND] }

test('the app secret exchanges for a token', async () => {
  const gateway = new FakeGateway({ contributions: CONTRIBUTIONS })
  const baseUrl = await gateway.listen()
  try {
    const token = await exchangeToken({ baseUrl, appName: APP_NAME, secret: gateway.secret })
    assert.equal(token, gateway.token)
  } finally {
    await gateway.close()
  }
})

test('a wrong secret is refused rather than yielding a token', async () => {
  const gateway = new FakeGateway({ contributions: CONTRIBUTIONS })
  const baseUrl = await gateway.listen()
  try {
    const outcome = await exchangeToken({ baseUrl, appName: APP_NAME, secret: 'wrong' })
      .then(() => 'exchanged', error => error.code)
    assert.equal(outcome, 'invalid_secret')
  } finally {
    await gateway.close()
  }
})

test('the token rides the query string, not a header', async () => {
  const gateway = new FakeGateway({ contributions: CONTRIBUTIONS })
  const baseUrl = await gateway.listen()
  const client = new ContractClient({ baseUrl, token: gateway.token })
  try {
    assert.ok(client.url('/api/x').includes(`token=${gateway.token}`))
    assert.equal(client.headers().authorization, undefined)

    // The real middleware ignores Authorization, so a header-only request is
    // refused; this is the failure a bearer-shaped client would hit everywhere.
    const bare = await fetch(`${baseUrl}/api/eventlog/${UNIT_KIND}/kiro/events?after=-1&limit=10`, {
      headers: { authorization: `Bearer ${gateway.token}` },
    })
    assert.equal(bare.status, 401)
    assert.equal((await bare.json()).error, 'Token required')

    // The same read with the token in the query string succeeds.
    const page = await client.readEvents(UNIT_KIND, 'kiro', -1)
    assert.deepEqual(page.events, [])
  } finally {
    await gateway.close()
  }
})

test('a projection key is percent-encoded whole, not split into path segments', () => {
  const client = new ContractClient({ baseUrl: 'http://127.0.0.1:1', token: 't' })
  const path = client.projectionPath(UNIT_KIND, 'kiro', `${APP_NAME}/sessionStats`)
  assert.ok(path.endsWith(`/projections/${APP_NAME}%2FsessionStats`), path)
  assert.equal(path.split('/projections/')[1].includes('/'), false)
})

test('a configured token skips the exchange, and a missing secret is named', async () => {
  const configured = readConfig({ DSH_ADAPTER_TOKEN: 'preset', DSH_ADAPTER_GATEWAY: 'http://127.0.0.1:1' })
  assert.deepEqual(await resolveToken(configured), { token: 'preset', reason: '' })

  const bare = readConfig({ DSH_ADAPTER_GATEWAY: 'http://127.0.0.1:1' })
  const outcome = await resolveToken(bare)
  assert.equal(outcome.token, '')
  assert.match(outcome.reason, /no app secret/)
})

test('an unreachable gateway is retried, because the app starts before it listens', async () => {
  const config = readConfig({
    DSH_ADAPTER_GATEWAY: 'http://127.0.0.1:1',
    KIROCREW_PROXY_SECRET: 'secret',
    KIROCREW_APP_NAME: APP_NAME,
  })
  const started = Date.now()
  const outcome = await resolveToken(config, { attempts: 3, delayMs: 30 })
  assert.equal(outcome.token, '')
  assert.match(outcome.reason, /after 3 attempts/)
  assert.ok(Date.now() - started >= 60, 'waited between attempts')
})

test('a refused secret is final, not retried for a minute', async () => {
  const gateway = new FakeGateway({ contributions: CONTRIBUTIONS })
  const baseUrl = await gateway.listen()
  try {
    const config = readConfig({
      DSH_ADAPTER_GATEWAY: baseUrl,
      KIROCREW_PROXY_SECRET: 'wrong',
      KIROCREW_APP_NAME: APP_NAME,
    })
    const started = Date.now()
    const outcome = await resolveToken(config, { attempts: 20, delayMs: 3000 })
    assert.equal(outcome.token, '')
    assert.match(outcome.reason, /refused/)
    assert.ok(Date.now() - started < 1000, 'returned at once instead of retrying')
  } finally {
    await gateway.close()
  }
})

test('the platform secret is what gets exchanged when no token is configured', async () => {
  const gateway = new FakeGateway({ contributions: CONTRIBUTIONS })
  const baseUrl = await gateway.listen()
  try {
    const config = readConfig({
      DSH_ADAPTER_GATEWAY: baseUrl,
      KIROCREW_PROXY_SECRET: gateway.secret,
      KIROCREW_APP_NAME: APP_NAME,
    })
    assert.deepEqual(await resolveToken(config), { token: gateway.token, reason: '' })
  } finally {
    await gateway.close()
  }
})
