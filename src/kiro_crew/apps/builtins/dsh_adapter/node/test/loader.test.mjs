/**
 * Loader tests: how a workspace package name becomes a source file.
 *
 * These are the rules that decide WHICH file runs, so a wrong answer here loads
 * the wrong code (or nothing) with no other signal. They are pure functions, so
 * they need no checkout.
 *
 * @module dsh_adapter/test/loader
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { exportedSource, indexWorkspace, sourceTarget, splitSpecifier } from '../lib/loader.mjs'

const CHECKOUT = process.env.DSH_ADAPTER_CHECKOUT ?? '/local/home/mingweic/oss/deepseek-harness'
const skip = existsSync(CHECKOUT) ? false : 'no plugin checkout'

test('a build target is rewritten to its source file', () => {
  assert.equal(sourceTarget('./lib/index.js'), 'src/index.ts')
  assert.equal(sourceTarget('./lib/types/message.js'), 'src/message.ts')
  assert.equal(sourceTarget('./src/projection.ts'), 'src/projection.ts')
})

test('a scoped specifier splits into package and subpath', () => {
  assert.deepEqual(splitSpecifier('@scope/pkg'), { name: '@scope/pkg', subpath: '.' })
  assert.deepEqual(splitSpecifier('@scope/pkg/message'), { name: '@scope/pkg', subpath: './message' })
  assert.deepEqual(splitSpecifier('zod'), { name: 'zod', subpath: '.' })
  assert.deepEqual(splitSpecifier('zod/v4'), { name: 'zod', subpath: './v4' })
})

test('a subpath resolves through the package export map, not a guessed layout', () => {
  const manifest = {
    exports: {
      '.': { types: './lib/types/index.d.ts', default: './lib/index.js' },
      './message': { types: './lib/types/message.d.ts', default: './lib/types/message.js' },
      './src/*': './src/*',
    },
  }
  assert.equal(exportedSource(manifest, '.'), 'src/index.ts')
  assert.equal(exportedSource(manifest, './message'), 'src/message.ts')
})

test('a package that only declares ./src/* still resolves its own source', () => {
  const manifest = { exports: { './src/*': './src/*' } }
  assert.equal(exportedSource(manifest, './projection'), 'src/projection.ts')
  assert.equal(exportedSource(manifest, './projection.ts'), 'src/projection.ts')
})

test('an undeclared subpath resolves to nothing rather than to a plausible path', () => {
  const manifest = { exports: { '.': './lib/index.js' } }
  assert.equal(exportedSource(manifest, './secret'), null)
})

test('the workspace index finds the packages the plugin needs', { skip }, () => {
  const packages = indexWorkspace(CHECKOUT)
  for (const name of ['@deepseek-ai/cordis', '@deepseek-ai/cosmokit', '@deepseek-ai/dsh-llm',
    '@deepseek-ai/dsh-session-projection', '@deepseek-ai/dsh-session-stats']) {
    assert.ok(packages.has(name), `${name} indexed`)
  }
  // The map is built from what is on disk, so a name that is not a workspace
  // package must stay absent and fall through to the adapter's own install.
  assert.equal(packages.has('zod'), false)
})
