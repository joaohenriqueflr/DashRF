var dagcomponentfuncs = window.dashAgGridComponentFunctions || {};

dagcomponentfuncs.SourceBadge = function (props) {
    var value = props.value ? props.value.toString().toLowerCase() : "";
    if (!value) {
        return null;
    }
    return React.createElement(
        "span",
        { className: "source-badge source-" + value },
        value
    );
};

dagcomponentfuncs.SideBadge = function (props) {
    var value = props.value ? props.value.toString().toLowerCase() : "";
    if (!value) {
        return null;
    }
    var label = value === "ask" ? "OFFER" : value.toUpperCase();
    return React.createElement(
        "span",
        { className: "type-badge type-" + value },
        label
    );
};

window.dashAgGridComponentFunctions = dagcomponentfuncs;

window.dashMantineFunctions = window.dashMantineFunctions || {};
window.dashMantineFunctions.formatExposure = function (value) {
    if (value === null || value === undefined || Number.isNaN(value)) {
        return "";
    }
    return "R$ " + Number(value).toFixed(2) + "B";
};

dagcomponentfuncs.BondTypeBadge = function (props) {
    if (!props.value) {
        return null;
    }
    return React.createElement(
        "span",
        { className: "portfolio-badge" },
        props.value
    );
};

dagcomponentfuncs.RatingBadge = function (props) {
    if (!props.value) {
        return null;
    }
    var value = props.value.toString();
    var ratingClassMap = {
        "AAA": "aaa",
        "AA+": "aa-plus",
        "AA": "aa",
        "AA-": "aa-minus",
        "A+": "a-plus",
        "A": "a",
        "A-": "a-minus",
        "BBB+": "bbb-plus",
        "BBB": "bbb",
        "Unrated": "unrated"
    };
    var normalizedValue =
        ratingClassMap[value] || value.toString().toLowerCase().replace(/\s+/g, "-");
    var className =
        "portfolio-badge rating-" + normalizedValue;
    return React.createElement("span", { className: className }, value);
};

dagcomponentfuncs.CashflowEventBadge = function (props) {
    if (!props.value) {
        return null;
    }
    var value = props.value.toString();
    var slug = value.toLowerCase().replace(/\s+/g, "-");
    return React.createElement(
        "span",
        { className: "portfolio-cashflow-badge " + slug },
        value
    );
};

dagcomponentfuncs.RfqDirectionBadge = function (props) {
    if (!props.value) {
        return null;
    }
    var value = props.value.toString().toLowerCase();
    return React.createElement(
        "span",
        { className: "rfq-badge rfq-" + value },
        props.value
    );
};

dagcomponentfuncs.RfqQuoteFormatBadge = function (props) {
    if (!props.value) {
        return null;
    }
    var value = props.value.toString();
    var slug = value.toLowerCase().replace(/\s+/g, "-");
    var className = "rfq-format-badge";
    if (slug === "trade" || slug === "mkt-color") {
        className += " rfq-format-" + slug;
    }
    return React.createElement("span", { className: className }, value);
};

dagcomponentfuncs.RfqQuoteCell = function (props) {
    if (!props.value || props.value === "--") {
        return props.value || null;
    }
    var parts = props.value.toString().split("|");
    var quote = parts[0] || "";
    var time = parts[1] || "";
    return React.createElement(
        "div",
        { className: "rfq-quote-stack" },
        React.createElement("span", { className: "rfq-quote-main" }, quote),
        time
            ? React.createElement(
                  "span",
                  { className: "rfq-quote-time" },
                  time
              )
            : null
    );
};

dagcomponentfuncs.rfqParseQuoteValue = function (value) {
    if (value === null || value === undefined) {
        return NaN;
    }
    var text = value.toString();
    if (!text || text === "--") {
        return NaN;
    }
    var quote = text.split("|")[0] || "";
    var cleaned = quote.replace(/,/g, "").trim();
    if (!cleaned) {
        return NaN;
    }
    var parsed = parseFloat(cleaned);
    return Number.isFinite(parsed) ? parsed : NaN;
};

