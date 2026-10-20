import fs, { promises as fsp } from 'node:fs'
import path from 'node:path'
import { bundleRoot, readBundle } from '@/commands/bundle/read-bundle'
import { HOST_ADDON_IMPORT } from '@/commands/verify/platform-addons'
import type { Logger } from '@/logging/types'

interface ListBundledAddonsOptions {
  bundlePath: string
  projectRoot: string
  logger: Logger
  includeAudioDecoder?: boolean
}

export const AUDIO_DECODER_ADDON = 'bare-ffmpeg'

const NODE_MODULES_RE = /\/node_modules\/(@[^/]+\/[^/]+|[^/]+)(?=\/)/g

export function buildNestedPathIndex(
  resolutions: Record<string, unknown>,
  projectRoot: string
): Map<string, Set<string>> {
  const index = new Map<string, Set<string>>()
  for (const key of Object.keys(resolutions)) {
    for (const match of key.matchAll(NODE_MODULES_RE)) {
      const pkgName = match[1]
      if (!pkgName) continue
      const idx = match.index
      if (idx === undefined) continue
      const marker = `/node_modules/${pkgName}/`
      const candidate = path.join(projectRoot, key.slice(1, idx + marker.length), 'package.json')
      let set = index.get(pkgName)
      if (!set) {
        set = new Set()
        index.set(pkgName, set)
      }
      set.add(candidate)
    }
  }
  return index
}

/** The `#host-addon` map of each split addon the bundle loads, by package name. */
export async function listBundledHostAddons(options: {
  bundlePath: string
  projectRoot: string
}): Promise<Map<string, unknown>> {
  const bundle = await readBundle(options.bundlePath)
  const pathsByPackage = buildNestedPathIndex(
    bundle.resolutions,
    bundleRoot(bundle.main, options.projectRoot)
  )

  const hostAddons = new Map<string, unknown>()
  for (const [pkgName, candidates] of pathsByPackage) {
    const candidate = [...candidates].find((file) => fs.existsSync(file))
    if (candidate === undefined) continue
    const manifest = JSON.parse(await fsp.readFile(candidate, 'utf8')) as {
      imports?: Record<string, unknown>
    }
    const hostAddon = manifest.imports?.[HOST_ADDON_IMPORT]
    if (hostAddon !== undefined) hostAddons.set(pkgName, hostAddon)
  }
  return hostAddons
}

/** The native addon packages the bundle's module graph loads, sorted by name. */
export async function listBundledAddons(options: ListBundledAddonsOptions): Promise<string[]> {
  const { bundlePath, projectRoot, logger, includeAudioDecoder = true } = options

  const bundle = await readBundle(bundlePath)
  const pathsByPackage = buildNestedPathIndex(
    bundle.resolutions,
    bundleRoot(bundle.main, projectRoot)
  )

  const addons: string[] = []
  for (const [pkgName, candidates] of pathsByPackage) {
    let pkgJson: { addon?: boolean } | null = null
    for (const candidate of candidates) {
      try {
        if (fs.existsSync(candidate)) {
          pkgJson = JSON.parse(await fsp.readFile(candidate, 'utf8')) as { addon?: boolean }
          break
        }
      } catch (err) {
        logger.warn(`   Could not read ${candidate}: ${(err as Error).message}`)
      }
    }
    if (pkgJson?.addon === true && (includeAudioDecoder || pkgName !== AUDIO_DECODER_ADDON)) {
      addons.push(pkgName)
    }
  }

  return addons.sort()
}

export async function listBundledAddonModules(
  options: ListBundledAddonsOptions
): Promise<string[]> {
  const addons = new Set(await listBundledAddons(options))
  const bundle = await readBundle(options.bundlePath)
  const root = bundleRoot(bundle.main, options.projectRoot)

  return Object.keys(bundle.resolutions)
    .filter((key) => {
      const packages = [...key.matchAll(NODE_MODULES_RE)]
      const owner = packages[packages.length - 1]?.[1]
      return owner !== undefined && addons.has(owner)
    })
    .map((key) => path.join(root, key.slice(1)))
    .sort()
}
