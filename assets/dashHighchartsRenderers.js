(function () {
    var dc = (window.dash_clientside = window.dash_clientside || {});
    dc.highcharts = dc.highcharts || {};
    var pendingRenders = {};
    var retryTimer = null;
    var retryCount = 0;

    function parseOptions(payload) {
        if (!payload) {
            return null;
        }
        if (typeof payload === "string") {
            try {
                return JSON.parse(payload);
            } catch (e) {
                return null;
            }
        }
        return payload;
    }

    function flushPendingRenders() {
        var containerIds = Object.keys(pendingRenders);
        if (!containerIds.length) {
            return;
        }
        for (var i = 0; i < containerIds.length; i += 1) {
            var containerId = containerIds[i];
            var options = pendingRenders[containerId];
            if (options) {
                renderSpecificChart(containerId, options);
            }
            delete pendingRenders[containerId];
        }
    }

    function scheduleRetry() {
        if (retryTimer) {
            return;
        }
        retryTimer = window.setInterval(function () {
            retryCount += 1;
            flushPendingRenders();
            if (!Object.keys(pendingRenders).length) {
                window.clearInterval(retryTimer);
                retryTimer = null;
                retryCount = 0;
                return;
            }
            if (retryCount >= 60) {
                window.clearInterval(retryTimer);
                retryTimer = null;
                retryCount = 0;
            }
        }, 500);
    }

    function isSeriesTypeReady(options) {
        if (!window.Highcharts || !window.Highcharts.seriesTypes) {
            return false;
        }
        var chartType = options && options.chart && options.chart.type;
        if (chartType && !window.Highcharts.seriesTypes[chartType]) {
            return false;
        }
        var series = (options && options.series) || [];
        for (var i = 0; i < series.length; i += 1) {
            var seriesType = series[i] && series[i].type;
            if (seriesType && !window.Highcharts.seriesTypes[seriesType]) {
                return false;
            }
        }
        return true;
    }

    function renderSpecificChart(containerId, options) {
        if (!options) {
            return;
        }
        var container = document.getElementById(containerId);
        if (!container) {
            return;
        }
        if (!isSeriesTypeReady(options)) {
            pendingRenders[containerId] = options;
            scheduleRetry();
            return;
        }
        try {
            window.Highcharts.chart(container, options);
        } catch (e) {
            // Keep UI responsive even if one chart fails.
        }
    }

    dc.highcharts.renderSpreadVolumeChart = function (optionsJson) {
        var options = parseOptions(optionsJson);
        if (!options) {
            return window.dash_clientside.no_update;
        }
        renderSpecificChart("spread-volume-chart", options);
        return window.dash_clientside.no_update;
    };

    dc.highcharts.renderPortfolioExposureChart = function (optionsJson) {
        var payload = parseOptions(optionsJson);
        if (!payload) {
            return window.dash_clientside.no_update;
        }
        renderSpecificChart("exposure-by-issuer-chart", payload.exposure);
        return window.dash_clientside.no_update;
    };

    dc.highcharts.renderPortfolioDurationFixedChart = function (optionsJson) {
        var payload = parseOptions(optionsJson);
        if (!payload) {
            return window.dash_clientside.no_update;
        }
        var duration = payload.duration || {};
        renderSpecificChart("duration-fixed-chart", duration.fixed);
        return window.dash_clientside.no_update;
    };

    dc.highcharts.renderPortfolioDurationFloatingChart = function (optionsJson) {
        var payload = parseOptions(optionsJson);
        if (!payload) {
            return window.dash_clientside.no_update;
        }
        var duration = payload.duration || {};
        renderSpecificChart("duration-floating-chart", duration.floating);
        return window.dash_clientside.no_update;
    };

    dc.highcharts.renderPortfolioDurationIpcaChart = function (optionsJson) {
        var payload = parseOptions(optionsJson);
        if (!payload) {
            return window.dash_clientside.no_update;
        }
        var duration = payload.duration || {};
        renderSpecificChart("duration-ipca-chart", duration.inflation);
        return window.dash_clientside.no_update;
    };

    dc.highcharts.renderPortfolioInstrumentTypeChart = function (optionsJson) {
        var payload = parseOptions(optionsJson);
        if (!payload) {
            return window.dash_clientside.no_update;
        }
        renderSpecificChart(
            "exposure-by-instrument-type-chart",
            payload.instrument_type
        );
        return window.dash_clientside.no_update;
    };

    dc.highcharts.renderPortfolioForwardCashFlowChart = function (optionsJson) {
        var payload = parseOptions(optionsJson);
        if (!payload) {
            return window.dash_clientside.no_update;
        }
        renderSpecificChart("forward-cashflow-chart", payload.forward_cashflow);
        return window.dash_clientside.no_update;
    };
})();
