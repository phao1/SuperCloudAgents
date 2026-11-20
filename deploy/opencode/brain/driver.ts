import { Effect, Redacted } from "effect"
import { ChildProcess } from "effect/process"

export interface HanddInput { socket: string; python: string; helper: string }
interface DockerInput {
  workdir: string; container: string; docker_prefix: string[]; turn_marker?: string; handd?: HanddInput
}
const quote = (word: string) => `'${word.replaceAll("'", "'\\''")}'`
const unwrap = (word: string | Redacted.Redacted<string>) => Redacted.isRedacted(word) ? Redacted.value(word) : word
const hostEnv = () => ({ PATH: process.env.PATH ?? "/usr/bin:/bin" })

function remoteEnv(command: ChildProcess.StandardCommand, input: DockerInput): Record<string, string> {
  const env: Record<string, string> = {}
  // The brain's model credentials stay on the host with either transport.
  for (const [key, value] of Object.entries(command.options.env ?? {})) {
    if (value !== undefined && !/KEY|TOKEN|SECRET|PASSWORD|OPENCODE_CONFIG/i.test(key)) {
      env[key] = unwrap(value)
    }
  }
  if (input.turn_marker) env.SCA_BRAIN_TURN = input.turn_marker
  return env
}

export const dockerCommand = (command: ChildProcess.Command, input: DockerInput): ChildProcess.Command => {
  if (command._tag === "PipedCommand") {
    return { ...command, left: dockerCommand(command.left, input), right: dockerCommand(command.right, input) }
  }
  const args = ["exec", "-i", "-w", command.options.cwd ?? input.workdir]
  for (const [key, value] of Object.entries(remoteEnv(command, input))) args.push("-e", `${key}=${value}`)
  args.push(input.container)
  if (input.turn_marker) {
    // Persist the process group before exec; descendants may clear their environment.
    args.push("python3", "-c", [
      "import os,sys",
      "try: os.setsid()",
      "except OSError: pass",
      "path = '/tmp/sca-brain-' + sys.argv[1] + '-' + str(os.getpid())",
      "temporary = '/tmp/.sca-brain-' + sys.argv[1] + '-' + str(os.getpid())",
      "with open(temporary, 'w') as stream: stream.write(str(os.getpgrp()))",
      "os.replace(temporary, path)",
      "if os.path.exists('/tmp/sca-brain-' + sys.argv[1] + '.cancelled'): sys.exit(125)",
      "os.execvp(sys.argv[2], sys.argv[2:])",
    ].join("\n"), input.turn_marker)
  }
  args.push(command.command, ...command.args.map(unwrap))
  const prefix = input.docker_prefix
  const argv = prefix[0] === "sg"
    ? [...prefix.slice(1), ["docker", ...args].map(quote).join(" ")]
    : [...prefix.slice(1), ...args]
  return ChildProcess.make(prefix[0], argv, {
    ...command.options, cwd: undefined, shell: false,
    env: hostEnv(), extendEnv: false,
  })
}

const helperArgs = (handd: HanddInput) => ["-I", "-S", handd.helper, handd.socket]

export function handdCommand(command: ChildProcess.Command, input: DockerInput, handd: HanddInput,
  piped = false): ChildProcess.Command {
  if (command._tag === "PipedCommand") {
    return { ...command, left: handdCommand(command.left, input, handd, piped),
      right: handdCommand(command.right, input, handd, (command.options.to ?? "stdin") === "stdin") }
  }
  const env = remoteEnv(command, input)
  const pty = env.OPENCODE_TERMINAL === "1"
  const stdin = command.options.stdin
  const buffer_stdin = !pty && (piped || (typeof stdin === "object" &&
    (!("stream" in stdin) || typeof stdin.stream !== "string")))
  const options = { cwd: command.options.cwd ?? input.workdir, env, pty, buffer_stdin }
  return ChildProcess.make(handd.python,
    [...helperArgs(handd), "run", JSON.stringify(options), command.command, ...command.args.map(unwrap)],
    { ...command.options, cwd: undefined, shell: false, env: hostEnv(), extendEnv: false })
}

