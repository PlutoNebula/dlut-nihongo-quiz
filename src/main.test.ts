import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  mount: vi.fn(),
  use: vi.fn(),
  app: { config: {} as Record<string, unknown> },
}))
vi.mock('vue', () => ({
  createApp: () => ({ ...mocks.app, mount: mocks.mount, use: mocks.use }),
}))
vi.mock('./App.vue', () => ({ default: {} }))
vi.mock('./router', () => ({ default: {} }))

const scope = 'http://localhost:5173/'
const scriptURL = `${scope}sw.js`
let serviceWorker: {
  controller: { scriptURL: string } | null
  getRegistration: ReturnType<typeof vi.fn>
  register: ReturnType<typeof vi.fn>
  addEventListener: ReturnType<typeof vi.fn>
}
let reload: ReturnType<typeof vi.fn>
let events: Map<string, Array<() => void>>

beforeEach(() => {
  vi.resetModules()
  vi.clearAllMocks()
  vi.stubEnv('DEV', true)
  vi.stubEnv('PROD', false)
  vi.stubEnv('BASE_URL', '/')
  serviceWorker = {
    controller: null,
    getRegistration: vi.fn().mockResolvedValue(undefined),
    register: vi.fn().mockResolvedValue({ addEventListener: vi.fn() }),
    addEventListener: vi.fn(),
  }
  reload = vi.fn()
  events = new Map()
  const location = { href: `${scope}#/quiz`, origin: 'http://localhost:5173', reload }
  vi.stubGlobal('location', location)
  vi.stubGlobal('navigator', { serviceWorker })
  vi.stubGlobal('window', {
    location,
    addEventListener: vi.fn((event, handler) => {
      events.set(event, [...(events.get(event) ?? []), handler])
    }),
  })
})
afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// Include the real HTML bootstrap so a second SW registration cannot bypass the entry policy.
function runEntryScripts() {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
  for (const [, script] of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
    runInNewContext(script!, { window, navigator, location, URL, console })
  }
}

describe('development service worker lifecycle', () => {
  it('mounts a clean development page without registering an offline worker', async () => {
    runEntryScripts()
    await import('./main')
    await vi.waitFor(() => expect(mocks.mount).toHaveBeenCalledWith('#app'))
    events.get('load')?.forEach((handler) => handler())
    expect(serviceWorker.register).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
  })

  it('unregisters the old app worker and reloads before mounting the quiz', async () => {
    const unregister = vi.fn().mockResolvedValue(true)
    serviceWorker.controller = { scriptURL }
    serviceWorker.getRegistration.mockResolvedValue({ scope, active: { scriptURL }, unregister })
    await import('./main')
    await vi.waitFor(() => expect(reload).toHaveBeenCalledOnce())
    expect(unregister).toHaveBeenCalledOnce()
    expect(mocks.mount).not.toHaveBeenCalled()
    expect(serviceWorker.register).not.toHaveBeenCalled()
  })

  it('removes an inactive app registration without reloading an uncontrolled page', async () => {
    const unregister = vi.fn().mockResolvedValue(true)
    serviceWorker.getRegistration.mockResolvedValue({ scope, waiting: { scriptURL }, unregister })
    await import('./main')
    await vi.waitFor(() => expect(mocks.mount).toHaveBeenCalledOnce())
    expect(unregister).toHaveBeenCalledOnce()
    expect(reload).not.toHaveBeenCalled()
  })

  it('does not unregister a different application on the same origin', async () => {
    const unregister = vi.fn()
    serviceWorker.getRegistration.mockResolvedValue({
      scope,
      active: { scriptURL: `${scope}other-worker.js` },
      unregister,
    })
    await import('./main')
    await vi.waitFor(() => expect(mocks.mount).toHaveBeenCalledOnce())
    expect(unregister).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
  })

  it('reloads a still-controlled page if another tab already unregistered the worker', async () => {
    serviceWorker.controller = { scriptURL }
    await import('./main')
    await vi.waitFor(() => expect(reload).toHaveBeenCalledOnce())
    expect(mocks.mount).not.toHaveBeenCalled()
  })

  it('preserves production worker registration and mounting', async () => {
    vi.stubEnv('DEV', false)
    vi.stubEnv('PROD', true)
    runEntryScripts()
    await import('./main')
    expect(mocks.mount).toHaveBeenCalledWith('#app')
    events.get('load')?.forEach((handler) => handler())
    expect(serviceWorker.register).toHaveBeenCalledExactlyOnceWith(scriptURL)
    expect(serviceWorker.getRegistration).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
  })
})
