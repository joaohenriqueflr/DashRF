(function () {
    var dc = (window.dash_clientside = window.dash_clientside || {});
    var controls = (dc.formControls = dc.formControls || {});

    // Keep function names stable; Python supplies form-specific settings as data.
    controls.countText = function (value, config) {
        if (!config) {
            return [dc.no_update, dc.no_update];
        }
        var counter = config.counter;
        var count = (value || "").length;
        var state = count > counter.limit ? counter.overClass :
            count === counter.limit ? counter.limitClass : "";
        return [count + " / " + counter.limit, counter.className + state];
    };

    controls.presentAIState = function (result, busy, config) {
        if (!config) {
            return [dc.no_update, dc.no_update, dc.no_update];
        }
        return config.stages[busy === "true" ? "running" :
            result && result.review ? "review" : "idle"].slice();
    };

    controls.resetAIForm = function (_clicks, opened, config) {
        if (!config) {
            return dc.no_update;
        }
        var reset = config.reset;
        var changed = (dc.callback_context && dc.callback_context.triggered) || [];
        var opening = opened && changed.some(function (item) {
            return item.prop_id === reset.openedProp;
        });
        var applying = changed.some(function (item) {
            return item.prop_id === reset.applyProp;
        });
        if (!opening && !applying) {
            return Array(reset.values.length + 1).fill(dc.no_update);
        }
        var values = reset.values.slice();
        values.push(opening ? "" : dc.no_update);
        return values;
    };
})();