const probes = new WeakMap<HanddInput, Promise<boolean>>()
function probeHandd(handd: HanddInput): Promise<boolean> {
  const pending = probes.get(handd)
  if (pending) return pending
  const probe = (async () => {
    try {
      const child = Bun.spawn([handd.python, ...helperArgs(handd), "probe"], {
        env: hostEnv(), stdout: "ignore", stderr: "ignore", stdin: "ignore",
      })
      return await child.exited === 0
    } catch { return false }
  })()
  probes.set(handd, probe)
  return probe
}

export class IdleSuspension {
  private active = 0
  private closed = false
  private timer?: ReturnType<typeof setTimeout>
  private queue: Promise<void> = Promise.resolve()
  private pausedAt?: number
  private suspendedMs = 0
  constructor(private enabled: boolean, private delayMs: number,
    private control: (operation: "pause" | "unpause") => Promise<void>,
    private notify: (paused: boolean) => void = () => {}) {}
  private serial(action: () => Promise<void>) {
    const result = this.queue.then(action)
    this.queue = result.catch(() => {})
    return result
  }
  idle() {
    if (!this.enabled || this.closed || this.active || this.timer || this.pausedAt !== undefined) return
    this.timer = setTimeout(() => {
      this.timer = undefined
      void this.serial(async () => {
        if (this.active || this.closed || this.pausedAt !== undefined) return
        await this.control("pause")
        this.pausedAt = performance.now()
        this.notify(true)
      }).catch(() => {})
    }, this.delayMs)
  }
  private async resume() {
    if (this.pausedAt === undefined) return
    await this.control("unpause")
    this.suspendedMs += performance.now() - this.pausedAt
    this.pausedAt = undefined
    this.notify(false)
  }
  async begin() {
    if (this.closed) throw new Error("Brain suspension closed")
    this.active++
    clearTimeout(this.timer); this.timer = undefined
    try { await this.serial(() => this.resume()) }
    catch (error) { this.end(); throw error }
  }
  end() { this.active--; this.idle() }
  async close() {
    this.closed = true
    clearTimeout(this.timer); this.timer = undefined
    await this.serial(() => this.resume())
    return this.suspendedMs / 1000
  }
}

export function suspension(input: DockerInput & { suspend_idle?: boolean; suspend_idle_min_s?: number },
  notify: (paused: boolean) => void = () => {}) {
  return new IdleSuspension(input.suspend_idle === true, (input.suspend_idle_min_s ?? 2) * 1000,
    async (operation) => {
      const args = [operation, input.container], prefix = input.docker_prefix
      const child = Bun.spawn(prefix[0] === "sg"
        ? [...prefix, ["docker", ...args].map(quote).join(" ")]
        : [...prefix, ...args], {
          env: { PATH: process.env.PATH ?? "/usr/bin:/bin" }, stdout: "ignore", stderr: "pipe",
        })
      const timer = setTimeout(() => child.kill(), 10_000)
      try {
        const error = await new Response(child.stderr).text()
        if (await child.exited !== 0 && !error.includes(operation === "pause" ? "already paused" : "not paused"))
          throw new Error("Sandbox suspension failed")
      } finally { clearTimeout(timer) }
    }, notify)
}

export const spawnWithSuspension = (local: import("effect/process/ChildProcessSpawner").ChildProcessSpawner["Service"],
  command: ChildProcess.Command, input: DockerInput, idle: IdleSuspension) => Effect.gen(function* () {
    yield* Effect.promise(() => idle.begin())
    let ended = false
    const end = () => { if (!ended) { ended = true; idle.end() } }
    const handle = yield* Effect.gen(function* () {
      const handd = input.handd
      const remote = handd && (yield* Effect.promise(() => probeHandd(handd)))
        ? handdCommand(command, input, handd) : dockerCommand(command, input)
      return yield* local.spawn(remote)
    }).pipe(Effect.onError(() => Effect.sync(end)))
    yield* handle.exitCode.pipe(Effect.ensuring(Effect.sync(end)), Effect.forkScoped)
    return handle
  })
