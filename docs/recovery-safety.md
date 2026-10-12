# Generation recovery and charging safety

Generation requests are potentially chargeable. A disconnected response, a
stopped client, or a server restart does not prove that the provider stopped.
No whole-generation automatic retry is performed after a read/write failure,
timeout, ambiguous 5xx, or download failure. The HTTP layer may retry a connection
only before that individual request was sent. A failed redirect destination does
not make the original submission safe to repeat.

## API contract

Task `status` keeps the existing `failed` / `partial_failed` values for index and
history compatibility. Render `result_certainty=submitted_unknown` as an unknown
provider outcome, not proof of provider failure. Each output slot, the aggregate
task card, and `GenerationErrorDetail` carry:

- `phase`: `submit`, `wait`, `download`, `save`, or `completed`
- `result_certainty`: `not_submitted`, `submitted_unknown`, `generated`, or `saved`
- `safe_retry_action`: `retry_submit`, `resume_poll`, `retry_download`,
  `retry_save`, `confirm_regenerate`, or `none`
- `provider_task_id` and `provider_request_id`, when returned

For mixed failed slots the aggregate prioritizes `confirm_regenerate`, then
`retry_save`, `retry_download`, `resume_poll`, and `retry_submit`. A request ID is
only diagnostic; it does not imply provider query or idempotency support.

`POST /api/tasks/{id}/retry-failed` resumes recoverable failed slots. Known URLs
are downloaded again, saved results are written locally, and T8 task IDs are
polled with GET against the same task. Completed slots are preserved. Ambiguous
submissions return HTTP 409 with `regeneration_confirmation_required`; sending
`{"confirm_regenerate":true}` explicitly permits a new, potentially chargeable
submission for those ambiguous slots.

For an expired URL, terminal provider task, or damaged result that cannot be
recovered, `{"regenerate":true,"confirm_regenerate":true}` explicitly regenerates
failed slots. Display the possible-additional-charge warning before sending it.
Tasks with a frozen generation snapshot keep their original provider even for
explicit regeneration. Submit a new task to choose another provider. Legacy tasks
also reject a provider switch during GET/save recovery. An expired URL never silently falls back to POST.

## Durable boundaries

A private journal is atomically committed before a generation POST, and a
successful provider response is committed before parsing/downloading its assets.
T8 submission receipts retain the task ID; completed poll responses are also
committed before download. Recovery feeds these receipts back through the same
parser, without sending a generation request. Decoded image results are spooled
before writing the final output. Partial recovery scans already-written output
slots even when the task had not yet saved their metadata.

A disk-full error can prevent any new durable write. If result bytes were
spooled, recovery is save-only. If only a response receipt survived, recovery may
need another download. If even the response receipt could not be written, the
pre-submission uncertainty marker remains and regeneration requires confirmation.
No claim of provider cancellation or durable local bytes is made in that case.

Stopping a task closes local work and preserves completed slots. The provider may
still finish and charge. Graceful shutdown does not automatically requeue an
ambiguous submitted operation. Restarted queued tasks with an uncertain receipt
are stopped for review. Corrupt receipts fail closed and require regeneration
confirmation. Previously attempted legacy failures without reliable submission
receipts also require confirmation. New schema-1 tasks commit a receipt before
sending; a task interrupted before that boundary remains `not_submitted` and can
be retried without the additional-charge confirmation. Providers without
query/idempotency support cannot recover a lost
response through an automatic new request.

## Private storage

Recovery/result JSON files live beside task metadata, use atomic fsync/rename
writes, and have mode 0600. They contain provider response bodies, generated
images, and potentially sensitive expiring asset URLs or generated text. They do
not contain outbound request headers, API keys, or cookie headers. Only response
Content-Type/request-ID headers are retained. Raw receipts and signed URLs are
never projected into task/card APIs or ordinary errors, and are not included in
history/config backup exports. Treat the source-data directory as private.

Image spools are removed after the output and its completed metadata are durable.
Completed operation bodies are compacted to small saved/no-resubmission markers.
Deleting the task removes its journals. Existing diagnostic/export paths use
allowlisted public metadata and do not collect these private files.
