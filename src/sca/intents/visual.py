"""Use Playwright Test's screenshot matcher in the optional web image."""

import json
import shlex
from pathlib import Path

from pydantic import Field

from sca.intents.base import Check, IntentBundle, IntentSource, source_bytes
from sca.intents.openapi import ServiceParams


class VisualParams(ServiceParams):
    page_url: str = Field(pattern=r"^https?://", max_length=2048)
    # Measured in pixels, not as a share of the page: a button that changed colour is a few
    # hundred pixels of a 1280x720 page (<0.1%) yet exactly what a design check must catch.
    # Anti-aliasing noise is absorbed by Playwright's per-pixel colour tolerance.
    max_diff_pixels: int = Field(default=50, ge=0, le=10_000_000)
    max_diff_pixel_ratio: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    width: int = Field(default=1280, ge=1, le=4096)
    height: int = Field(default=720, ge=1, le=4096)


class VisualAdapter:
    type = "visual"

    def compile(self, source: IntentSource, workspace: Path) -> IntentBundle:
        params = VisualParams.model_validate(source.params)
        data = source_bytes(source, workspace)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("visual baseline must be PNG")
        name = source.name
        test = f"{name}.spec.cjs"
        diff = f".sca/context/visual-{name}/diff.png"
        code = """const {test, expect} = require('/opt/sca-web/node_modules/@playwright/test');
const fs = require('fs');
test('design intent', async ({page}) => {
  await page.setViewportSize(settings.viewport);
  await page.goto(settings.url);
  try { await expect(page).toHaveScreenshot('baseline.png', settings.limits); }
  catch (error) {
    const image = test.info().outputPath('baseline-diff.png');
    if (fs.existsSync(image)) fs.copyFileSync(image, settings.diff);
    console.log('Visual diff: ' + settings.diff);
    throw error;
  }
});
"""
        values = {
            "viewport": {"width": params.width, "height": params.height},
            "url": params.page_url,
            "limits": {
                "maxDiffPixels": params.max_diff_pixels,
                **(
                    {"maxDiffPixelRatio": params.max_diff_pixel_ratio}
                    if params.max_diff_pixel_ratio is not None
                    else {}
                ),
            },
            "diff": diff,
        }
        code = "const settings = " + json.dumps(values) + ";\n" + code
        config = name + "-service.json"
        argv = [
            "/opt/sca-web/node_modules/.bin/playwright",
            "test",
            "--config",
            ".sca/context/" + name + ".config.cjs",
        ]
        configuration = {
            "testDir": ".",
            "testMatch": test,
            "workers": 1,
            "snapshotPathTemplate": "{testDir}/" + name + "-baseline.png",
            "outputDir": "visual-" + name,
            "use": {"browserName": "chromium"},
        }
        service = ServiceParams.model_validate(
            {k: getattr(params, k) for k in ServiceParams.model_fields}
        )
        return IntentBundle(
            context_files={
                name + "-baseline.png": data,
                test: code.encode(),
                name + ".config.cjs": ("module.exports = " + json.dumps(configuration)).encode(),
                config: json.dumps({**service.model_dump(), "argv": argv}).encode(),
                "service_runner.py": Path(__file__).with_name("service_runner.py").read_bytes(),
            },
            checks=[
                Check(
                    name=name,
                    command=shlex.join(
                        ["python", ".sca/context/service_runner.py", ".sca/context/" + config]
                    ),
                    artifacts=[diff],
                )
            ],
        )
