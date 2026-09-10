/**
 * Loads a foreign plugin from a read-only source checkout without copying,
 * patching, or building it.
 *
 * The checkout is a pnpm workspace with no `node_modules` and no build output,
 * so neither of Node's normal answers works: bare specifiers like
 * `@deepseek-ai/cordis` have nothing to resolve against, and the files are
 * TypeScript. Two Node built-in hooks close both gaps, which is why this file
 * has no dependencies at all:
 *
 * - `module.registerHooks` resolves a workspace package name to the file its
 *   OWN `exports` map names, rewritten from build output to source. The map is
 *   read from the package's `package.json`; nothing here guesses a layout.
 * - `module.stripTypeScriptTypes` compiles each `.ts` file in memory. `mode`
 *   is `'transform'`, not `'strip'`: the runtime under here uses `const enum`,
 *   `namespace`, and constructor parameter properties, none of which erase.
 *
 * A third-party import made from inside the checkout (the plugin's own `zod`,
 * say) is re-resolved against THIS package, so the adapter's pinned
 * dependencies are the ones that load. That is the whole reason those pins
 * exist.
 *
 * Consequence worth stating plainly: the plugin executes in this process with
 * this process's privileges. The gateway never runs contributor code — that
 * boundary is the adapter's own process, not a sandbox inside it.
 *
 * @module dsh_adapter/loader
 */

import { registerHooks, stripTypeScriptTypes } from 'node:module'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

/** Where a workspace package's `exports` targets live once built. */
const BUILD_DIRS = ['lib/types/', 'lib/']

/** Directories under the checkout root that hold workspace packages. */
const WORKSPACE_ROOTS = ['packages', 'vendor']

/**
 * Read one `package.json`, tolerating anything unreadable or malformed.
 *
 * @param {string} file - absolute path to a package.json.
 * @returns {object | null} the parsed manifest, or null when it cannot be read.
 */
function readManifest(file) {
  try {
    return JSON.parse(readFileSync(file, 'utf8'))
  } catch {
    return null
  }
}

/**
 * Index every workspace package by name.
 *
 * Walks `packages/` two levels deep (the checkout groups packages by domain)
 * and `vendor/` one level, so the map is derived from what is on disk rather
 * than from a hardcoded list that would rot the moment the checkout moves.
 *
 * @param {string} root - absolute path to the checkout root.
 * @returns {Map<string, {dir: string, manifest: object}>} package name to its directory and manifest.
 */
export function indexWorkspace(root) {
  const found = new Map()
  const consider = dir => {
    const manifest = readManifest(path.join(dir, 'package.json'))
    if (manifest && typeof manifest.name === 'string') found.set(manifest.name, { dir, manifest })
  }
  const children = dir => {
    try {
      return readdirSync(dir, { withFileTypes: true }).filter(e => e.isDirectory()).map(e => path.join(dir, e.name))
    } catch {
      return []
    }
  }
  for (const group of WORKSPACE_ROOTS) {
    for (const first of children(path.join(root, group))) {
      consider(first)
      for (const second of children(first)) consider(second)
    }
  }
  return found
}

/**
 * Rewrite one `exports` target from build output to source.
 *
 * `./lib/types/message.js` and `./lib/message.js` both become
 * `./src/message.ts`. A target already under `src/` is returned unchanged.
 *
 * @param {string} target - an exports target, relative to the package dir.
 * @returns {string} the source-relative path to load instead.
 */
