import { expect, test } from "bun:test"
import { ChildProcess } from "effect/process"
import { dockerCommand } from "./driver"

const input = { container: "sca-test", workdir: "/workspace", docker_prefix: ["docker"] }
test("remote cwd and explicit environment, no inherited gateway credential", () => {
  const result = dockerCommand(ChildProcess.make("sh", ["-c", "cat > file"], {
    cwd: "/workspace/sub", extendEnv: true,
    env: { LC_ALL: "C", SCA_MODEL_KEY: "fake-key", AWS_SECRET_ACCESS_KEY: "fake-secret" },
  }), input)
  expect(result._tag).toBe("StandardCommand")
  if (result._tag !== "StandardCommand") throw new Error("expected standard command")
  expect(result.command).toBe("docker")
  expect(result.args).toEqual(["exec", "-i", "-w", "/workspace/sub", "-e", "LC_ALL=C",
    "sca-test", "sh", "-c", "cat > file"])
  expect(result.options.extendEnv).toBe(false)
  expect(result.options.cwd).toBeUndefined()
  expect(result.options.env).not.toHaveProperty("SCA_MODEL_KEY")
})
test("sg prefix quotes shell arguments", () => {
  const result = dockerCommand(ChildProcess.make("printf", ["$(false); 'hello'"]), {
    ...input, docker_prefix: ["sg", "docker", "-c"],
  })
  expect(result.command).toBe("sg")
  expect(result.args[2]).toContain("'$(false); '\\''hello'\\'''\n".trim())
})

test("turn marker tags remote commands", () => {
  const result = dockerCommand(ChildProcess.make("sleep", ["60"]), {
    ...input, turn_marker: "turn-test",
  })
  if (result._tag !== "StandardCommand") throw new Error("expected standard command")
  expect(result.args).toContain("SCA_BRAIN_TURN=turn-test")
})

test("turn commands record a process group before exec and reject cancelled turns", async () => {
  const marker = `driver-test-${crypto.randomUUID()}`
  const result = dockerCommand(ChildProcess.make("sh", ["-c", "exit 99"]), {
    ...input, turn_marker: marker,
  })
  if (result._tag !== "StandardCommand") throw new Error("expected standard command")
  const args = result.args.map(String)
  const wrapper = args.indexOf("python3")
  expect(wrapper).toBeGreaterThan(0)
  const cancelled = `/tmp/sca-brain-${marker}.cancelled`
  await Bun.write(cancelled, "")
  try {
    const child = Bun.spawn(args.slice(wrapper), { stdout: "ignore", stderr: "ignore" })
    expect(await child.exited).toBe(125)
    const group = `/tmp/sca-brain-${marker}-${child.pid}`
    expect(Number(await Bun.file(group).text())).toBeGreaterThan(0)
    await Bun.file(group).delete()
  } finally {
    await Bun.file(cancelled).delete()
  }
})

const { IdleSuspension, spawnWithSuspension } = await import("./driver")
const { Effect } = await import("effect")
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

test("idle threshold, parallel tools, resume ordering and close", async () => {
  const calls: string[] = []
  const idle = new IdleSuspension(true, 20, async (operation) => { calls.push(operation) })
  idle.idle()
  await sleep(5)
  await idle.begin()
  await sleep(30)
  expect(calls).toEqual([])
  await idle.begin()
  idle.end()
  await sleep(30)
  expect(calls).toEqual([])
  idle.end()
  await sleep(40)
  expect(calls).toEqual(["pause"])
  await idle.begin()
  calls.push("exec")
  expect(calls).toEqual(["pause", "unpause", "exec"])
  idle.end()
  await sleep(40)
  expect(await idle.close()).toBeGreaterThan(0)
  expect(calls.at(-1)).toBe("unpause")
  const count = calls.length
  idle.idle()
  await sleep(30)
  expect(calls.length).toBe(count)
})

test("a pause in flight completes before tool resume", async () => {
  const calls: string[] = []
  let release!: () => void
  const paused = new Promise<void>((resolve) => { release = resolve })
  const idle = new IdleSuspension(true, 1, async (operation) => {
    calls.push(operation)
    if (operation === "pause") await paused
  })
  idle.idle()
  await sleep(10)
  const tool = idle.begin().then(() => calls.push("exec"))
  await sleep(5)
  expect(calls).toEqual(["pause"])
  release()
  await tool
  expect(calls).toEqual(["pause", "unpause", "exec"])
  idle.end()
  await idle.close()
})

test("disabled suspension issues no freezer commands", async () => {
  const idle = new IdleSuspension(false, 1, async () => { throw new Error("unexpected") })
  idle.idle(); await sleep(5)
  await idle.begin(); idle.end()
  expect(await idle.close()).toBe(0)
})

test("spawn holds sandbox active until process exit", async () => {
  const calls: string[] = []
  const idle = new IdleSuspension(true, 5, async (operation) => { calls.push(operation) })
  const local = { spawn: () => Effect.succeed({ exitCode: Effect.promise(async () => {
    await sleep(30); calls.push("exit"); return 0
  }) }) }
  await Effect.runPromise(Effect.scoped(Effect.gen(function* () {
    yield* spawnWithSuspension(local as unknown as Parameters<typeof spawnWithSuspension>[0], ChildProcess.make("true"), input, idle)
    yield* Effect.promise(() => sleep(20))
    expect(calls).toEqual([])
    yield* Effect.promise(() => sleep(30))
    expect(calls).toEqual(["exit", "pause"])
    yield* Effect.promise(() => idle.close())
  })))
})