dagcomponentfuncs.rfqParseNumber = function (value) {
    if (value === null || value === undefined) {
        return NaN;
    }
    if (typeof value === "number") {
        return Number.isFinite(value) ? value : NaN;
    }
    var cleaned = value.toString().replace(/,/g, "").trim();
    if (!cleaned) {
        return NaN;
    }
    var parsed = parseFloat(cleaned);
    return Number.isFinite(parsed) ? parsed : NaN;
};

dagcomponentfuncs.RfqQuoteHighlight = function (params) {
    if (!params || !params.data) {
        return false;
    }
    var direction = (params.data.direction || "").toString().toLowerCase();
    if (direction !== "buy" && direction !== "sell") {
        return false;
    }
    var keys = Object.keys(params.data).filter(function (key) {
        return key.indexOf("cp_") === 0;
    });
    if (!keys.length) {
        return false;
    }
    var values = [];
    keys.forEach(function (key) {
        var value = dagcomponentfuncs.rfqParseQuoteValue(params.data[key]);
        if (Number.isFinite(value)) {
            values.push(value);
        }
    });
    if (!values.length) {
        return false;
    }
    var target =
        direction === "buy"
            ? Math.max.apply(Math, values)
            : Math.min.apply(Math, values);
    var current = dagcomponentfuncs.rfqParseQuoteValue(params.value);
    if (!Number.isFinite(current)) {
        return false;
    }
    return Math.abs(current - target) < 1e-9;
};

dagcomponentfuncs.RfqLimitHighlight = function (params) {
    if (!params || !params.data) {
        return false;
    }
    var direction = (params.data.direction || "").toString().toLowerCase();
    if (direction !== "buy" && direction !== "sell") {
        return false;
    }
    var limitValue = dagcomponentfuncs.rfqParseQuoteValue(params.value);
    if (!Number.isFinite(limitValue)) {
        return false;
    }
    var keys = Object.keys(params.data).filter(function (key) {
        return key.indexOf("cp_") === 0;
    });
    if (!keys.length) {
        return false;
    }
    var values = [];
    keys.forEach(function (key) {
        var value = dagcomponentfuncs.rfqParseQuoteValue(params.data[key]);
        if (Number.isFinite(value)) {
            values.push(value);
        }
    });
    if (!values.length) {
        return false;
    }
    var minValue = Math.min.apply(Math, values);
    var maxValue = Math.max.apply(Math, values);
    if (direction === "sell") {
        return limitValue < minValue;
    }
    return limitValue > maxValue;
};

window.dashAgGridFunctions = window.dashAgGridFunctions || {};
window.dashAgGridFunctions.rfqQuoteHighlight = function (params) {
    return dagcomponentfuncs.RfqQuoteHighlight(params);
};
window.dashAgGridFunctions.rfqLimitHighlight = function (params) {
    return dagcomponentfuncs.RfqLimitHighlight(params);
};

dagcomponentfuncs.RfqActionsCell = function (props) {
    return React.createElement(
        "div",
        { className: "rfq-actions" },
        React.createElement(
            "span",
            {
                className: "rfq-action neutral",
                title: "Copy RFQ",
                onClick: function (event) {
                    event.stopPropagation();
                    if (props && typeof props.setData === "function") {
                        props.setData({
                            action: "copy",
                            rfq_id: props.data ? props.data.rfq_id : null,
                        });
                    }
                },
            },
            "⧉"
        ),
        React.createElement(
            "span",
            {
                className: "rfq-action success",
                title: "Close RFQ",
                onClick: function (event) {
                    event.stopPropagation();
                    if (props && typeof props.setData === "function") {
                        props.setData({
                            action: "close",
                            rfq_id: props.data ? props.data.rfq_id : null,
                        });
                    }
                },
            },
            "✓"
        )
    );
};
