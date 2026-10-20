import { promises as fsp } from 'node:fs'
import path from 'node:path'
import link from 'bare-link'
import semver from 'semver'
import { listBundledHostAddons } from '@/commands/bundle/addons'
import { DEFAULT_HOSTS } from '@/commands/bundle/constants'
import { resolveAddonPlatformPackage } from '@/commands/verify/platform-addons'
import { findInAncestorNodeModules } from '@/expo/plugins/find-in-ancestor-node-modules'
import { AddonLinkFailedError, ReactNativeBareKitUnsupportedError } from '@/utils/errors-client'
import type { Logger } from '@/logging/types'

const REACT_NATIVE_BARE_KIT = 'react-native-bare-kit'

const PHONE_HOSTS = DEFAULT_HOSTS.filter(
  (host) => host.startsWith('android-') || host.startsWith('ios-')
)

interface PackageManifest {
  version?: unknown
  peerDependencies?: Record<string, unknown>
}

async function readManifest(dir: string): Promise<PackageManifest | null> {
  try {
    return JSON.parse(await fsp.readFile(path.join(dir, 'package.json'), 'utf8')) as PackageManifest
  } catch {
    return null
  }
}

/** Checks the installed react-native-bare-kit against the SDK's peer range for it. */
export async function assertReactNativeBareKitSupported(
  projectRoot: string,
  sdkPath: string
): Promise<void> {
  const bareKitDir = findInAncestorNodeModules(projectRoot, REACT_NATIVE_BARE_KIT)
  if (bareKitDir === null) return

  const required = (await readManifest(sdkPath))?.peerDependencies?.[REACT_NATIVE_BARE_KIT]
  const installed = (await readManifest(bareKitDir))?.version
  if (typeof required !== 'string' || typeof installed !== 'string') return
  if (semver.satisfies(installed, required, { includePrerelease: true })) return

  throw new ReactNativeBareKitUnsupportedError(installed, required)
}

/** The name of the `node_modules` package a module path belongs to. */
function owningPackage(modulePath: string): string | null {
  const marker = `${path.sep}node_modules${path.sep}`
  const index = modulePath.lastIndexOf(marker)
  if (index === -1) return null
  const parts = modulePath.slice(index + marker.length).split(path.sep)
  return (parts[0]?.startsWith('@') ? parts.slice(0, 2) : parts.slice(0, 1)).join('/')
}

/**
 * The addon modules to link for `hosts`. The phone bundle covers every phone
 * host, so it also loads the platform packages split addons name for the
 * other platform, which have no prebuilds for `hosts`. `hostAddons` holds each
 * split addon's `#host-addon` map, by package name.
 */
export function addonModulesForHosts(
  addonModules: string[],
  hosts: string[],
  hostAddons: Map<string, unknown>
): string[] {
  const otherHosts = PHONE_HOSTS.filter((host) => !hosts.includes(host))
  const own = new Set<string>()
  const other = new Set<string>()
  for (const [name, hostAddon] of hostAddons) {
    for (const host of hosts) {
      const platformPackage = resolveAddonPlatformPackage(name, hostAddon, host)
      if (platformPackage !== null) own.add(platformPackage)
    }
    for (const host of otherHosts) {
      const platformPackage = resolveAddonPlatformPackage(name, hostAddon, host)
      if (platformPackage !== null) other.add(platformPackage)
    }
  }

  return addonModules.filter((modulePath) => {
    const owner = owningPackage(modulePath)
    return owner === null || own.has(owner) || !other.has(owner)
  })
}

/** Where react-native-bare-kit builds each platform's linked addons from. */
const PLATFORMS = [
  { prefix: 'android-', out: ['android', 'src', 'main', 'addons'] },
  { prefix: 'ios-', out: ['ios', 'addons'] }
]

export interface LinkAddonsOptions {
  projectRoot: string
  /** The phone bundle the addon modules come from. */
  bundlePath: string
  addonModules: string[]
  hosts: string[]
  logger: Logger
}

/**
 * Links the native addons the bundle loads into react-native-bare-kit for
 * each phone platform in `hosts`, replacing what the platform had.
 */
export async function linkAddons(options: LinkAddonsOptions): Promise<string[]> {
  const { projectRoot, bundlePath, addonModules, hosts, logger } = options

  const bareKitDir = findInAncestorNodeModules(projectRoot, REACT_NATIVE_BARE_KIT)
  if (bareKitDir === null) {
    logger.warn('react-native-bare-kit is not installed; the native addons were not linked.')
    return []
  }

  const hostAddons =
    addonModules.length > 0
      ? await listBundledHostAddons({ bundlePath, projectRoot })
      : new Map<string, unknown>()

  const written: string[] = []
  for (const platform of PLATFORMS) {
    const platformHosts = hosts.filter((host) => host.startsWith(platform.prefix))
    if (platformHosts.length === 0) continue
    // bare-link signs Apple frameworks with codesign.
    if (platform.prefix === 'ios-' && process.platform !== 'darwin') {
      logger.warn(`Linking iOS addons needs macOS; skipped ${platformHosts.join(', ')}.`)
      continue
    }

    const out = path.join(bareKitDir, ...platform.out)
    await fsp.rm(out, { recursive: true, force: true })
    const modules = addonModulesForHosts(addonModules, platformHosts, hostAddons)
    const platformWritten: string[] = []
    try {
      for await (const resource of link(modules, { hosts: platformHosts, out })) {
        logger.debug(`Linked ${path.relative(projectRoot, resource)}`)
        platformWritten.push(resource)
      }
    } catch (error) {
      throw new AddonLinkFailedError(platformHosts, error)
    }
    warnOverwritten(platformWritten, out, logger)
    written.push(...platformWritten)
  }
  return written
}

/**
 * Addons that ship a library under the same name write it to the same path,
 * and the copy linked last is the one the app loads.
 */
export function warnOverwritten(written: string[], out: string, logger: Logger) {
  const seen = new Set<string>()
  const overwritten = new Set<string>()
  for (const file of written) {
    if (seen.has(file)) overwritten.add(file)
    seen.add(file)
  }
  if (overwritten.size === 0) return
  const files = [...overwritten].map((file) => path.relative(out, file)).sort()
  logger.warn(`Several native addons ship ${files.join(', ')}; only the copy linked last is kept.`)
}
