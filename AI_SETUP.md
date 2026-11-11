# AI assistance

Each RFQ modal has a **Fill with AI** panel above the trade-detail form. For sovereign
and corporate bonds, describe one or more trades, select **Apply to form**, review every
block, then select **Create RFQs**. Derivatives accept one RFQ per form.
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
previous trades, previous response, and previous form state are never sent to the
model. Omitted optional values are cleared. Failures, unrelated requests, and
conflicting trades also leave a fresh form. Multiple distinct sovereign or corporate
trades create separate blocks; the derivative form rejects multiple trades. If details
are missing, complete the controls manually or include them in a complete description
and apply again. Sovereign and corporate warnings identify the trade number that needs attention.

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

`assets/aiFormControls.js` provides generic browser helpers for character counts,
running/review shading, and immediate resets. RFQ-specific defaults and labels stay in
the Python page, supplied through each `rfq-ai-ui-config-*` store. Callbacks use stable
`formControls` function names instead of content-generated JavaScript names, so editing
form settings does not change the function Dash calls. The asset loads before Dash's
renderer. Refresh an already-open tab after updating the clientside code.

Individual RFQ copying and Copy Grid share the browser helper in
`assets/dashAgGridComponentFunctions.js`. Python prepares each row's `rfq_copy_text`
using `_build_rfq_text()`; the row button copies it directly during the click instead
of waiting for a server callback. Where the secure Clipboard API is unavailable or
denied, the helper tries the browser's selection-based copy method. If both methods
are denied for an individual RFQ, a prompt provides the text for manual copying.
The close/conclude action remains a server callback.

`rfq_json_contract(asset_class, username, sovereign_assets=...)` defines a separate JSON contract for:

- `sovereign`: direction, requester, bond type, maturity, quantity,
  volume, and optional yield limit.
- `corporate`: direction, requester, instrument category, banking
  instrument description or security dropdown value, quantity, volume, and optional
  yield limit.
- `derivatives`: requester, ticker, tenor/unit, notional, premium, direction,
  strategy, strike/solve, and barrier/type/solve.

All responses have `asset_class`, `is_rfq_request`, `request_count`, and `has_conflicts`.
Sovereign and corporate responses each contain a `trades` array; each entry contains the exact
`fields` and `unresolved_fields` for one trade. `request_count` must match the array
length. Multiple distinct trades are accepted in their original order. Conflicting
instructions within a trade reset the batch; absent or invalid fields are cleared only
in that trade and reported by number. Derivative responses retain their single
`fields` / `unresolved_fields` envelope and reject multiple trades. The SDK
requests strict JSON, and Python validates it before assigning component values.

Sovereign and corporate modals repeat Trade Type, Traded Security, and Trade Definition for each
trade. The compact grey **+** button (Add trade) adds a blank block above the footer; each block's
**−** button removes it. Removing the final block leaves one fresh block. Adding or
removing preserves all other live edits and their stable row identities. Maturity
choices filter independently for each block using the shared catalog snapshot.

`sovereign_trade_block()` and `corporate_trade_block()` build their respective controls
and supply the AI contract's definitions. They share block headings, add/remove buttons,
and population through `fixed_income_trade_block()`. `manage_sovereign_trades()` and
`manage_corporate_trades()` delegate opening, adding, removing, and fresh AI replacement
to `manage_rfq_trade_blocks()`. A newly changed AI result takes priority over a batched
Apply/reset event. Pattern-matching IDs keep callbacks scoped to the
correct trade. The 200-character limit applies to the entire AI description, including
multi-trade requests. One AI call and one audit row cover the whole described batch.

`submit_fixed_income_batch()` reads the visible controls and validates all blocks before
inserting anything. Errors identify the trade number. All RFQs are inserted in
one SQLite transaction; any database failure rolls back every trade. The batch always
uses the signed-in requester and Mkt Color. `submit_sovereign_batch()` delegates to this
shared path. Derivative submission is unchanged.

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
Corporate security labels/values use `load_corporate_security_options()` once when the
corporate modal opens. `rfq-corporate-options` holds that snapshot for all blocks, AI
contracts, and batch validation; callers can supply `corporate_options` to
`rfq_json_contract()` or `extract_rfq()`. Closing clears it. Each block's category switch
independently shows either the Corporate Bonds dropdown or Banking Instruments text
input. The inactive input is cleared during AI interpretation and ignored at submission.
A corporate batch can mix categories. Catalog maturities are resolved when saving;
repeated securities reuse their maturity lookup within the batch.
No separate AI asset SQL exists. Required, optional, and conditional field rules
are shared by AI extraction and submission.

The extractor never inserts an RFQ. The Create RFQs / Create RFQ actions save
only after user review. Derivative strategy/strike/barrier controls are filled,
but persistence of those additional leg fields remains outside the current core
RFQ table submission.

## AI request history

Auditing now happens inside the shared `OpenAIService`, for **all** SDK requests,
including RFQ, routine extraction and routine conversations. Feature callbacks must
not create their own begin/finish records. `extract` and `converse` accept
`audit_context` (`username`, `form_type`, `text`, optional `chat_message_id`) and
return `log_id`; backend validation can mark that same row invalid. The default
database is `data/fixed_income.db`; isolated callers/tests pass `database_path`.

`RECORD_TYPE='REQUEST'` identifies individual attempts. `LEGACY_AGGREGATE` rows
preserve previously consolidated routine usage and must be excluded when counting
individual requests. Unknown historical prompt/model provenance is null.
`CHAT_MESSAGE_ID` links calls to the chat interaction; `UGM_ROUTINE_CHAT` contains
messages only and no token counters. Multi-call interactions therefore produce
several central request rows, not one aggregate request.

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

## Form alerts

Validation errors, missing trade details, invalid broker quotes, and database failures
are shown in the UI. Manual form alerts do not create SQLite log records.

AI validation warnings populate the existing `AI_REQUEST_LOG.ERROR_TYPE` and
`ERROR_MESSAGE` directly. A model response that cannot fill a valid form is marked
`invalid_response`, including `ConflictingRFQs`, `MissingRFQDetails`, and
`UnrelatedRequest`. Model output, full request, and token usage remain intact.
Provider failures keep their original status and error details. Empty or oversized
inputs do not call the model and therefore do not create an AI request-history row.

## Verification

```sh
python -m unittest discover -s tests -v
```

Run in the app's Dash environment for the RFQ HTTP callback tests. Tests use
isolated temporary databases and mocked model responses; no paid API calls are
made. They cover prompt/model selection, shared options and schemas, session
requesters, fresh-form replacement, conflicting requests, and saving all classes. Fixed-income batch
tests cover multiple AI trades, per-trade missing details, independent maturities,
add/remove preservation, replacement resets, and transaction rollback on a later trade.
Corporate tests also cover mixed banking/corporate categories, independent security
controls, shared catalog snapshots, inactive fields, and batched Apply/result events.
A SQL-trace test verifies a single sovereign catalog query across opening,
filtering, AI filling, and submission. Audit tests also cover unsaved/canceled RFQs,
repeated prompts, UTC timestamps,
transport failures, invalid JSON, and responses rejected by the form.
Additional checks verify formatted numeric controls, exact full-request snapshots,
AI warning persistence, visible manual validation alerts, transaction rollback, and
token usage logging for successful and failed responses.

Reference: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
