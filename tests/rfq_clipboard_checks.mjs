// Run with: node --input-type=module -e "import('./tests/rfq_clipboard_checks.mjs').then(m => m.runClipboardChecks()).then(console.log)"
export async function runClipboardChecks() {
    const assert = (await import("node:assert/strict")).default;
    const originals = {window: globalThis.window, document: globalThis.document, React: globalThis.React};
    let state;
    const reset = () => {
        state = {legacy: [], modern: [], prompts: [], mounted: [], focusRestored: 0, legacyResult: true};
        window.isSecureContext = false;
        window.navigator = {};
    };
    globalThis.window = {dash_clientside: {no_update: {}},
        prompt: (message, text) => { state.prompts.push({message, text}); }};
    globalThis.React = {createElement: (type, props, ...children) => ({type, props, children})};
    globalThis.document = {
        activeElement: {focus: () => { state.focusRestored += 1; }},
        body: {appendChild: node => state.mounted.push(node),
               removeChild: node => state.mounted.splice(state.mounted.indexOf(node), 1)},
        createElement: () => ({style: {}, setAttribute() {}, focus() {}, select() {}, setSelectionRange() {}}),
        execCommand: command => {
            assert.equal(command, "copy");
            state.legacy.push(state.mounted.at(-1).value);
            if (state.legacyError) throw new Error("Copy denied");
            return state.legacyResult;
        },
    };
    try {
        reset();
        await import(new URL("../assets/dashAgGridComponentFunctions.js", import.meta.url));
        const rfq = window.dash_clientside.rfq;
        const row = {rfq_id: 42, rfq_copy_text: "offer NTN-B Aug-50 500k BRL D0"};
        const events = [];
        const cell = window.dashAgGridComponentFunctions.RfqActionsCell({data: row, setData: value => events.push(value)});

        // Insecure context: fallback must run during the original row click, before any await.
        window.navigator.clipboard = {writeText: () => { throw new Error("Must not request secure API on HTTP"); }};
        let stopped = false;
        const copied = cell.children[0].props.onClick({stopPropagation: () => { stopped = true; }});
        assert.deepEqual(state.legacy, [row.rfq_copy_text]);
        assert.equal(stopped, true);
        assert.deepEqual(events, []);
        assert.equal(await copied, true);
        assert.equal(state.mounted.length, 0);
        assert.equal(state.focusRestored, 1);
        cell.children[1].props.onClick({stopPropagation() {}});
        assert.deepEqual(events, [{action: "close", rfq_id: 42}]);

        for (const quoteType of ["Mkt Color", " MKT COLOR ", "Trade", "Live", "", null]) {
            const gwmEvents = [];
            const gwmCell = window.dashAgGridComponentFunctions.RfqActionsCell({
                data: {...row, quote_format: quoteType}, mktColorOnly: true,
                setData: value => gwmEvents.push(value),
            });
            assert.equal(gwmCell.children[1].type, "span");
            assert.equal(gwmCell.children[1].children[0], "✓");
            assert.equal(gwmCell.children[0].props.title, "Copy RFQ");
            assert.equal(await gwmCell.children[0].props.onClick({stopPropagation() {}}), true);
            gwmCell.children[1].props.onClick({stopPropagation() {}});
            const allowed = String(quoteType || "").trim().toLowerCase() === "mkt color";
            assert.deepEqual(gwmEvents, allowed ? [{action: "close", rfq_id: 42}] : []);
            assert.equal(gwmCell.children[1].props.title, allowed ? "Close RFQ" :
                "GWM users can conclude Mkt Color RFQs only");
        }

        reset();
        window.isSecureContext = true;
        window.navigator.clipboard = {writeText: text => { state.modern.push(text); return Promise.resolve(); }};
        assert.equal(await rfq.copyTradeToClipboard(row), true);
        assert.deepEqual(state.modern, [row.rfq_copy_text]);
        assert.deepEqual(state.legacy, []);

        reset();
        window.isSecureContext = true;
        window.navigator.clipboard = {writeText: () => Promise.reject(new Error("NotAllowedError"))};
        assert.equal(await rfq.copyTradeToClipboard(row), true);
        assert.deepEqual(state.legacy, [row.rfq_copy_text]);

        reset();
        window.isSecureContext = true;
        window.navigator.clipboard = {writeText: () => { throw new Error("Permission denied"); }};
        assert.equal(await rfq.copyTradeToClipboard(row), true);
        assert.deepEqual(state.legacy, [row.rfq_copy_text]);

        reset();
        state.legacyResult = false;
        assert.equal(await rfq.copyTradeToClipboard(row), false);
        assert.equal(state.prompts[0].text, row.rfq_copy_text);
        assert.equal(state.mounted.length, 0);

        reset();
        state.legacyError = true;
        assert.equal(await rfq.copyTradeToClipboard(row), false);
        assert.equal(state.prompts[0].text, row.rfq_copy_text);
        assert.equal(state.mounted.length, 0);

        reset();
        assert.equal(await rfq.copyTradeToClipboard(null), false);
        assert.deepEqual(state.legacy, []);
        assert.deepEqual(state.prompts, []);

        reset();
        const columns = [{field: "id", hide: true}, {headerName: "Security", field: "security"},
                         {headerName: "Best", field: "best_quote"}, {field: "actions"}];
        const data = [{security: "Test bond", best_quote: "6.40|10:02"}];
        const gridCopy = rfq.copyGridToClipboard(1, data, columns);
        assert.deepEqual(state.legacy, ["Security\tBest\r\nTest bond\t6.40 10:02"]);
        assert.equal((await gridCopy).copied, true);
        state.legacyResult = false;
        assert.equal((await rfq.copyGridToClipboard(2, data, columns)).copied, false);
        assert.equal(rfq.copyGridToClipboard(0, data, columns), window.dash_clientside.no_update);

        // Poll responses must not interrupt typing or arrive in a different view.
        const noUpdate = window.dash_clientside.no_update;
        const unchanged = [noUpdate, noUpdate];
        const user = {role: "gwm", user: "Reader"};
        const editEvent = {newValue: "7.5", data: {rfq_id: 42}};
        const snapshot = {rows: data, columns: columns,
            view: ["sovereign", true, {user: "Reader", role: "gwm"}, 0,
                   {data: {rfq_id: 42}, newValue: "7.5"}]};
        const apply = (patch = {}) => rfq.applyLiveSnapshot(
            patch.snapshot === undefined ? snapshot : patch.snapshot,
            patch.tab || "sovereign", patch.showActive === undefined ? true : patch.showActive,
            patch.user || user, patch.refresh || 0, patch.editEvent || editEvent,
            patch.rows || [], patch.columns || columns);
        window.dash_ag_grid = {getApi: id => {
            assert.equal(id, "rfq-grid");
            return {getEditingCells: () => []};
        }};
        assert.deepEqual(apply(), [data, noUpdate]);
        assert.deepEqual(apply({rows: data}), unchanged);
        assert.deepEqual(apply({columns: []}), [data, columns]);
        assert.deepEqual(apply({snapshot: null}), unchanged);
        assert.deepEqual(apply({tab: "corporate"}), unchanged);
        assert.deepEqual(apply({showActive: false}), unchanged);
        assert.deepEqual(apply({user: {user: "Other user", role: "master"}}), unchanged);
        assert.deepEqual(apply({refresh: 1}), unchanged);
        assert.deepEqual(apply({editEvent: {newValue: "8.5"}}), unchanged);
        window.dash_ag_grid.getApi = () => ({getEditingCells: () => [{rowIndex: 0}]});
        assert.deepEqual(apply(), unchanged);
        window.dash_ag_grid.getApi = () => null;
        assert.deepEqual(apply(), unchanged);
        window.dash_ag_grid.getApi = () => { throw new Error("Grid destroyed"); };
        assert.deepEqual(apply(), unchanged);
        return "RFQ browser checks passed: clipboard fallbacks, profile actions, live updates, unchanged data, active editing, stale responses, and page changes.";
    } finally {
        for (const [key, value] of Object.entries(originals)) {
            if (value === undefined) delete globalThis[key];
            else globalThis[key] = value;
        }
    }
}
