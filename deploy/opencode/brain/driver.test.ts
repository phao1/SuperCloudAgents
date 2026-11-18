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
