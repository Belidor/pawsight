// Page navigation shared by every page: a Back button and one tab per page.
//
// The desktop app shows these pages in its own window, which has no browser
// toolbar: without a Back button and visible tabs, a page such as /setup was a
// dead end. The same bar is shown in a browser, so both look alike.
//
// Included with <script src="/static/nav.js" defer></script>; it adds the bar
// as the last row of the page's <header>. Styles live here too, because
// setup.html has its own inline CSS and does not load dashboard.css.
(function () {
  "use strict";

  // "/?dashboard=1": on a new install "/" sends you to the setup page,
  // and this tab must always lead to the live view.
  var TABS = [
    { path: "/", href: "/?dashboard=1", label: "Live view",
      title: "camera, what the dog is doing, drinks and meals" },
    { path: "/label", href: "/label", label: "Labelling",
      title: "review camera frames and draw boxes for fine-tuning" },
    { path: "/zones", href: "/zones", label: "Zones",
      title: "draw zones and choose which send Telegram notifications" },
    { path: "/setup", href: "/setup", label: "Setup",
      title: "camera address, Telegram, water bowl choice" },
  ];
  var HOME = "/?dashboard=1";

  var CSS = [
    ".appnav{flex-basis:100%; order:99; display:flex; align-items:center; gap:8px;",
    "  flex-wrap:wrap; margin:10px 0 4px; padding:6px; background:var(--panel,#1D1A15);",
    "  border:1px solid var(--edge,#332C21); border-radius:10px}",
    ".appnav a,.appnav button{font:inherit; font-size:15px; font-weight:600; line-height:1;",
    "  color:var(--muted,#A2988A); text-decoration:none; border:1px solid transparent;",
    "  background:none; border-radius:7px; padding:10px 16px; cursor:pointer;",
    "  display:inline-flex; align-items:center; gap:8px}",
    ".appnav a:hover,.appnav button:hover:not(:disabled){color:var(--ink,#EDE6D9);",
    "  background:var(--panel-2,#262119)}",
    ".appnav a[aria-current=page]{color:#14120E; background:var(--accent,#E0A063)}",
    ".appnav .back{color:var(--ink,#EDE6D9); border-color:var(--edge,#332C21);",
    "  background:var(--panel-2,#262119); margin-right:6px}",
    ".appnav .back:disabled{opacity:.4; cursor:default}",
    ".appnav .sep{width:1px; align-self:stretch; background:var(--edge,#332C21); margin:2px 4px}",
    ".appnav a:focus-visible,.appnav button:focus-visible{outline:2px solid var(--accent,#E0A063);",
    "  outline-offset:2px}",
  ].join("\n");

  function current() {
    var p = location.pathname.replace(/\/+$/, "") || "/";
    for (var i = 0; i < TABS.length; i++) if (TABS[i].path === p) return TABS[i];
    return null;
  }

  // Back goes to the previous page of this dashboard when there is one;
  // otherwise (opened directly, e.g. the app's first window) to the live view.
  function canGoBack() {
    return history.length > 1 && document.referrer.indexOf(location.origin + "/") === 0;
  }
  function goBack() {
    if (canGoBack()) history.back();
    else if (current() !== TABS[0]) location.href = HOME;
  }

  function build() {
    var header = document.querySelector("header");
    if (!header || header.querySelector(".appnav")) return;

    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);

    var nav = document.createElement("nav");
    nav.className = "appnav";
    nav.setAttribute("aria-label", "pages");

    var back = document.createElement("button");
    back.type = "button";
    back.className = "back";
    back.textContent = "← Back";
    back.title = "previous page (⌘[ or Alt+←)";
    back.disabled = !canGoBack() && current() === TABS[0];
    back.addEventListener("click", goBack);
    nav.appendChild(back);

    var sep = document.createElement("span");
    sep.className = "sep";
    nav.appendChild(sep);

    var here = current();
    TABS.forEach(function (t) {
      var a = document.createElement("a");
      a.href = t.href;
      a.textContent = t.label;
      a.title = t.title;
      if (t === here) a.setAttribute("aria-current", "page");
      nav.appendChild(a);
    });
    header.appendChild(nav);
  }

  // Cmd+[ (macOS) and Alt+Left: the usual "back" keys, which the app window
  // does not provide by itself. Not while typing in a field.
  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if ((e.metaKey && e.key === "[") || (e.altKey && e.key === "ArrowLeft")) {
      e.preventDefault();
      goBack();
    }
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
