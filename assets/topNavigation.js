(function () {
    function initializeNavigation() {
        var nav = document.querySelector(".top-nav");
        if (!nav || nav.dataset.dropdownsReady) return;
        nav.dataset.dropdownsReady = "true";
        var groups = Array.from(nav.querySelectorAll(".nav-group"));

        function setOpen(group, open) {
            if (open) {
                groups.forEach(function (other) {
                    if (other !== group) setOpen(other, false);
                });
            }
            group.classList.toggle("is-open", open);
            group.querySelector(".nav-group-trigger").setAttribute("aria-expanded", String(open));
        }

        groups.forEach(function (group) {
            var trigger = group.querySelector(".nav-group-trigger");
            var links = Array.from(group.querySelectorAll(".nav-submenu-link"));

            group.addEventListener("pointerenter", function (event) {
                if (event.pointerType !== "touch") setOpen(group, true);
            });
            group.addEventListener("pointerleave", function () {
                if (!group.contains(document.activeElement)) setOpen(group, false);
            });
            trigger.addEventListener("click", function () {
                setOpen(group, !group.classList.contains("is-open"));
            });
            group.addEventListener("focusout", function (event) {
                if (!group.contains(event.relatedTarget)) setOpen(group, false);
            });
            group.addEventListener("keydown", function (event) {
                if (event.key === "Escape") {
                    trigger.focus();
                    setOpen(group, false);
                    event.preventDefault();
                } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                    event.preventDefault();
                    setOpen(group, true);
                    var index = links.indexOf(document.activeElement);
                    var next = index < 0 ? (event.key === "ArrowDown" ? 0 : links.length - 1) :
                        (index + (event.key === "ArrowDown" ? 1 : -1) + links.length) % links.length;
                    links[next].focus();
                }
            });
            links.forEach(function (link) {
                link.addEventListener("click", function () { setOpen(group, false); });
            });
        });

        document.addEventListener("click", function (event) {
            groups.forEach(function (group) {
                if (!group.contains(event.target)) setOpen(group, false);
            });
        });
    }

    // Dash mounts its layout after assets load and may replace it during hot reload.
    new MutationObserver(initializeNavigation).observe(document.documentElement, {
        childList: true,
        subtree: true
    });
    initializeNavigation();
})();
