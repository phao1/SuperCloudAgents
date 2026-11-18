import { Redacted } from "effect"
import { ChildProcess } from "effect/process"

interface DockerInput { workdir: string; container: string; docker_prefix: string[]; turn_marker?: string }
const quote = (word: string) => `'${word.replaceAll("'", "'\\''")}'`
const unwrap = (word: string | Redacted.Redacted<string>) => Redacted.isRedacted(word) ? Redacted.value(word) : word
export const dockerCommand = (command: ChildProcess.Command, input: DockerInput): ChildProcess.Command => {
  if (command._tag === "PipedCommand") {
    return { ...command, left: dockerCommand(command.left, input), right: dockerCommand(command.right, input) }
  }
  const args = ["exec", "-i", "-w", command.options.cwd ?? input.workdir]
  // Never inherit the brain's environment into docker exec, including its gateway key.
  for (const [key, value] of Object.entries(command.options.env ?? {})) {
    if (value !== undefined && !/KEY|TOKEN|SECRET|PASSWORD|OPENCODE_CONFIG/i.test(key)) {
      args.push("-e", `${key}=${unwrap(value)}`)
    }
  }
  if (input.turn_marker) args.push("-e", `SCA_BRAIN_TURN=${input.turn_marker}`)
  args.push(input.container, command.command, ...command.args.map(unwrap))
  const prefix = input.docker_prefix
  const argv = prefix[0] === "sg"
    ? [...prefix.slice(1), ["docker", ...args].map(quote).join(" ")]
    : [...prefix.slice(1), ...args]
  return ChildProcess.make(prefix[0], argv, {
    ...command.options, cwd: undefined, shell: false,
    env: { PATH: process.env.PATH ?? "/usr/bin:/bin" }, extendEnv: false,
  })
}
