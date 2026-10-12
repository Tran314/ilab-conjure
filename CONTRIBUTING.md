# Contributing

Thanks for your interest in improving iLab CONJURE.

Before opening a pull request:

1. Keep changes focused.
2. Do not commit local inputs, outputs, credentials, task databases, or logs.
3. Run the relevant checks:

```bash
.venv/bin/python -m unittest discover -s tests -v
npm run check:webui
```

Frontend TypeScript and CSS changes should include the generated static assets
under `codex_image/webui/static/`.

The preset/final-summary browser regression uses a disposable local profile and
disables queue execution. After installing the locked npm/Python dependencies:

```bash
npx playwright install chromium
PYTHON=.venv/bin/python npm run test:webui:browser
```

Linux also needs Playwright's browser system libraries; CI installs them with
`npx playwright install --with-deps chromium`. The test refuses an unrelated
process on its test port and never sends provider generation requests. Screenshots
are saved under `.dist/presets-browser/`. The same regression runs in CI.
