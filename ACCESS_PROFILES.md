# Access profiles

Master users can create keys for **Trader**, **GWM**, or **Master** in UGM → Access.
GWM is stored as `ROLE = 'gwm'` in `USER_KEYS`; the existing text column needs no migration.
No accounts or keys are created automatically for this profile.

GWM users log in through the account bubble. They can use Fixed Income and Request
for Quote. The FX and UGM navigation groups are hidden. Their routes, including FX
Trading, Routines and its workspaces, AI Maintenance, and Access, are restricted during
in-app navigation and direct URL visits. Direct server callbacks for these pages are
also rejected. Existing Trader and Master access rules continue to apply.
FX and UGM pages require an active login through the account bubble.

For sovereign and corporate RFQs, GWM receives one read-only **Best** quote column.
It uses the same yield comparison as the existing green highlights: highest for Buy,
lowest for Sell, using each active counterparty's latest quote. Non-numeric quotes are
ignored, and missing quotes display `--`. The chosen quote retains its own timestamp.
Closed RFQs use the same view. Other counterparty quotes and names are omitted from
the returned grid data and column definitions, including the initial layout. Copy Grid
copies the displayed Best quote. Creating RFQs continues to use the existing forms.
GWM users can conclude **Mkt Color** RFQs in any tab. The conclude action remains
visible for every trade, but other quote types do not respond to it; its tooltip
explains the restriction. The server also checks the authenticated database role
and the RFQ's stored type in the update itself, so modified browser data cannot
conclude other types. Trader and Master users can still conclude all quote types.
Copying individual RFQs and the grid remains available. Derivative quote columns
are unchanged.

While the RFQ page is open and signed in, each browser checks the shared SQLite
database every two seconds for new RFQs, quote edits, and concluded trades. Each
refresh uses the authenticated profile's view, including Best-only quotes for GWM.
Unchanged data is left alone. Incoming updates wait while a grid cell is being
edited, and they do not close or reset open RFQ forms. All server instances must
use the same database for their users to see the same trades.

Profile checks live in `services/access.py`. A successful key login stores the key ID
in a signed, HttpOnly server-session cookie. Access checks re-read the active database
role; changing the browser's profile fields does not override that signed identity.
Logout clears the cookie identity. Set `DASH_SECRET_KEY` in the server environment to
use a configured signing key. Otherwise the app creates a private key in
`.instance/session-secret` and reuses it across restarts and local workers. This
directory is excluded from Git. Keep the same environment key across deployed
workers. An already-expired session must sign in again after this update; the RFQ
page displays a sign-in message instead of an unexplained empty grid.

Run the profile, routing, key-management, RFQ response, and quote-editing checks with:

```sh
python -m unittest discover -s tests -q
```
