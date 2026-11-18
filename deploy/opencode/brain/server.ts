import { Effect } from "effect"
import * as Spawner from "effect/process/ChildProcessSpawner"
import * as NodeServices from "@effect/platform-node/NodeServices"
import { AsyncLocalStorage } from "node:async_hooks"
import { createServer } from "node:net"
import { createInterface } from "node:readline"
import { pathToFileURL } from "node:url"
import { chmodSync, mkdirSync } from "node:fs"
import { suspension, spawnWithSuspension, IdleSuspension } from "./driver"
import { turns } from "./turn-provider"
import type { Context as PluginContext } from "@opencode/plugin/effect/plugin"
import type { Session } from "@opencode/schema/session"

interface Turn {
  op: "turn"; id: string; prompt: string; model: string; gateway_url: string
  container: string; workdir: string; task_id: string; session_id?: string
  suspend_idle?: boolean; suspend_idle_min_s?: number
  turn_marker: string; docker_prefix: string[]; model_key: string
}
const directory = process.argv[2]!
mkdirSync(directory, { recursive: true, mode: 0o700 })
chmodSync(directory, 0o700)
const load = (file: string) => import(pathToFileURL(`${process.env.SCA_OPENCODE_SOURCE}/packages/${file}`).href)
const { AbsolutePath, Location, Workspace } = await load("sdk/src/effect/index.ts")
const { OpenCode: Client } = await load("client/src/promise/index.ts")
const { EmbeddedHost } = await load("sdk/src/internal/host.ts")
const { runNonInteractivePrompt } = await load("cli/src/run/noninteractive.ts")
const validTurn = (value: unknown): value is Turn => {
  if (typeof value !== "object" || value === null) return false
  const input = value as Record<string, unknown>
  return ["id", "prompt", "model", "gateway_url", "container", "workdir", "task_id", "turn_marker", "model_key"]
    .every((key) => {
      const field = input[key]
      return typeof field === "string" && (key === "prompt" || field.length > 0)
    }) && Array.isArray(input.docker_prefix) && input.docker_prefix.length > 0 &&
    input.docker_prefix.every((part: unknown) => typeof part === "string") &&
    (input.session_id == null || typeof input.session_id === "string")
}
const active = new Map<string, Turn>()
const suspensions = new Map<string, IdleSuspension>()
const running = new Map<string, AbortController>()
const output = new AsyncLocalStorage<(line: string) => void>()
// The upstream renderer writes JSON to stdout. Route only its async context to its socket.
process.stdout.write = ((chunk: unknown, ...args: unknown[]) => {
  const sink = output.getStore()
  if (sink) sink(String(chunk))
  const callback = args.find((arg) => typeof arg === "function") as (() => void) | undefined
  callback?.()
  return true
}) as typeof process.stdout.write

