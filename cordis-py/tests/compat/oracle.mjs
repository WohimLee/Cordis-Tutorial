#!/usr/bin/env node

import { readFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

function usage(message) {
  if (message) console.error(message)
  console.error('usage: node tests/compat/oracle.mjs <scenario.json>')
  console.error('set CORDIS_REFERENCE_ENTRY to the built vendored cordis lib/index.js')
  process.exit(2)
}

const scenarioPath = process.argv[2]
const referenceEntry = process.env.CORDIS_REFERENCE_ENTRY
if (!scenarioPath) usage('missing scenario path')
if (!referenceEntry) usage('missing CORDIS_REFERENCE_ENTRY')

const scenario = JSON.parse(await readFile(scenarioPath, 'utf8'))
const cordis = await import(pathToFileURL(referenceEntry).href)

async function runCoreSmoke() {
  const trace = []
  const ctx = new cordis.Context()

  const probe = (pluginCtx) => {
    trace.push('activate')
    pluginCtx.on('probe', value => trace.push(`event:${value}`))
    return () => trace.push('cleanup')
  }

  const fiber = ctx.plugin(probe)
  await fiber
  ctx.emit('probe', 'value')
  await fiber.dispose()
  ctx.emit('probe', 'after-dispose')
  await ctx.fiber.dispose()

  return {
    trace,
    fiber_state: cordis.FiberState[fiber.state],
  }
}

async function runPluginShapes() {
  const activated = []
  const cleaned = []
  const ctx = new cordis.Context()

  const functionProbe = (_ctx, config) => {
    activated.push(`function:${config}`)
    return () => cleaned.push('function')
  }
  class ClassProbe {
    constructor(_ctx, config) {
      activated.push(`class:${config}`)
    }
  }
  const objectProbe = {
    name: 'objectProbe',
    apply(_ctx, config) {
      activated.push(`object:${config}`)
      return () => cleaned.push('object')
    },
  }

  await Promise.all([
    ctx.plugin(functionProbe, 'function'),
    ctx.plugin(ClassProbe, 'class'),
    ctx.plugin(objectProbe, 'object'),
  ])
  const registrySizeBefore = ctx.registry.size
  await ctx.fiber.dispose()

  return {
    activated: activated.sort(),
    cleaned: cleaned.sort(),
    registry_size_before: registrySizeBefore,
    registry_size_after: ctx.registry.size,
  }
}

async function runContextRegistry() {
  const trace = []
  const ctx = new cordis.Context()
  const child = ctx.extend({ marker: 'child' })
  const probe = (pluginCtx) => {
    trace.push(pluginCtx.marker)
  }
  const fiber = child.plugin(probe)
  await fiber
  const runtime = ctx.registry.get(probe)
  let visited = 0
  ctx.registry.forEach(() => visited++)

  const result = {
    child_marker: child.marker,
    parent_marker: ctx.marker ?? null,
    missing_strict: ctx.get('missing') ?? null,
    missing_loose: ctx.get('missing', false) ?? null,
    trace,
    registry: {
      size: ctx.registry.size,
      has: ctx.registry.has(probe),
      keys: [...ctx.registry.keys()].length,
      values: [...ctx.registry.values()].length,
      entries: [...ctx.registry.entries()].length,
      visited,
      runtime: runtime ? [runtime.name, runtime.callback === probe, runtime.Config === undefined] : null,
    },
    service_contexts: [
      ctx.events.ctx === ctx,
      ctx.registry.ctx === ctx,
      ctx.reflect.ctx === ctx,
      ctx.logger.ctx === ctx,
    ],
  }
  if (!runtime) throw new Error('missing probe runtime')
  await fiber.dispose()
  result.registry_size_after = ctx.registry.size
  await ctx.fiber.dispose()
  return result
}

async function runInjectDelete() {
  const trace = []
  const ctx = new cordis.Context()
  const consumer = (pluginCtx) => {
    trace.push(`activate:${pluginCtx.value}`)
    return () => trace.push('cleanup')
  }
  const consumerFiber = ctx.inject(['value'], consumer)
  await consumerFiber
  const initialState = cordis.FiberState[consumerFiber.state]
  const provider = ctx.plugin((providerCtx) => {
    providerCtx.provide('value', 'ready')
  })
  await provider
  await consumerFiber
  const activeState = cordis.FiberState[consumerFiber.state]
  const consumerRuntime = ctx.registry.get(consumer)
  const mountedFiber = [...consumerRuntime.fibers][0]
  const removed = ctx.registry.delete(consumer)
  await mountedFiber.dispose()
  const result = {
    initial_state: initialState,
    active_state: activeState,
    trace,
    delete_returned_runtime: !!removed,
    has_after_delete: ctx.registry.has(consumer),
    registry_size_after: ctx.registry.size,
  }
  await ctx.fiber.dispose()
  return result
}

async function runStrictGet() {
  const result = {}
  const ctx = new cordis.Context()
  const provider = ctx.plugin((pluginCtx) => {
    pluginCtx.provide('value', 'loading')
    result.during_loading_strict = pluginCtx.get('value') ?? null
    result.during_loading_loose = pluginCtx.get('value', false) ?? null
  })
  await provider
  result.after_loading_strict = ctx.get('value') ?? null
  await ctx.fiber.dispose()
  return result
}

async function settleRegistry(ctx) {
  const fibers = [...ctx.registry.values()].flatMap(runtime => [...runtime.fibers])
  await Promise.all(fibers.map(fiber => fiber.await()))
}

async function runInjectMetadata() {
  const trace = []
  const ctx = new cordis.Context()
  const initializers = []

  class Base extends cordis.Service {
    constructor(pluginCtx) {
      super(pluginCtx, 'worker')
    }
  }
  cordis.Inject('a')(Base, { kind: 'class' })

  class Worker extends Base {
    constructor(pluginCtx) {
      super(pluginCtx)
      trace.push(`construct:${pluginCtx.a}:${pluginCtx.c}`)
      for (const initialize of initializers) initialize.call(this)
    }

    run() {
      const value = this.ctx.b
      trace.push(`method:${value}`)
      return () => trace.push(`method-cleanup:${value}`)
    }
  }
  cordis.Inject('c')(Worker, { kind: 'class' })
  cordis.Inject('b')(Worker.prototype.run, {
    kind: 'method',
    addInitializer(initialize) {
      initializers.push(initialize)
    },
  })

  const worker = ctx.plugin(Worker)
  await worker
  const beforeServices = cordis.FiberState[worker.state]

  const providerA = ctx.plugin((pluginCtx) => {
    pluginCtx.provide('a', 'A')
  })
  await providerA
  await worker
  const afterInheritedOnly = cordis.FiberState[worker.state]

  const providerC = ctx.plugin((pluginCtx) => {
    pluginCtx.provide('c', 'C')
  })
  await providerC
  await worker
  const afterClassDependencies = cordis.FiberState[worker.state]

  const providerB1 = ctx.plugin((pluginCtx) => {
    pluginCtx.provide('b', 'B1')
  })
  await providerB1
  await settleRegistry(ctx)
  await providerB1.dispose()
  await settleRegistry(ctx)

  const providerB2 = ctx.plugin((pluginCtx) => {
    pluginCtx.provide('b', 'B2')
  })
  await providerB2
  await settleRegistry(ctx)
  await ctx.fiber.dispose()

  return {
    before_services: beforeServices,
    after_inherited_only: afterInheritedOnly,
    after_class_dependencies: afterClassDependencies,
    trace,
  }
}

async function runContextFilter() {
  const ctx = new cordis.Context()
  ctx.baseUrl = 'file:///root/'
  const allowed = ctx.extend()
  const blocked = ctx.extend()
  const seen = []
  let dispatchContextSeen = false
  const dispatchCtx = ctx.extend({ [cordis.Context.filter]: owner => owner === allowed })
  ctx.on('internal/dispatch', (_mode, _name, _args, current) => {
    dispatchContextSeen = current === dispatchCtx
  })
  allowed.on('probe', () => seen.push('allowed'))
  blocked.on('probe', () => seen.push('blocked'))
  blocked.on('probe', () => seen.push('global'), { global: true })
  ctx.emit(dispatchCtx, 'probe')
  const result = {
    root_is_context: cordis.Context.is(ctx),
    child_is_context: cordis.Context.is(allowed),
    plain_is_context: cordis.Context.is({}),
    root_base_url: ctx.baseUrl,
    child_base_url: allowed.baseUrl,
    seen,
    dispatch_context_seen: dispatchContextSeen,
  }
  await ctx.fiber.dispose()
  return result
}

async function runInjectConfig() {
  const ctx = new cordis.Context()
  const seen = []
  class Configurable extends cordis.Service {
    static provide = 'configurable'
    constructor(pluginCtx) {
      super(pluginCtx, 'configurable')
    }
    read() {
      return this[cordis.Service.resolveConfig]({ base: 1, shared: 'base' }, { head: 3 })
    }
  }
  await ctx.plugin(Configurable)
  const consumer = ctx.inject(
    { configurable: { middle: 2, shared: 'inject' } },
    pluginCtx => {
      seen.push(pluginCtx.configurable.read())
    },
  )
  await consumer
  const result = { seen, state: cordis.FiberState[consumer.state] }
  await ctx.fiber.dispose()
  return result
}

async function runEventContracts() {
  const ctx = new cordis.Context()
  const order = []
  const modes = []
  const replacementOptions = []
  let replacementActive = true
  ctx.on('internal/dispatch', mode => modes.push(mode))
  ctx.on('internal/listener', (name, _listener, options) => {
    if (name !== 'replaced') return
    replacementOptions.push(options)
    return () => {
      const previous = replacementActive
      replacementActive = false
      return previous
    }
  })
  const first = ctx.on('order', () => order.push('normal'))
  ctx.on('order', () => order.push('prepend'), { prepend: true, global: false })
  const once = ctx.once('once', () => order.push('once'))
  const replaced = ctx.on('replaced', () => order.push('unexpected'), {
    prepend: true,
    global: false,
  })
  ctx.emit('order')
  ctx.emit('once')
  ctx.emit('once')
  const firstDispose = [first() ?? null, first() ?? null]
  const onceAfterEmit = once() ?? null
  const replacementDispose = [replaced(), replaced()]

  let finishEmit
  const emitDone = new Promise(resolve => {
    finishEmit = resolve
  })
  ctx.on('async-emit', async () => {
    await Promise.resolve()
    order.push('emit-async')
    finishEmit()
  })
  ctx.emit('async-emit')
  await emitDone

  ctx.on('bail', async () => 'async-bail')
  const bailResult = await ctx.bail('bail')
  ctx.on('serial', () => false)
  ctx.on('serial', () => 'serial')
  const serialResult = await ctx.serial('serial')
  ctx.on('parallel', async () => {
    throw new Error('parallel')
  })
  let parallelErrors = 0
  try {
    await ctx.parallel('parallel')
  } catch (error) {
    parallelErrors = error.errors.length
  }
  ctx.on('waterfall', async (_value, next) => `outer(${await next()})`)
  const waterfallResult = await ctx.waterfall('waterfall', 'value', () => 'inner')
  const result = {
    order,
    modes,
    first_dispose: firstDispose,
    once_after_emit: onceAfterEmit,
    replacement_options: replacementOptions,
    replacement_dispose: replacementDispose,
    bail_result: bailResult,
    serial_result: serialResult,
    parallel_errors: parallelErrors,
    waterfall_result: waterfallResult,
  }
  await ctx.fiber.dispose()
  return result
}

async function runEffectContracts() {
  const ctx = new cordis.Context()
  const trace = []
  const effect = ctx.effect(
    () => [() => trace.push('first'), () => trace.push('second')],
    'pair',
  )
  const awaited = await effect
  const firstDisposal = effect()
  const secondDisposal = effect()
  await firstDisposal
  let invalidError = null
  try {
    ctx.effect(() => 1)
  } catch (error) {
    invalidError = error.message
  }
  const asyncTrace = []
  let notifyStarted
  let allowSetup
  const setupStarted = new Promise(resolve => {
    notifyStarted = resolve
  })
  const setupGate = new Promise(resolve => {
    allowSetup = resolve
  })
  const asyncEffect = ctx.effect(async () => {
    asyncTrace.push('setup-start')
    notifyStarted()
    await setupGate
    asyncTrace.push('setup-end')
    return () => asyncTrace.push('cleanup')
  })
  await setupStarted
  const asyncDisposal = asyncEffect()
  const pendingBeforeRelease = asyncTrace.length === 1
  allowSetup()
  await asyncDisposal
  const parent = ctx.effect(() => ctx.effect(() => {}, 'child'), 'parent')
  await parent
  const effectMeta = ctx.fiber.getEffects()[0]
  const metadata = [effectMeta.label, effectMeta.children.map(child => child.label)]
  await parent()
  const result = {
    awaited_callable: typeof awaited === 'function',
    shared_disposal: firstDisposal === secondDisposal,
    trace,
    invalid_error: invalidError,
    live_effects: ctx.fiber.getEffects().length,
    async_trace: asyncTrace,
    pending_before_release: pendingBeforeRelease,
    metadata,
  }
  await ctx.fiber.dispose()
  return result
}

async function runFiberContracts() {
  const ctx = new cordis.Context()
  const trace = []
  const states = []
  let pluginCtx
  ctx.on('internal/status', (fiber, old) => {
    states.push([cordis.FiberState[old], cordis.FiberState[fiber.state]])
  })
  const plugin = (current, config) => {
    pluginCtx = current
    trace.push(`activate:${config}`)
    return () => trace.push(`cleanup:${config}`)
  }
  const mounted = ctx.plugin(plugin, 'one')
  const fiber = await mounted
  const initialUid = fiber.uid
  const initial = {
    name: fiber.name,
    ctx_same: pluginCtx === fiber.ctx,
    raw_config: fiber._config,
    config: fiber.config,
    store_size: Object.keys(fiber.store || {}).length,
    inertia: fiber.inertia !== undefined,
  }
  const restartResult = await fiber.restart()
  const updateResult = await fiber.update('two')
  await fiber.dispose()
  let inactiveError = null
  try {
    fiber.assertActive()
  } catch (error) {
    inactiveError = error.code
  }
  const result = {
    initial_uid_positive: typeof initialUid === 'number' && initialUid > 0,
    initial,
    restart_result: restartResult ?? null,
    update_result: updateResult ?? null,
    trace,
    states: [...states],
    disposed_uid: fiber.uid,
    disposed_state: cordis.FiberState[fiber.state],
    disposed_store: fiber.store ?? null,
    inactive_error: inactiveError,
  }
  await ctx.fiber.dispose()
  return result
}

async function runFiberInvalidUpdate() {
  const ctx = new cordis.Context()
  const plugin = () => {}
  plugin.inject = ['ready']
  plugin.Config = {
    '~standard': {
      validate(value) {
        return value > 0 ? { value } : { issues: [{ message: 'positive' }] }
      },
    },
  }
  const provider = current => current.provide('ready', true)
  await ctx.plugin(provider)
  const active = await ctx.plugin(plugin, 1)
  let activeFailedImmediately = false
  let activeError = null
  try {
    await active.update(0)
  } catch (error) {
    activeFailedImmediately = true
    activeError = error.message
  }
  const isolated = ctx.isolate('ready')
  const pending = await isolated.plugin(plugin, 1)
  let pendingFailedImmediately = false
  try {
    await pending.update(0)
  } catch {
    pendingFailedImmediately = true
  }
  await isolated.plugin(provider)
  let pendingFailedOnActivation = false
  try {
    await pending.await()
  } catch {
    pendingFailedOnActivation = true
  }
  const result = {
    active_failed_immediately: activeFailedImmediately,
    active_error: activeError,
    active_raw: active._config,
    active_config: active.config,
    active_state: cordis.FiberState[active.state],
    pending_failed_immediately: pendingFailedImmediately,
    pending_raw: pending._config,
    pending_failed_on_activation: pendingFailedOnActivation,
    pending_state: cordis.FiberState[pending.state],
  }
  await ctx.fiber.dispose()
  return result
}

async function runFiberFailures() {
  const ctx = new cordis.Context()
  const trace = []
  const unstable = (pluginCtx, config) => {
    trace.push(`activate:${config}`)
    pluginCtx.effect(() => () => trace.push(`cleanup:${config}`))
    if (config === 'fail') throw new Error('startup failed')
  }
  const fiber = await (async () => {
    const mounted = ctx.plugin(unstable, 'fail')
    try {
      await mounted
    } catch {}
    return [...ctx.registry.get(unstable).fibers][0]
  })()
  let startupFailed = false
  try {
    await fiber.await()
  } catch {
    startupFailed = true
  }
  const failedState = cordis.FiberState[fiber.state]
  await fiber.update('ok')
  await fiber.await()

  let cleanupCalled = false
  const brokenCleanup = () => () => {
    cleanupCalled = true
    throw new Error('cleanup failed')
  }
  const broken = await ctx.plugin(brokenCleanup)
  try {
    await broken.dispose()
  } catch {}
  const result = {
    startup_failed: startupFailed,
    failed_state: failedState,
    recovered_state: cordis.FiberState[fiber.state],
    trace: [...trace],
    cleanup_called: cleanupCalled,
    cleanup_final_state: cordis.FiberState[broken.state],
    cleanup_final_uid: broken.uid,
    cleanup_removed: !ctx.registry.has(brokenCleanup),
  }
  await ctx.fiber.dispose()
  return result
}

async function runFiberDependencyRaces() {
  class Value extends cordis.Service {
    static provide = 'value'
    constructor(pluginCtx, config) {
      super(pluginCtx, 'value')
      this.value = config
    }
  }

  const lossCtx = new cordis.Context()
  const provider = await lossCtx.plugin(Value, 'first')
  let notifyLoading
  let allowLoading
  const loadingStarted = new Promise(resolve => { notifyLoading = resolve })
  const loadingGate = new Promise(resolve => { allowLoading = resolve })
  let lossCleanups = 0
  const loadingConsumer = async () => {
    notifyLoading()
    await loadingGate
    return () => lossCleanups++
  }
  loadingConsumer.inject = ['value']
  const lossMounted = lossCtx.plugin(loadingConsumer)
  await loadingStarted
  const providerDisposal = provider.dispose()
  await Promise.resolve()
  allowLoading()
  await providerDisposal
  const lossFiber = await lossMounted
  const lossResult = {
    state: cordis.FiberState[lossFiber.state],
    cleanups: lossCleanups,
  }
  await lossCtx.fiber.dispose()

  const restoreCtx = new cordis.Context()
  const providerA = await restoreCtx.plugin(Value, 'A')
  let notifyCleanup
  let allowCleanup
  const cleanupStarted = new Promise(resolve => { notifyCleanup = resolve })
  const cleanupGate = new Promise(resolve => { allowCleanup = resolve })
  const activations = []
  const restoreConsumer = pluginCtx => {
    activations.push(pluginCtx.value.value)
    return async () => {
      notifyCleanup()
      await cleanupGate
    }
  }
  restoreConsumer.inject = ['value']
  const restoreFiber = await restoreCtx.plugin(restoreConsumer)
  const disposalA = providerA.dispose()
  await cleanupStarted
  const providerB = restoreCtx.plugin(Value, 'B')
  allowCleanup()
  await providerB
  await disposalA
  await restoreFiber.await()
  const restoreResult = {
    state: cordis.FiberState[restoreFiber.state],
    activations: [...activations],
  }
  await restoreCtx.fiber.dispose()
  return { loss: lossResult, restore: restoreResult }
}

async function runReflectService() {
  const ctx = new cordis.Context()
  const trace = []
  class Fancy extends cordis.Service {
    static provide = 'fancy'
    static Config = {
      '~standard': { validate: value => ({ value }) },
      merge: (...configs) => ({ count: configs.length }),
    }
    constructor(pluginCtx) {
      super(pluginCtx, 'fancy')
      this.Config = Fancy.Config
    }
    [cordis.Service.invoke]() {
      return this.ctx.marker ?? null
    }
    [cordis.Service.init]() {
      trace.push('init')
      return () => trace.push('cleanup')
    }
    read() {
      return this[cordis.Service.resolveConfig]({ base: true }, { head: true })
    }
    derive() {
      return this[cordis.Service.extend]({ extra: true })
    }
  }
  const provider = await ctx.plugin(Fancy)
  const consumer = ctx.extend({ marker: 'consumer' }).intercept('fancy', { middle: true })
  const service = consumer.fancy
  const derived = service.derive()

  const sharedLabel = Symbol('shared')
  const isolatedA = ctx.isolate('shared', sharedLabel)
  const isolatedB = ctx.isolate('shared', sharedLabel)
  isolatedA.provide('shared', 'scoped')
  const scopeResult = [isolatedB.get('shared'), ctx.get('shared') ?? null]

  const rootEvents = []
  const isolatedEvents = []
  const isolated = ctx.isolate('notice')
  ctx.on('internal/service', (_name, value) => rootEvents.push(value))
  isolated.on('internal/service', (_name, value) => isolatedEvents.push(value))
  const notice = ctx.provide('notice', 'root')
  ctx.reflect.notify(['notice'])
  const result = {
    call: service(),
    config: service.read(),
    derived_extra: derived.extra,
    scope: scopeResult,
    service_events: [rootEvents.at(-1), isolatedEvents.length],
    set_result: ctx.set('notice', 'updated'),
  }
  await notice()
  await provider.dispose()
  result.trace = trace
  await ctx.fiber.dispose()
  return result
}

async function runAccessorMixin() {
  const ctx = new cordis.Context()
  const state = { value: 1 }
  const accessor = ctx.accessor('computed', {
    get() {
      return state.value
    },
    set(value) {
      if (typeof value !== 'number' || value < 0) return false
      state.value = value
      return true
    },
  })
  const sourceObject = {
    count: 2,
    inc() {
      return ++this.count
    },
  }
  const source = ctx.provide('source', sourceObject)
  const mixin = ctx.mixin('source', ['count', 'inc'])
  const before = ctx.computed
  const accepted = Reflect.set(ctx, 'computed', 3)
  const values = [before, accepted, ctx.computed]
  const rejected = Reflect.set(ctx, 'computed', -1)
  const mixed = [ctx.count, ctx.inc(), ctx.count]
  await mixin()
  await accessor()
  const result = {
    values,
    rejected,
    mixed,
    after_dispose: [ctx.get('computed') ?? null, ctx.get('count') ?? null],
  }
  await source()
  await ctx.fiber.dispose()
  return result
}

async function runLoggerContracts() {
  const ctx = new cordis.Context()
  const capture = {
    colors: false,
    maxLength: 5,
    levels: { scope: cordis.LoggerLevel.DEBUG },
    formatters: { x: value => `<${value}>` },
    messages: [],
    export(message) {
      this.messages.push(message)
    },
  }
  const effect = ctx.logger.exporter(capture)
  const child = ctx.intercept('logger', {
    name: 'scope',
    level: cordis.LoggerLevel.WARN,
  })
  const logger = child.logger()
  logger.debug('debug')
  logger.info('hello %s', 'world')
  logger.warn('warn')
  logger.error('error')
  const formatted = cordis.Logger.format(capture, {
    sn: 0,
    ts: 0,
    name: 'scope',
    type: 'info',
    level: 1,
    args: ['abcdef\n%s %x', 'ok', 'z'],
  })
  const messages = capture.messages.map(message => ({
    sn: message.sn,
    name: message.name,
    type: message.type,
    level: message.level,
    args: message.args,
    fiber: message.fiber?.deref() === child.fiber,
  }))
  ctx.logger.bufferSize = 2
  const rootLogger = ctx.logger('buffer')
  rootLogger.info('one')
  rootLogger.info('two')
  rootLogger.info('three')
  const result = {
    levels: [
      cordis.LoggerLevel.ERROR,
      cordis.LoggerLevel.INFO,
      cordis.LoggerLevel.WARN,
      cordis.LoggerLevel.DEBUG,
    ],
    messages,
    formatted,
    buffer: ctx.logger.buffer.map(message => message.args),
  }
  await effect()
  await ctx.fiber.dispose()
  return result
}

async function runLoggerOptions() {
  const ctx = new cordis.Context()
  const capture = {
    colors: false,
    levels: { default: cordis.LoggerLevel.DEBUG },
    messages: [],
    export(message) {
      this.messages.push(message)
    },
  }
  const effect = ctx.logger.exporter(capture)
  const logger = new cordis.Logger({
    name: 'original',
    level: cordis.LoggerLevel.DEBUG,
    meta: { name: 'override', type: 'custom', level: 0, tag: 'value' },
  }, ctx.logger)
  logger.debug('message')

  function CamelCase(pluginCtx) {
    pluginCtx.logger().info('plugin')
  }
  const fiber = ctx.plugin(CamelCase)
  await fiber.await()
  const [first, second] = capture.messages
  const result = {
    meta: [first.name, first.type, first.level, first.tag],
    default_name: second.name,
    codes: [cordis.Logger.code('short', 1), cordis.Logger.code('veryLongLoggerName', 2)],
  }
  await effect()
  await ctx.fiber.dispose()
  return result
}

async function runDisposableList() {
  const values = new cordis.DisposableList()
  const first = { name: 'a' }
  const second = { name: 'b' }
  const disposeFirst = values.push(first)
  const disposeSecond = values.push(second)
  values.push(first)
  const before = [...values].map(value => value.name)
  const deleted = values.delete(first)
  const afterDelete = [...values].map(value => value.name)
  const disposed = disposeFirst()
  const cleared = values.clear().map(value => value.name)
  return {
    before,
    deleted,
    after_delete: afterDelete,
    disposed,
    cleared,
    length: values.length,
    stale_disposer: disposeSecond(),
  }
}

async function runServiceFacades() {
  const ctx = new cordis.Context()
  const child = ctx.extend()
  const trace = []
  const capture = {
    colors: false,
    messages: [],
    export(message) {
      this.messages.push(message)
    },
  }
  const exporter = child.logger.exporter(capture)
  const listener = child.events.on('facade', value => trace.push(String(value)))
  const provided = child.reflect.provide('facade_value', 7)
  function probe() {
    trace.push('plugin')
  }
  const fiber = child.registry.plugin(probe)
  await fiber
  child.events.emit('facade', 'event')
  child.logger('facade').info('logged')
  const result = {
    value: child.reflect.get('facade_value'),
    registered: child.registry.has(probe),
    trace,
    message: [capture.messages[0].name, capture.messages[0].args],
    contexts: [
      child.events.ctx === child,
      child.reflect.ctx === child,
      child.registry.ctx === child,
      child.logger.ctx === child,
    ],
  }
  listener()
  await provided()
  await exporter()
  await ctx.fiber.dispose()
  return result
}

async function runRemainingContracts() {
  const ctx = new cordis.Context()
  let activations = 0
  class Switchable extends cordis.Service {
    constructor(pluginCtx) {
      super(pluginCtx, 'switchable')
      this.enabled = true
    }
    [cordis.Service.check]() {
      return this.enabled
    }
  }
  const provider = await ctx.plugin(Switchable)
  const service = provider.ctx.reflect.get('switchable')
  function consumer() {
    activations++
  }
  consumer.inject = ['switchable']
  const dependent = await ctx.plugin(consumer)
  service.enabled = false
  await Promise.all(ctx.reflect.notify(['switchable']).map(fiber => fiber.await()))
  const unavailable = cordis.FiberState[dependent.state]
  service.enabled = true
  await Promise.all(ctx.reflect.notify(['switchable']).map(fiber => fiber.await()))
  const restored = cordis.FiberState[dependent.state]

  function probe() {}
  const first = await ctx.plugin(probe)
  const second = await ctx.plugin(probe)
  const runtime = ctx.registry.get(probe)
  const count = runtime.fibers.length
  const removed = ctx.registry.delete(probe)
  await first.dispose()
  await second.dispose()
  const result = {
    availability: [unavailable, restored, activations],
    registry: [count, removed === runtime, ctx.registry.has(probe)],
    await_returns_fiber: provider.ctx.fiber === provider,
  }
  await ctx.fiber.dispose()
  return result
}

const cases = {
  'core-smoke': runCoreSmoke,
  'plugin-shapes': runPluginShapes,
  'context-registry': runContextRegistry,
  'inject-delete': runInjectDelete,
  'strict-get': runStrictGet,
  'inject-metadata': runInjectMetadata,
  'context-filter': runContextFilter,
  'inject-config': runInjectConfig,
  'event-contracts': runEventContracts,
  'effect-contracts': runEffectContracts,
  'fiber-contracts': runFiberContracts,
  'fiber-invalid-update': runFiberInvalidUpdate,
  'fiber-failures': runFiberFailures,
  'fiber-dependency-races': runFiberDependencyRaces,
  'reflect-service': runReflectService,
  'accessor-mixin': runAccessorMixin,
  'logger-contracts': runLoggerContracts,
  'logger-options': runLoggerOptions,
  'disposable-list': runDisposableList,
  'service-facades': runServiceFacades,
  'remaining-contracts': runRemainingContracts,
}

const run = cases[scenario.id]
if (!run) usage(`unsupported scenario: ${scenario.id}`)
const result = await run()
if (process.argv.includes('--check') && JSON.stringify(result) !== JSON.stringify(scenario.expected)) {
  console.error(JSON.stringify({ expected: scenario.expected, actual: result }, null, 2))
  process.exit(1)
}
process.stdout.write(`${JSON.stringify(result)}\n`)
