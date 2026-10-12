# Named generation presets and final preview

Generation presets save reusable output settings. They are separate from prompt
templates: applying a preset keeps the prompt and the current reference inputs.
Use a prompt template for text and a generation preset for the model, provider
binding and output parameters.

## What is saved

- A name, operation, canonical model ID, provider ID and explicit binding ID.
- Applicable canonical generation parameters, including size, quality, output
  count and model-specific options.
- Applicable prompt-processing options, such as fidelity and the Responses main
  model. The transport mode is determined by the explicit provider binding.
- Reference-input rules: preserve, require or forbid images/documents, while
  preserving their current order.

Presets do not save prompt text, credentials, provider URLs, machine-local file
paths, reference IDs or copies of original images. A rule that forbids references
does not delete them from the composer: incompatible inputs must be reviewed.

Names are Unicode-normalized and must be unique. Renaming and deleting a preset
does not change existing tasks. Deletion requires confirmation. Applying an
unavailable model/provider/binding or an incompatible parameter set is rejected
with an explanation; no replacement provider is selected silently.

## Final submission preview

The preview uses the same canonical resolver and request preparation as task
submission. It shows the resolved provider/model/binding, effective output
parameters, output/task counts, concurrency limits and reference order.
Previewing does not submit a provider generation request or enqueue a task.

Changing inputs or applying a preset invalidates the old preview. The browser
discards stale responses. A short-lived preview token also lets the server detect
changes to the effective plan or provider configuration before accepting the
submission. Review the updated summary if the server reports it is stale.

The summary is not a price estimate or a promise that stopping a submitted task
prevents charging. Existing durable submission IDs and recovery safeguards remain
in force; see [generation recovery and charging safety](recovery-safety.md).

## Storage and backup compatibility

Presets use a versioned, atomically written `generation-presets.json` in the
profile's source-data directory. A profile without that file starts with an empty
list. Invalid or unsupported future schemas are reported and are not silently
replaced with an empty list. Keep the file when moving the source-data directory.

Configuration backups include an optional `settings/generation-presets.json`
member. The archive format remains version 1, and older archives without that
member remain readable. Restoring an older archive preserves existing presets in
both incremental and replacement modes. Incremental restoration preserves local
items and gives conflicting imported copies distinct names; replacement follows
the existing preview, confirmation, empty-replacement and rollback safeguards.

Earlier application versions reject archives containing this new optional
member, because their member allowlist does not recognize it. Upgrade the target
application before restoring such an archive. No existing backup is rewritten.
Restored presets keep their provider IDs; configure a missing provider before
applying them rather than silently rebinding them.

For a hand-authored or imported preset without optional `request_options`, the
current auxiliary prompt-processing settings are preserved. The final summary
shows the actual options that will be submitted. Newly saved presets record the
applicable options explicitly.

Variable combinations, project entities, provider pricing and automatic package
replacement are outside this feature.
