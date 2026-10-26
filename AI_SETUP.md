# AI assistance

Each RFQ modal has a **Fill with AI** panel above the trade-detail form. Describe one complete
RFQ, select **Apply to form**, review the filled controls, then create the RFQ.
English and Portuguese descriptions are supported.

Quantity, volume, and derivative notional controls display comma thousands
separators (for example, `1,234` and `1,234,567.89`). Their values remain numeric
for AI population, validation, and SQLite storage. Quantities accept whole units.

The AI description is limited to **200 characters**, with a live `0 / 200` counter.
Its text area has a fixed two-line height. Python also rejects oversized input
before any model call. While AI is running, the input panel highlights; after
fields are populated, that panel becomes neutral and the trade-details block
below receives the shading and a review reminder. A new run or modal opening
resets the review highlight. Failed or empty responses keep focus on the input.

Every modal opening clears the trade fields, previous AI text, and status. The
signed-in requester is supplied automatically. Every AI run also resets the trade fields. The
previous trade, previous response, and previous form state are never sent to the
model. Omitted optional values are cleared. Failures, unrelated requests, and
multiple or conflicting trades also leave a fresh form. If details are missing,
include them in a complete description and apply again.

The requester is a read-only field containing the signed-in username. Python uses
that username when saving `RFQ.REQUESTER` and `RFQ.USR_INPUT`, for every asset class
and quote type. Neither model output nor a supplied form value can override it.

Sovereign and corporate RFQs always use **Mkt Color**. There is no quote-type
selector, no quote-type input required from the user, and no AI-editable quote-type
field. The AI contract declares `fixed_values.quote_type = "Mkt Color"`; Python
supplies and enforces the value when saving. The active fixed-income prompts in
SQLite describe this rule as well.

## Configuration

Install the SDK in the environment that runs `app.py`:

```sh
python -m pip install -r requirements-ai.txt
```

Set `OPENAI_API_KEY` in the server environment, then restart the app. `.env` files
are ignored by Git but are not automatically loaded. Keep the key outside source
control and out of browser-side code.

**The prompt and model both come from SQLite.** There is no Python default prompt,
no model fallback, and no `OPENAI_MODEL` override. A missing active prompt, empty
instructions, or empty model prevents the AI call and displays an unavailable
message. Requests are stateless, have no tools or automatic retries, and use a
45-second timeout with `store=False`.

## SQLite prompt configuration

`AI_PROMPTS` in `data/fixed_income.db` contains:

| Column | Meaning |
| --- | --- |
| `PROMPT_ID` | Unique prompt identifier |
| `FEATURE` | Feature scope, such as `rfq` |
| `PROMPT_KEY` | Task/form identifier |
| `VERSION` | Prompt version |
| `INSTRUCTIONS` | Full instructions sent to the model |
| `MODEL` | Model used for that prompt |
| `COMMENT` | Editable notes about code locations, activation conditions, and expected effects |
| `FL_ACTIVE` | Whether this version is active |
| `DT_CREATED` | UTC creation timestamp |

The RFQ entries are `fill_sovereign`, `fill_corporate`, and `fill_derivatives`, all
under `FEATURE = 'rfq'`. The active SQLite row selects the model for each form;
previous versions are retained as inactive history. The active sovereign/corporate prompts
explicitly treat `500k`, `2mm`, and `10k` as trade values (500,000,
2,000,000, and 10,000 in `volume`) and `500q`, `10q`, and `25q` as quantities
(500, 10, and 25 in `quantity`). Startup never seeds or overwrites instructions
or chooses a model.

Inspect or edit the active configuration using your SQLite editor:

```sql
SELECT PROMPT_ID, PROMPT_KEY, VERSION, MODEL, "COMMENT", INSTRUCTIONS
FROM AI_PROMPTS
WHERE FEATURE = 'rfq' AND FL_ACTIVE = 1;
```

