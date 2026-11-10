# UGM AI Maintenance

Open **UGM → AI Maintenance** at `/ai-maintenance` with an active master profile.
The page rechecks the profile's key ID, user name, active flag and master role in
`USER_KEYS` before returning data, exporting records or activating a prompt.

- **Usage:** daily and month-to-date totals, input/output tokens, an hourly or daily
  chart, and breakdowns by user, routine/feature, model and user × routine/feature.
  Columns fit the available table width, with wrapped labels and values. Selecting
  a user/routine row filters usage; the combined table filters both together.
  **Inspect matching requests** opens the same scope in the request log.
  Model grouping uses the recorded model; unknown models stay in an Unknown group.
  The combined table counts each routine chat message once by its operational date,
  including messages processed without AI. Multiple model calls and legacy token
  aggregates do not multiply interaction counts. Request-only features such as RFQ
  show **—** interactions because they have no routine chat record. Token totals
  continue to use request start dates, so a call after midnight can fall on the day
  after its originating interaction. Chat-only rows show zero tokens when no AI
  request was made in the selected period; unreported request usage remains **—**.
- **Prompts:** searchable active prompt catalog, instructions, model and comment
  editing, version history, restoring a previous version into a new draft, and a
  review step before activation. Activation retains previous versions, records
  `CREATED_BY`, and applies to future calls. A version check inside the write
  transaction rejects an editor whose active version has changed.
- **Routine chats:** date/user/routine/status/text filters, column selection,
  sorting, server-side paging, a complete interaction inspector, day conversation
  and raw row inspection. Conversation grouping includes activity when available.
- **AI request log:** the same browsing tools, plus model, prompt version, record
  type and missing-usage filters. Inspect the submitted text, original request,
  model output, error and raw row; open the exact recorded prompt version or
  originating chat. **Back to investigation** restores the previous filters and
  selection.

Chat date filters use `DT_REF`. Request and usage date filters convert request
timestamps into `America/Sao_Paulo`, including the correct local day at midnight.
Opening linked records expands the date range to include every linked request.

Token counts are reported values from `AI_REQUEST_LOG`. Unknown usage stays null
and displays as **—**; reported zero remains zero. Historical aggregates contribute
known tokens without increasing the number of individual model calls. Unknown
historical model and prompt provenance are never assigned to the current prompt.

Browsing uses read-only SQLite connections and does not initialize or migrate the
operational tables. CSV export includes the entire filtered result and all original
columns, beyond the currently loaded grid page. Text cells that could be interpreted
as spreadsheet formulas are escaped.

Implementation: `pages/ai_maintenance.py`, `assets/ai_maintenance.css`,
`services/ai_maintenance.py`, and the optional concurrency/actor arguments on
`PromptStore.save_version`. No additional dependencies or AI calls are needed.

Run in the application's Python environment:

```sh
python -m unittest discover -s tests -p test_ai_maintenance.py -v
python -m unittest discover -s tests -v
```
