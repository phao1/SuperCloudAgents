import { Effect, Redacted } from "effect"
import { pathToFileURL } from "node:url"

export const turns = new Map<string, { model_key: string; gateway_url: string; model: string }>()
const load = (file: string) => import(pathToFileURL(`${process.env.SCA_OPENCODE_SOURCE}/packages/ai/src/${file}`).href)
const { configure } = await load("providers/openai-compatible.ts")
const { Auth } = await load("route/auth.ts")

export function model(modelID: string, settings: { workspace: string; baseURL: string }) {
  // Cached models retain only the workspace ID; credentials are resolved for each HTTP request.
  const credential = Auth.effect(Effect.suspend(() => {
    const turn = turns.get(settings.workspace)
    return turn ? Effect.succeed(Redacted.make(turn.model_key))
      : Effect.fail(new Auth.MissingCredentialError("inactive brain turn"))
  }))
  return configure({ baseURL: settings.baseURL, auth: credential.bearer(), provider: "sca" }).model(modelID)
}
