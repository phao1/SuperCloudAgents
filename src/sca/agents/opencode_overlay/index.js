import {readFileSync} from "node:fs";

const placeholder = "${OPENCODE_TOOL_GUIDANCE}";
// The pinned upstream system prompt renders its tool guidance between these lines.
const guidanceBlock = /(?<=- Prefer parallelizing independent tool calls\.\n)[\s\S]*?(?=\n# Communication\n)/;

export default {
  id: "sca.runtime-overlay",
  async setup(ctx) {
    const overlay = JSON.parse(readFileSync(ctx.options.path, "utf8"));
    if (overlay.tool_descriptions) {
      await ctx.tool.transform((editor) => {
        for (const [name, description] of Object.entries(overlay.tool_descriptions)) {
          editor.update(name, (tool) => { tool.description = description; });
        }
      });
    }
    await ctx.session.hook("context", (event) => {
      const system = event.system[0];
      const block = system?.text.match(guidanceBlock);
      const guidance = overlay.tool_guidance ?? block?.[0] ?? "";
      if (overlay.system_prompt !== undefined) {
        const prompt = overlay.system_prompt;
        const text = prompt.includes(placeholder)
          ? prompt.replace(placeholder, () => guidance)
          : prompt + "\n\n" + guidance;
        if (system) event.system[0] = {...system, text};
        else event.system.push({type: "text", text});
      } else if (overlay.tool_guidance !== undefined && system) {
        const text = block
          ? system.text.replace(guidanceBlock, () => guidance)
          : system.text + "\n\n" + guidance;
        event.system[0] = {...system, text};
      }
    });
  },
};