Choose a model supporting the Responses API and strict JSON Schema outputs.
For versioned edits, the generic `PromptStore.save_version(feature, key,
instructions, model)` method creates a new active row atomically and retains
previous versions. Each API request carries the selected prompt ID/version in its
metadata.

`COMMENT` is free-form documentation stored only in SQLite; it is not included in
the model instructions or input. Existing RFQ prompt rows include the relevant
page/functions, the Apply to form activation conditions, and the fields affected.
Edit these notes directly in SQLite without changing prompt behavior. Existing
notes are never overwritten by schema initialization.

`save_version(..., comment="New notes")` replaces the notes for the new version.
Omitting `comment` carries the current notes forward; `comment=""` clears them.
Earlier versions retain their own comments.

## RFQ code and JSON contracts

All RFQ Python behavior lives in `pages/request_for_quote.py`: form layouts,
SQLite asset loaders, field mappings, schemas, validation, extraction, callbacks,
and submission. `services/openai_client.py` contains only reusable SDK transport;
`services/prompts.py` contains only reusable prompt storage, and
`services/ai_audit.py` provides reusable SQLite request logging.

`rfq_json_contract(asset_class, username, sovereign_assets=...)` defines a separate JSON contract for:

- `sovereign`: direction, requester, bond type, maturity, quantity,
  volume, and optional yield limit.
- `corporate`: direction, requester, instrument category, banking
  instrument description or security dropdown value, quantity, volume, and optional
  yield limit.
- `derivatives`: requester, ticker, tenor/unit, notional, premium, direction,
  strategy, strike/solve, and barrier/type/solve.

Each response has `asset_class`, `is_rfq_request`, `request_count`, `has_conflicts`,
`fields`, and `unresolved_fields`. The SDK requests strict JSON, and Python validates
and interprets it before assigning component values. Several trades or conflicts
cause the response to be rejected as a single form entry.

The contract reads fixed options and blank initial values from the actual Dash
components. `load_sovereign_assets()` performs one query when the sovereign modal
opens and returns a list containing each asset class, maturity, and canonical
security identifier, for example:

```json
[{"asset_class": "NTN-B", "maturity": "2029-05-15", "security": "NTN-B May-29"}]
```

The list is held in `rfq-sovereign-assets`. `sovereign_form_options()` builds both
bond choices and grouped maturity choices in Python. Selection changes, the AI
contract, and submission reuse that same snapshot without querying sovereign
assets again. Closing clears the snapshot; reopening performs one fresh query.
Direct Python callers can also pass `sovereign_assets` to `rfq_json_contract` and
`extract_rfq`; if omitted, the contract loads the entire catalog with one query.
Corporate security labels/values still use `load_corporate_security_options()`.
No separate AI asset SQL exists. Required, optional, and conditional field rules
are shared by AI extraction and submission.

The extractor never inserts an RFQ. The existing Create RFQ action still saves
only after user review. Derivative strategy/strike/barrier controls are filled,
but persistence of those additional leg fields remains outside the current core
RFQ table submission.

## AI request history

`AI_REQUEST_LOG` in the same SQLite database records every AI attempt independently
of RFQ submission. The request is committed before the API call and completed when
the response is processed. Canceling the modal or never creating an RFQ does not
remove its history. Repeating the same text creates a separate row each time.

The log stores the user's original `PROMPT_TEXT`, session `USERNAME`, UTC
`DT_STARTED` / `DT_FINISHED`, and the original `MODEL_OUTPUT` before Python
normalizes or rejects its fields. It also records feature/form type, prompt ID and
version, model, status, and error details. `MODEL_OUTPUT` is the only output column:
it preserves JSON, invalid text, or incomplete output exactly as returned. It is
null if no model output was returned. Response IDs are not stored in the log.
Existing history is migrated to this single column without dropping its records.
Provider errors are sanitized rather than storing credentials or SDK exception
messages.

