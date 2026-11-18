import { Effect } from "effect"
import { dockerCommand } from "./driver"
import * as Spawner from "effect/process/ChildProcessSpawner"
import * as NodeServices from "@effect/platform-node/NodeServices"
import { pathToFileURL } from "node:url"

interface Input {
  prompt: string
  model: string
  gateway_url: string
  container: string
  workdir: string
  session_dir: string
  task_id: string
  session_id?: string
  turn_marker?: string
  docker_prefix: string[]
}
const checking = process.argv[2] === "--check-imports"
const input: Input = checking ? {} as Input : JSON.parse(process.argv[2] ?? await Bun.stdin.text())
if (!checking) {
  const inspectArgs = ["inspect", "--format", "{{.HostConfig.NetworkMode}}", input.container]
  const inspectPrefix = input.docker_prefix
  const inspected = Bun.spawn(inspectPrefix[0] === "sg"
    ? [...inspectPrefix, ["docker", ...inspectArgs].map((s) => "'" + s.replaceAll("'", "'\\''") + "'").join(" ")]
    : [...inspectPrefix, ...inspectArgs], {
      env: { PATH: process.env.PATH ?? "/usr/bin:/bin" }, stdout: "pipe", stderr: "ignore",
    })
  if (await inspected.exited !== 0 || (await new Response(inspected.stdout).text()).trim() !== "none") {
    console.log(JSON.stringify({ type: "error", error: { message: "Brain requires a network=none sandbox" } }))
    process.exit(1)
  }
}
const source = process.env.SCA_OPENCODE_SOURCE!
const load = (file: string) => import(pathToFileURL(`${source}/packages/${file}`).href)
const { AbsolutePath, Location, Workspace } = await load("sdk/src/effect/index.ts")
const { OpenCode: Client } = await load("client/src/promise/index.ts")
const { EmbeddedHost } = await load("sdk/src/internal/host.ts")
const { runNonInteractivePrompt } = await load("cli/src/run/noninteractive.ts")
if (checking) { console.log("brain imports ready"); process.exit(0) }
await Effect.runPromise(Effect.scoped(Effect.gen(function* () {
  const local = yield* Spawner.ChildProcessSpawner
  const spawner = Spawner.make((command) => local.spawn(dockerCommand(command, input)))
  const driver = {
    create: () => Effect.succeed({ binding: { container: input.container } }),
    connect: () => Effect.succeed({ spawner }),
    suspendForIdle: () => Effect.void,
    destroy: () => Effect.void,
  }
  const host = yield* Effect.acquireRelease(EmbeddedHost.create({
    database: { path: `${input.session_dir}/brain.sqlite` },
    workspaceProviders: { sca: driver },
    config: { directory: input.session_dir, project: false, claude: false, content: JSON.stringify({
      providers: { sca: { name: "SCA gateway", env: ["SCA_MODEL_KEY"],
        package: "@opencode/ai/providers/openai-compatible",
        settings: { baseURL: input.gateway_url }, models: { [input.model]: {} } } },
    }) },
    models: { fetch: false }, fs: { filewatcher: false, fff: false },
  }), (host) => Effect.promise(host.close))
  const workspaceID = yield* Effect.promise(() => host.runtime.runPromise(host.workspace.create({
    id: Workspace.ID.make(`wrk_sca_${input.task_id}`), provider: "sca",
  })))
  const location = Location.Ref.make({ directory: AbsolutePath.make(input.workdir), workspaceID })
  const session = input.session_id
    ? yield* Effect.promise(() => host.runtime.runPromise(host.sessions.get(input.session_id)))
    : yield* Effect.promise(() => host.runtime.runPromise(host.sessions.create({ location })))
  const client = Client.make({ baseUrl: "http://opencode.local", fetch: host.fetch })
  yield* Effect.promise(() => runNonInteractivePrompt({
    client, sessionID: session.id, location, message: input.prompt, files: [],
    model: { providerID: "sca", id: input.model }, thinking: false, format: "json",
    auto: true, standalone: true, signal: AbortSignal.timeout(850_000),
    renderTool: async () => {}, renderToolError: async () => {},
  }))
})).pipe(Effect.provide(NodeServices.layer))).catch(() => {
  console.log(JSON.stringify({ type: "error", error: { message: "Brain host failed" } }))
  process.exitCode = 1
})