await Effect.runPromise(Effect.scoped(Effect.gen(function* () {
  const local = yield* Spawner.ChildProcessSpawner
  const driver = {
    create: ({ workspaceID }: { workspaceID: string }) => Effect.succeed({ binding: { workspaceID } }),
    connect: ({ workspaceID }: { workspaceID: string }) => Effect.succeed({
      spawner: Spawner.make((command) => {
        const turn = active.get(workspaceID)
        const idle = suspensions.get(workspaceID)
        return turn && idle && !running.get(turn.id)?.signal.aborted ? spawnWithSuspension(local, command, turn, idle) : Effect.die("Inactive brain workspace")
      }),
    }),
    suspendForIdle: ({ workspaceID }: { workspaceID: string }) => Effect.sync(() => suspensions.get(workspaceID)?.idle()), destroy: () => Effect.void,
  }
  const host = yield* Effect.acquireRelease(EmbeddedHost.create({
    database: { path: `${directory}/brain.sqlite` }, workspaceProviders: { sca: driver },
    config: { directory, project: false, claude: false, content: "{}" },
    models: { fetch: false }, fs: { filewatcher: false, fff: false },
    instances: {
      key: (session: Session.Info) => {
        const workspace = session.location.workspaceID
        const turn = workspace ? active.get(workspace) : undefined
        if (!turn) throw new Error("Inactive brain workspace")
        return JSON.stringify([workspace, turn.model, turn.gateway_url])
      },
      configure: (key: string) => Effect.sync(() => {
        const [workspace, model, gateway] = JSON.parse(key)
        return { plugins: [{ id: "sca-turn-provider", effect: (ctx: PluginContext) =>
          ctx.provider.transform((providers) => {
            providers.update("sca", (provider) => {
              provider.name = "SCA gateway"; provider.activation = "enabled"
              provider.package = new URL("./turn-provider.ts", import.meta.url).pathname
              provider.settings = { workspace, baseURL: gateway }
            })
            providers.models.update("sca", model, (value) => { value.name = model })
          }) }] }
      }),
    },
  }), (host) => Effect.promise(host.close))
  const client = Client.make({ baseUrl: "http://opencode.local", fetch: host.fetch })
  const turn = async (input: Turn, emit: (value: object) => void) => {
    const workspaceID = `wrk_sca_${input.task_id}`
    if (active.has(workspaceID) || running.has(input.id)) {
      emit({ id: input.id, done: true, exit_code: 1 }); return
    }
    const controller = new AbortController()
    running.set(input.id, controller); active.set(workspaceID, input); turns.set(workspaceID, input)
    controller.signal.addEventListener("abort", () => turns.delete(workspaceID), { once: true })
    const idle = suspension(input, (paused) => emit({
      id: input.id, sca_suspend_transition: true, paused, at: Date.now() / 1000,
    }))
    suspensions.set(workspaceID, idle); idle.idle()
    controller.signal.addEventListener("abort", () => { void idle.close().catch(() => {}) }, { once: true })
    let failed = false
    try {
      const args = ["inspect", "--format", "{{.HostConfig.NetworkMode}}", input.container]
      const quote = (s: string) => "'" + s.replaceAll("'", "'\\''") + "'"
      const inspected = Bun.spawn(input.docker_prefix[0] === "sg"
        ? [...input.docker_prefix, ["docker", ...args].map(quote).join(" ")]
        : [...input.docker_prefix, ...args], {
          env: { PATH: process.env.PATH ?? "/usr/bin:/bin" }, stdout: "pipe", stderr: "ignore",
        })
      const inspectionTimeout = setTimeout(() => inspected.kill(), 10_000)
      const abortInspection = () => inspected.kill()
      controller.signal.addEventListener("abort", abortInspection, { once: true })
      try {
        if (await inspected.exited !== 0 || (await new Response(inspected.stdout).text()).trim() !== "none")
          throw new Error("Brain requires network=none")
      } finally {
        clearTimeout(inspectionTimeout)
        controller.signal.removeEventListener("abort", abortInspection)
      }
      if (controller.signal.aborted) throw new Error("Brain turn cancelled")
      await host.runtime.runPromise(host.workspace.create({ id: Workspace.ID.make(workspaceID), provider: "sca" }))
      const location = Location.Ref.make({ directory: AbsolutePath.make(input.workdir), workspaceID })
      const session = input.session_id ? await host.runtime.runPromise(host.sessions.get(input.session_id))
        : await host.runtime.runPromise(host.sessions.create({ location }))
      if (session.location.workspaceID !== workspaceID) throw new Error("Session workspace mismatch")
      let pending = ""
      await output.run((chunk) => {
        pending += chunk
        while (pending.includes("\n")) {
          const end = pending.indexOf("\n"), line = pending.slice(0, end)
          pending = pending.slice(end + 1)
          try {
            const event = JSON.parse(line)
            if (event.type === "error") failed = true
            const safe = JSON.stringify({ ...event, id: input.id }).replaceAll(JSON.stringify(input.model_key).slice(1, -1), "[redacted]")
            emit(JSON.parse(safe))
          } catch { /* Ignore non-JSON upstream diagnostics. */ }
        }
      }, () => runNonInteractivePrompt({
        client, sessionID: session.id, location, message: input.prompt, files: [],
        model: { providerID: "sca", id: input.model }, thinking: false, format: "json",
        auto: true, standalone: false, signal: controller.signal,
        renderTool: async () => {}, renderToolError: async () => {},
      }))
    } catch {
      failed = true
      emit({ id: input.id, type: "error", error: { message: "Brain turn failed" } })
    } finally {
      try { emit({ id: input.id, sca_suspend: true, suspended_s: await idle.close() }) }
      catch { failed = true }
      suspensions.delete(workspaceID)
      running.delete(input.id); active.delete(workspaceID); turns.delete(workspaceID)
      emit({ id: input.id, done: true, exit_code: failed || controller.signal.aborted ? 1 : 0 })
      process.exitCode = 0
    }
  }
  yield* Effect.promise(() => new Promise<void>((resolve) => {
    const server = createServer((socket) => {
      const owned = new Set<string>()
      const emit = (value: object) => { if (!socket.destroyed) socket.write(JSON.stringify(value) + "\n") }
      const lines = createInterface({ input: socket })
      lines.on("line", (line) => {
        if (line.length > 4 * 1024 * 1024) { socket.destroy(); return }
        try {
          const input = JSON.parse(line)
          if (input.op === "ping") emit({ id: input.id, pong: true, generation: process.argv[3], pid: process.pid, rss: process.memoryUsage().rss })
          else if (input.op === "cancel") { if (owned.has(input.id)) running.get(input.id)?.abort() }
          else if (input.op === "turn" && validTurn(input)) {
            owned.add(input.id)
            void turn(input, emit).finally(() => owned.delete(input.id))
          } else socket.destroy()
        } catch { socket.destroy() }
      })
      socket.on("error", () => {})
      socket.on("close", () => { for (const id of owned) running.get(id)?.abort() })
    })
    server.listen("brain.sock")
    const close = () => { for (const controller of running.values()) controller.abort(); server.close(() => resolve()) }
    process.once("SIGTERM", close); process.once("SIGINT", close)
  }))
})).pipe(Effect.provide(NodeServices.layer))).catch(() => { process.exitCode = 1 })