`FULL_PROMPT` contains the complete request body for each new attempt: the model,
system instructions, serialized user input, requester, complete form contract,
allowed options, strict output schema, and generation settings. Both logging and
the SDK use `build_structured_request()` in `services/openai_client.py`, so this is
the same body sent to the model. API keys and authentication headers are excluded.
Historical records retain a null `FULL_PROMPT` because their original context was
not captured; the migration preserves all existing inputs and outputs.

Each new request also records `INPUT_TOKENS`, `OUTPUT_TOKENS`, and `TOTAL_TOKENS`
from the API response's usage fields. Counts are saved independently of RFQ
submission, including incomplete, refused, malformed, or form-rejected responses
when the API reports usage. Values are not estimated from text length. Historical
rows and failures without reported usage retain null counts; an API-reported zero
is stored as zero. Totals are the API-reported totals rather than a local estimate.

Statuses are `running`, `completed`, `failed`, and `invalid_response`. A process
interrupted while waiting for the model may leave a `running` row with its request
already saved. Missing details, conflicting trades, and unrelated requests still
retain their model response even when the form is not populated. Empty/oversized
text and missing prompt definitions do not invoke the model and do not create a
request-history row. SDK/key configuration failures after an attempt starts are
logged as failed attempts.

If the initial log record cannot be saved, the model is not called. Request-history
writes use their own short transactions; they do not depend on the RFQ transaction.

```sql
SELECT LOG_ID, USERNAME, FORM_TYPE, DT_STARTED, STATUS,
       INPUT_TOKENS, OUTPUT_TOKENS, TOTAL_TOKENS,
       PROMPT_TEXT, FULL_PROMPT, MODEL_OUTPUT
FROM AI_REQUEST_LOG
WHERE FEATURE = 'rfq'
ORDER BY LOG_ID DESC;
```

## Form errors and alerts

`FORM_EVENT_LOG` records user-facing AI warnings/errors, submission validation
notifications, broker-quote validation errors, and failed database operations.
It includes `USERNAME`, UTC `DT_EVENT`, `FORM_TYPE`, `LEVEL`, `SOURCE`, `MESSAGE`,
optional `AI_LOG_ID` / `RFQ_ID`, and diagnostic `CONTEXT_JSON`. An AI warning links
to its request log when one exists. Empty/oversized AI inputs and manual form
validation are logged here even though no model call or RFQ submission occurs.

Linked AI warnings/errors also populate `AI_REQUEST_LOG.ERROR_TYPE` and
`ERROR_MESSAGE` in the same transaction as the event. A model call that completed
but could not fill a valid form is marked `invalid_response`. Examples include
`ConflictingRFQs`, `MissingRFQDetails`, and `UnrelatedRequest`. Model output, full
request, and token usage remain intact. Existing transport/API failures keep their
original status and error details; the event retains the full user-facing message.

Broker validation rolls back its RFQ transaction before logging the alert so the
event write does not contend with its own SQLite lock. If SQLite itself is
unavailable, logging falls back to the server log and the original user alert is
still displayed. Routine resets, loading indicators, and `PreventUpdate` control
flow do not create error records.

```sql
SELECT DT_EVENT, USERNAME, FORM_TYPE, LEVEL, SOURCE, MESSAGE, AI_LOG_ID, RFQ_ID
FROM FORM_EVENT_LOG
WHERE FEATURE = 'rfq'
ORDER BY EVENT_ID DESC;
```

## Verification

```sh
python -m unittest discover -s tests -v
```

Run in the app's Dash environment for the RFQ HTTP callback tests. Tests use
isolated temporary databases and mocked model responses; no paid API calls are
made. They cover prompt/model selection, shared options and schemas, session
requesters, fresh-form replacement, conflicting requests, and saving all classes.
A SQL-trace test verifies a single sovereign catalog query across opening,
filtering, AI filling, and submission. Audit tests also cover unsaved/canceled RFQs,
repeated prompts, UTC timestamps,
transport failures, invalid JSON, and responses rejected by the form.
Additional checks verify formatted numeric controls, exact full-request snapshots,
warning/error persistence before model calls and after RFQ rollback, and token
usage logging for successful and failed responses.

Reference: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
