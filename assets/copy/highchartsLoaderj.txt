(function () {
    var mirrors = [
        {
            core: "https://code.highcharts.com/highcharts.js",
            more: "https://code.highcharts.com/highcharts-more.js",
            treemap: "https://code.highcharts.com/modules/treemap.js"
        },
        {
            core: "https://cdn.jsdelivr.net/npm/highcharts@12/highcharts.js",
            more: "https://cdn.jsdelivr.net/npm/highcharts@12/highcharts-more.js",
            treemap: "https://cdn.jsdelivr.net/npm/highcharts@12/modules/treemap.js"
        },
        {
            core: "https://unpkg.com/highcharts@12/highcharts.js",
            more: "https://unpkg.com/highcharts@12/highcharts-more.js",
            treemap: "https://unpkg.com/highcharts@12/modules/treemap.js"
        },
        {
            core: "https://cdnjs.cloudflare.com/ajax/libs/highcharts/12.4.0/highcharts.js",
            more: "https://cdnjs.cloudflare.com/ajax/libs/highcharts/12.4.0/highcharts-more.js",
            treemap: "https://cdnjs.cloudflare.com/ajax/libs/highcharts/12.4.0/modules/treemap.js"
        }
    ];

    function loadScript(url, onSuccess, onError) {
        var script = document.createElement("script");
        script.src = url;
        script.async = true;
        script.onload = onSuccess;
        script.onerror = onError;
        document.head.appendChild(script);
    }

    function hasModule(moduleName) {
        return (
            window.Highcharts &&
            window.Highcharts.seriesTypes &&
            window.Highcharts.seriesTypes[moduleName]
        );
    }

    function ensureModules(index) {
        if (index >= mirrors.length || !window.Highcharts) {
            return;
        }

        var mirror = mirrors[index];

        function ensureTreemap() {
            if (hasModule("treemap")) {
                return;
            }
            loadScript(
                mirror.treemap,
                function () {},
                function () {
                    ensureModules(index + 1);
                }
            );
        }

        if (hasModule("bubble")) {
            ensureTreemap();
            return;
        }

        loadScript(
            mirror.more,
            function () {
                ensureTreemap();
            },
            function () {
                ensureTreemap();
            }
        );
    }

    function tryMirror(index) {
        if (index >= mirrors.length) {
            return;
        }

        if (window.Highcharts) {
            ensureModules(index);
            return;
        }

        var mirror = mirrors[index];
        loadScript(
            mirror.core,
            function () {
                ensureModules(index);
            },
            function () {
                tryMirror(index + 1);
            }
        );
    }

    tryMirror(0);
})();