export function sourceTarget(target) {
  const clean = target.replace(/^\.\//, '')
  for (const prefix of BUILD_DIRS) {
    if (clean.startsWith(prefix)) return `src/${clean.slice(prefix.length).replace(/\.js$/, '.ts')}`
  }
  return clean
}

/**
 * Resolve a package subpath through the package's own `exports` map.
 *
 * @param {object} manifest - the package's parsed manifest.
 * @param {string} subpath - `'.'` or `'./message'`.
 * @returns {string | null} the source-relative file, or null when the package does not export it.
 */
export function exportedSource(manifest, subpath) {
  const map = manifest.exports
  if (!map || typeof map !== 'object') return subpath === '.' ? 'src/index.ts' : null
  const entry = map[subpath]
  const target = typeof entry === 'string' ? entry : entry && typeof entry === 'object' ? entry.default : null
  if (typeof target === 'string') return sourceTarget(target)
  // A package that only declares `./src/*` still resolves its own source.
  if (typeof map['./src/*'] === 'string' && subpath.startsWith('./')) {
    const bare = subpath.slice(2)
    return `src/${bare.endsWith('.ts') ? bare : `${bare}.ts`}`
  }
  return null
}

/**
 * Split a bare specifier into its package name and subpath.
 *
 * @param {string} specifier - e.g. `@deepseek-ai/dsh-llm/message`.
 * @returns {{name: string, subpath: string}} the package name and `'.'`-rooted subpath.
 */
export function splitSpecifier(specifier) {
  const parts = specifier.split('/')
  const cut = specifier.startsWith('@') ? 2 : 1
  return { name: parts.slice(0, cut).join('/'), subpath: parts.length > cut ? `./${parts.slice(cut).join('/')}` : '.' }
}

/**
 * Whether a specifier is a bare package name (not relative, absolute, or built-in).
 *
 * @param {string} specifier - the import specifier.
 * @returns {boolean} true when Node would look it up in `node_modules`.
 */
function isBare(specifier) {
  return !specifier.startsWith('.') && !specifier.startsWith('/')
    && !specifier.startsWith('node:') && !specifier.startsWith('file:')
}

/**
 * Install the resolution and compile hooks for one checkout.
 *
 * Idempotent per process: a second call for the same root is a no-op, because
 * `registerHooks` stacks and a duplicate layer would compile every file twice.
 *
 * @param {object} options - loader options.
 * @param {string} options.checkoutRoot - absolute path to the read-only plugin checkout.
 * @param {string} [options.dependencyAnchor] - file URL whose `node_modules` serves the
 *   checkout's third-party imports; defaults to this module.
 * @returns {{root: string, packages: Map<string, {dir: string, manifest: object}>}} the installed index.
 */
export function installLoader({ checkoutRoot, dependencyAnchor } = {}) {
  const root = path.resolve(checkoutRoot)
  if (!statSync(root, { throwIfNoEntry: false })?.isDirectory()) {
    throw new Error(`plugin checkout not found: ${root}`)
  }
  const installed = installLoader.installed ??= new Map()
  if (installed.has(root)) return installed.get(root)

  const packages = indexWorkspace(root)
  const anchor = dependencyAnchor ?? import.meta.url
  const inCheckout = url => typeof url === 'string' && url.startsWith(pathToFileURL(root).href)
  const state = { root, packages }
  installed.set(root, state)

  registerHooks({
    resolve(specifier, context, nextResolve) {
      if (isBare(specifier)) {
        const { name, subpath } = splitSpecifier(specifier)
        const hit = packages.get(name)
        if (hit) {
          const relative = exportedSource(hit.manifest, subpath)
          if (relative === null) throw new Error(`${name} does not export ${subpath}`)
          return { url: pathToFileURL(path.join(hit.dir, relative)).href, format: 'module', shortCircuit: true }
        }
        // A third-party import from inside the checkout: the checkout has no
        // node_modules, so serve it from the adapter's pinned install.
        if (inCheckout(context.parentURL)) return nextResolve(specifier, { ...context, parentURL: anchor })
      }
      return nextResolve(specifier, context)
    },
    load(url, context, nextLoad) {
      if (url.startsWith('file:') && url.endsWith('.ts')) {
        const source = readFileSync(fileURLToPath(url), 'utf8')
        return {
          format: 'module',
          source: stripTypeScriptTypes(source, { mode: 'transform', sourceUrl: url }),
          shortCircuit: true,
        }
      }
      return nextLoad(url, context)
    },
  })

  return state
}

/**
 * Absolute path to a file inside the checkout.
 *
 * @param {string} root - the checkout root.
 * @param {string} relative - a checkout-relative path.
 * @returns {string} the absolute path.
 */
export function checkoutPath(root, relative) {
  return path.join(path.resolve(root), relative)
}
