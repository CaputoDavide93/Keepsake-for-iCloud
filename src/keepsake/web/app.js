/* Keepsake for iCloud: the web page. No framework, no external requests.
   Everything shown from the server goes through textContent, never innerHTML. */
(function () {
  "use strict";

  var view = document.getElementById("view");
  var state = { csrf: "", authRequired: false, authed: false, status: null, timer: null, keyDown: false, keyAt: 0, inputAt: 0, pollToken: 0, stops: [] };
  // Unsaved edits on the Settings page, kept in memory while the person moves between its
  // tabs or presses a button that redraws the page. Dropped when they leave Settings.
  var settingsDraft = null;
  // The page redraws every few seconds; never in the middle of a key press, or a
  // Space that went down on a button would come up on its replacement and do nothing.
  // A key-up can be lost (a Cmd-Shift-4 screenshot, Cmd-Tab, a dialog taking focus), and
  // a stuck flag would freeze every redraw for good: the preview would finish on the
  // server and the page would still say "Reading". So the flag expires, and it is
  // cleared whenever the window loses focus or the tab is hidden.
  var KEY_HOLD_MS = 2000, INPUT_QUIET_MS = 1500;
  // "Busy" also covers the moment just after the person did something (focused a
  // control, pressed a key or a button): a redraw then would swallow what comes next.
  function keyBusy() { return (state.keyDown && Date.now() - state.keyAt < KEY_HOLD_MS) || Date.now() - state.inputAt < INPUT_QUIET_MS; }
  ["focusin", "pointerdown"].forEach(function (type) { document.addEventListener(type, function () { state.inputAt = Date.now(); }, true); });
  document.addEventListener("keydown", function () { state.keyDown = true; state.keyAt = state.inputAt = Date.now(); }, true);
  document.addEventListener("keyup", function () { state.keyDown = false; }, true);
  window.addEventListener("blur", function () { state.keyDown = false; });
  document.addEventListener("visibilitychange", function () { state.keyDown = false; });

  // ---- small helpers -------------------------------------------------------
  function h(tag, attrs) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === false || v === null || v === undefined) return;
      if (k === "class") el.className = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
      else el.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(function (x) { add(el, x); });
    else el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
  function api(method, path, body) {
    var opts = { method: method, credentials: "same-origin", headers: {} };
    if (method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-CSRF-Token"] = state.csrf;
      opts.body = JSON.stringify(body || {});
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.status === 401 && state.authRequired && state.authed && path !== "/api/login") { state.authed = false; route(); }
        if (!r.ok) { var e = new Error(data.message || ("request failed (" + r.status + ")")); e.status = r.status; e.data = data; throw e; }
        return data;
      });
    });
  }
  function fmtBytes(n) {
    if (!n) return "0 B";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + " " + u[i];
  }
  function rel(iso) {
    if (!iso) return "never";
    var d = new Date(iso), s = Math.round((d - Date.now()) / 1000);
    var f = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    var a = Math.abs(s);
    if (a < 60) return f.format(s, "second");
    if (a < 3600) return f.format(Math.round(s / 60), "minute");
    if (a < 86400) return f.format(Math.round(s / 3600), "hour");
    return f.format(Math.round(s / 86400), "day");
  }
  // "Fri 18:02" within the last week, "12 Oct, 18:02" after that.
  function when(iso) {
    var d = new Date(iso), old = Date.now() - d > 6 * 86400000;
    // 24-hour time everywhere: "Tue 14:24", never "Tue 02:24 PM", whatever the browser's language.
    return d.toLocaleString(undefined, old ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" } : { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  }
  function checkBad(j) { return !!(j.verify && j.verify.state === "mismatch"); }
  function checkLine(j) {
    return j.verify ? j.verify.message + ", " + when(j.verify.at)
      : "Not checked yet.";
  }
  var AUTO = [["off", "Off"], ["daily", "Daily"], ["weekly", "Weekly"], ["monthly", "Monthly"]];
  // "Next check Fri 03:00", "Check due now", "Waits for the first copy", "Automatic check off".
  function nextCheckText(j) {
    if (j.auto_check === "off") return "Automatic check off";
    if (!j.enabled) return "Paused";
    if (!j.next_check) return "Waits for the first copy";
    return new Date(j.next_check) <= new Date() ? "Check due now" : "Next check " + when(j.next_check);
  }
  // How a rotating check covers everything, in numbers the server worked out.
  function coverageText(j) {
    var n = j.check_cycle_checks;
    return n ? "Every file has been looked at after about " + n + (n === 1 ? " check" : " checks") + " (more if some are skipped)."
      : "Coverage is worked out after the first copy.";
  }
  function autoSelect(id, value) {
    return h("select", { id: id }, AUTO.map(function (o) { return h("option", { value: o[0], selected: o[0] === value }, o[1]); }));
  }
  function showErr(j) { return !!j.error && j.status !== "ok" && !(checkBad(j) && j.result === "ok"); }
  // What the last run changed: one sentence, and up to 50 example paths to open.
  function changesBlock(j) {
    var c = j.changes;
    if (!c || !c.summary) return null;
    var list = (c.samples || []).map(function (x) { return h("li", null, x.kind + ": " + x.path); });
    var total = c.added + c.updated + c.removed;
    return h("div", null, [h("p", { class: "hint" }, "Last run: " + c.summary + "."),
      list.length ? h("details", null, [h("summary", null, "Show " + (total > list.length ? "the first " + list.length : "the") + " changed files"), h("ul", { class: "changes" }, list)]) : null]);
  }
  function absTime(iso) { return iso ? new Date(iso).toLocaleString() : ""; }
  function toast(msg) {
    var t = document.getElementById("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast.t);
    toast.t = setTimeout(function () { t.hidden = true; }, 5000);
  }
  function fail(e) { toast(e.message || "something went wrong"); }
  function slug(s) {
    return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "folder";
  }
  // Camel Case folder name from anything typed or found: "photo library" -> "PhotoLibrary".
  function camel(s) {
    var words = String(s).split(/[^A-Za-z0-9]+/).filter(Boolean);
    return words.map(function (w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join("").slice(0, 40) || "Folder";
  }
  // The readable label for a name: HomeFolder -> Home Folder, photo-library -> Photo Library.
  // The real name is what is on disk and in the API; show it too where it matters.
  function displayName(name) {
    return String(name).replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
      .replace(/[-_]+/g, " ").replace(/\s+/g, " ").trim().replace(/(^|\s)([a-z])/g, function (m, a, b) { return a + b.toUpperCase(); });
  }
  function nameLabel(name) { var d = displayName(name); return d === name ? d : d + " (" + name + ")"; }
  var calm = matchMedia("(prefers-reduced-motion: reduce)");
  // A dialog grows out of the control that opened it and goes back the same way.
  function openSheet(dlg, trigger) {
    dlg.showModal();
    if (calm.matches || !dlg.animate) return;
    var r = dlg.getBoundingClientRect(), t = trigger && trigger.getBoundingClientRect ? trigger.getBoundingClientRect() : null;
    var origin = t ? (t.left + t.width / 2 - r.left) + "px " + (t.top + t.height / 2 - r.top) + "px" : "50% 50%";
    dlg.animate([{ opacity: 0, transform: "scale(.92)", transformOrigin: origin }, { opacity: 1, transform: "scale(1)", transformOrigin: origin }],
      { duration: 320, easing: "cubic-bezier(.32,.72,0,1)" });
  }
  function closeSheet(dlg, value) {
    if (calm.matches || !dlg.animate) { dlg.close(value); return; }
    dlg.animate([{ opacity: 1, transform: "scale(1)" }, { opacity: 0, transform: "scale(.97)" }], { duration: 140, easing: "cubic-bezier(.32,.72,0,1)" })
      .onfinish = function () { dlg.close(value); };
  }
  // `alt` is an optional middle button {label, run}: the dialog closes, answers
  // "no" and then calls run (used to offer a Preview before a risky Sync).
  function confirmDialog(title, body, okLabel, danger, alt) {
    return new Promise(function (resolve) {
      var trigger = document.activeElement;
      var dlg = document.getElementById("dlg");
      while (dlg.firstChild) dlg.removeChild(dlg.firstChild);
      var extra = null;
      if (typeof body === "object" && body && body.nodeType) extra = body, body = "";
      add(dlg, [
        h("h2", { id: "dlg-title" }, title),
        h("p", null, body), extra,
        h("div", { class: "row end" }, [
          h("button", { type: "button", class: "btn", onclick: function () { closeSheet(dlg, "no"); } }, "Cancel"),
          alt ? h("button", { type: "button", class: "btn", onclick: function () { closeSheet(dlg, "alt"); } }, alt.label) : null,
          h("button", { type: "button", class: "btn " + (danger ? "danger" : "primary"), onclick: function () { closeSheet(dlg, "yes"); } }, okLabel)
        ])
      ]);
      dlg.returnValue = "";   // an earlier answer must never be read as this dialog's
      dlg.onclose = function () {
      // A close event is delivered after the dialog may already have been opened again (Enter right after Escape):
      // that event belongs to the earlier open, so the new one must not run its clean-up.
      if (dlg.open) return;
        var v = dlg.returnValue;
        resolve(v === "yes");
        if (v === "alt" && alt && alt.run) alt.run();
      };
      openSheet(dlg, trigger);
    });
  }
  // A read-only dialog with a Close button and optional extra buttons
  // [{label, primary, run}]. Resolves when it is closed.
  function infoDialog(title, nodes, buttons, returnFocus) {
    return new Promise(function (resolve) {
      var trigger = document.activeElement;
      var dlg = document.getElementById("dlg");
      while (dlg.firstChild) dlg.removeChild(dlg.firstChild);
      add(dlg, [h("h2", { id: "dlg-title" }, title), nodes,
        h("div", { class: "row end" }, [
          h("button", { type: "button", class: "btn", onclick: function () { closeSheet(dlg, "close"); } }, "Close")]
          .concat((buttons || []).map(function (b, i) {
            return h("button", { type: "button", class: "btn" + (b.primary ? " primary" : ""), onclick: function () { closeSheet(dlg, "b" + i); } }, b.label);
          })))]);
      dlg.classList.add("wide");
      dlg.returnValue = "";
      dlg.onclose = function () {
      // A close event is delivered after the dialog may already have been opened again (Enter right after Escape):
      // that event belongs to the earlier open, so the new one must not run its clean-up.
      if (dlg.open) return;
        dlg.classList.remove("wide");
        var v = dlg.returnValue, m = /^b(\d+)$/.exec(v);
        // The page redraws while the dialog is open, so the control that opened it may be gone.
        var back = returnFocus && document.querySelector('[data-focus="' + returnFocus + '"]');
        // A control inside a closed menu cannot take focus: its menu button does.
        if (back && back.closest('[role="menu"]')) back = back.closest('.menuwrap').querySelector('.morebtn');
        if (back && !back.disabled) back.focus({ preventScroll: true });
        resolve(v);
        if (m && buttons[+m[1]].run) buttons[+m[1]].run();
      };
      openSheet(dlg, trigger);
    });
  }
  function field(label, input, hint, id) {
    return h("div", { class: "field" }, [h("label", { for: id }, label), input, hint ? h("p", { class: "hint" }, hint) : null]);
  }
  function check(id, label, checked, hint) {
    return h("div", { class: "field" }, [
      h("label", { class: "check", for: id }, [h("input", { type: "checkbox", id: id, checked: checked }), h("span", null, label)]),
      hint ? h("p", { class: "hint" }, hint) : null]);
  }
  function setView(title, nodes) {
    while (view.firstChild) view.removeChild(view.firstChild);
    add(view, nodes);
    document.title = title + " \u00b7 Keepsake";
  }
  // "Back to X" above the heading of a page that sits under a menu section.
  function backLink(href, label) { return h("a", { class: "back", href: href }, "\u2190 " + label); }

  // ---- theme ---------------------------------------------------------------
  // Light, Dark or Auto. Auto removes data-theme so the page follows the system
  // (prefers-color-scheme). theme.js applies the saved choice before first paint.
  var themeButtons = document.querySelectorAll("#theme button");
  function currentChoice() {
    var t = document.documentElement.getAttribute("data-theme");
    return t === "light" || t === "dark" ? t : "auto";
  }
  function setTheme(t, save) {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else { t = "auto"; document.documentElement.removeAttribute("data-theme"); }
    themeButtons.forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.t === t)); });
    if (!save) return;
    try { if (t === "auto") localStorage.removeItem("keepsake-theme"); else localStorage.setItem("keepsake-theme", t); } catch (e) { /* storage blocked: the choice lasts until reload */ }
  }
  setTheme(currentChoice(), false);
  themeButtons.forEach(function (b) { b.addEventListener("click", function () { setTheme(b.dataset.t, true); }); });
  document.getElementById("logout").addEventListener("click", function () {
    api("POST", "/api/logout").then(function () { return boot(); }).catch(fail);
  });

  // ---- pages ---------------------------------------------------------------
  var STATUS = { ok: "Up to date", failed: "Needs attention", skipped: "Waiting", never: "Not run yet",
                 running: "Copying now", queued: "Queued", paused: "Paused" };
  var OUTCOME = { copied: "copied", nothing: "nothing new", paused: "paused or failed" };

  function pill(job) {
    return h("span", { class: "pill " + job.status }, STATUS[job.status] || job.status);
  }

  function loadStatus() { return api("GET", "/api/status").then(function (s) { state.status = s; renderBanner(); return s; }); }

  // A time in the server's time zone (TZ), so "until 08:00" is the 08:00 the schedules use.
  function whenIn(iso, tz) {
    var o = { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" };
    try { return new Date(iso).toLocaleString(undefined, Object.assign({ timeZone: tz }, o)); }
    catch (e) { return new Date(iso).toLocaleString(undefined, o); }
  }
  function pauseText(p) { return (p.kind === "quiet" ? "Quiet hours until " : "Paused until ") + whenIn(p.until, p.timezone); }
  function renderChip() {
    var chip = document.getElementById("pausechip"), p = state.status && state.status.pause;
    if (!p || !p.paused) { chip.hidden = true; return; }
    chip.textContent = pauseText(p);
    chip.setAttribute("title", p.message);
    chip.setAttribute("aria-label", pauseText(p) + ". " + p.message + " Open Settings.");
    chip.hidden = false;
  }

  function renderBanner() {
    renderChip();
    var b = document.getElementById("banner");
    while (b.firstChild) b.removeChild(b.firstChild);
    var s = state.status;
    if (!s) return;
    if (s.icloud_available && !s.anchor_ok) b.appendChild(h("div", { class: "notice" }, [
      h("p", { class: "mt0" }, "Nothing is copied until you confirm the iCloud folder. " + s.anchor_note), h("a", { class: "btn", href: "#/settings/locations" }, "Confirm in Settings")]));
    else if (!s.icloud_available) b.appendChild(h("div", { class: "notice bad" }, "The iCloud Drive folder is not available to Keepsake, so nothing can be copied. See Help, step 2."));
    else if (s.attention_count) b.appendChild(h("div", { class: "notice" }, s.attention_count + " folder" + (s.attention_count > 1 ? "s need" : " needs") + " attention: " + s.attention.map(function (a) { return a.job; }).join(", ")));
  }

  // The last 14 days of one folder, oldest first, ending today (the server's day).
  // Green: something was copied. Grey: ran, nothing new. Amber: paused, refused or
  // failed. An outlined bar: no run that day. A day is only green if a run finished.
  var NIGHTS = 14;
  function dayAdd(day, n) {
    var p = day.split("-"), d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2] + n));
    return d.toISOString().slice(0, 10);
  }
  function dayLabel(day) {
    var p = day.split("-");
    return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
  }
  function dayShort(day) {
    var p = day.split("-");
    return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])).toLocaleDateString(undefined, { day: "numeric", month: "short", timeZone: "UTC" });
  }
  // "12 added, 2 updated, 1 moved to .deleted": only the non-zero counts.
  function countsText(d) {
    var parts = [];
    if (d.added) parts.push(d.added + " added");
    if (d.updated) parts.push(d.updated + " updated");
    if (d.removed) parts.push(d.removed + " " + d.removed_how);
    return parts.join(", ");
  }
  // "Nov 6: copied, 12 added" is the bar's accessible name.
  function nightName(day, entry) {
    var o = entry && entry.outcome, known = o === "copied" || o === "nothing" || o === "paused";
    var what = known ? OUTCOME[o] : "no run";
    if (known && o === "copied" && entry.detail && countsText(entry.detail)) what += ", " + countsText(entry.detail);
    return dayShort(day) + ": " + what;
  }
  function nightKey(j, day) { return "night:" + j.name + ":" + day; }
  var nightLast = null;
  function openNight(j, day, entry, bar) {
    var dlg = document.getElementById("dlg"), o = entry && entry.outcome, d = entry && entry.detail;
    while (dlg.firstChild) dlg.removeChild(dlg.firstChild);
    var body = [];
    if (!entry) body.push(h("p", null, "Keepsake recorded no run for this folder on this day."));
    else {
      body.push(h("p", null, h("span", { class: "pill " + (o === "copied" ? "ok" : o === "paused" ? "paused" : "") }, o === "copied" ? "Copied" : o === "nothing" ? "Nothing new" : "Paused or failed")));
      if (!d) body.push(h("p", { class: "muted" }, "No detail recorded for this night. It was recorded before Keepsake kept what each night did, so only the outcome is known."));
      else {
        if (d.error) body.push(h("div", { class: "notice " + (o === "paused" ? "" : "bad") }, d.error));
        body.push(h("dl", { class: "counts" }, [
          h("dt", null, "Added"), h("dd", null, String(d.added)),
          h("dt", null, "Updated"), h("dd", null, String(d.updated)),
          h("dt", null, d.removed_how === "removed" ? "Removed" : "Moved to .deleted"), h("dd", null, String(d.removed))]));
        var total = d.added + d.updated + d.removed;
        if (d.samples.length) body.push(h("details", { open: true }, [
          h("summary", null, total > d.samples.length ? "First " + d.samples.length + " of " + total + " changed files" : "Changed files"),
          h("ul", { class: "changes", "aria-label": "Changed files on " + dayShort(day) }, d.samples.map(function (x) { return h("li", null, x.kind + ": " + x.path); }))]));
        else if (total) body.push(h("p", { class: "muted" }, "No example paths were recorded for this night."));
        var foot = [];
        if (d.at) foot.push("Last run that day: " + absTime(d.at) + ".");
        if (d.runs > 1) foot.push("The counts add up " + d.runs + " copying runs that day.");
        if (foot.length) body.push(h("p", { class: "hint" }, foot.join(" ")));
      }
    }
    add(dlg, [h("h2", { id: "dlg-title" }, j.name + ", " + dayLabel(day)), body,
      h("div", { class: "row end" }, h("button", { type: "button", class: "btn", id: "night-close", onclick: function () { closeSheet(dlg, "close"); } }, "Close"))]);
    nightLast = nightKey(j, day);
    dlg.classList.add("sheet");
    dlg.returnValue = "";
    dlg.onclick = function (e) { if (e.target === dlg) closeSheet(dlg, "close"); };
    dlg.onclose = function () {
      // A close event is delivered after the dialog may already have been opened again (Enter right after Escape):
      // that event belongs to the earlier open, so the new one must not run its clean-up.
      if (dlg.open) return;
      dlg.classList.remove("sheet");
      dlg.onclick = null;
      // The page may have redrawn since, so find the bar again by name.
      var el = document.querySelector('[data-focus="' + nightLast + '"]');
      if (el) el.focus({ preventScroll: true });
    };
    openSheet(dlg, bar);
  }
  function nightsStrip(j, today) {
    var seen = {}, counts = { copied: 0, nothing: 0, paused: 0, none: 0 };
    (j.history || []).forEach(function (e) { seen[e.day] = e; });
    var bars = [];
    for (var i = NIGHTS - 1; i >= 0; i--) {
      (function (day, entry) {
        var o = entry && entry.outcome;
        var known = o === "copied" || o === "nothing" || o === "paused";
        counts[known ? o : "none"]++;
        var name = nightName(day, entry);
        bars.push(h("button", { type: "button", class: "nb" + (!known ? " e" : o === "nothing" ? " n" : o === "paused" ? " w" : ""),
          "data-focus": nightKey(j, day), "data-day": day, tabindex: day === today ? "0" : "-1", "aria-label": name, title: name,
          onclick: function (e) { openNight(j, day, entry, e.currentTarget); },
          onkeydown: function (e) {
            var all = Array.prototype.slice.call(e.currentTarget.parentNode.children), at = all.indexOf(e.currentTarget);
            var to = e.key === "ArrowLeft" ? at - 1 : e.key === "ArrowRight" ? at + 1 : e.key === "Home" ? 0 : e.key === "End" ? all.length - 1 : -1;
            if (to < 0 || to >= all.length) return;
            e.preventDefault();
            all.forEach(function (b) { b.setAttribute("tabindex", "-1"); });
            all[to].setAttribute("tabindex", "0"); all[to].focus();
          } }));
      })(dayAdd(today, -i), seen[dayAdd(today, -i)]);
    }
    var label = "Last " + NIGHTS + " nights: " + counts.copied + " copied, " + counts.nothing + " nothing new, " + counts.paused + " paused or failed, " + counts.none + " without a run. Each night opens its detail";
    return h("div", { class: "strip", role: "group", "aria-label": label }, bars);
  }

  function shrank(j) { return j.status === "failed" && /shrank|moved or gone/.test(j.error); }
  function busyOf(j) { return j.status === "running" || j.status === "queued"; }
  function noteOf(j) { var n = (j.notes || []).filter(function (x) { return x.indexOf("plan:") !== 0; }); return n.length ? n[n.length - 1] : ""; }

  // The row's short forms. The exact values are in the title and in text for screen readers.
  // "18s ago", "55m ago", "3h ago", "2d ago".
  function shortAgo(iso) {
    var s = Math.max(0, Math.floor((Date.now() - new Date(iso)) / 1000));
    if (s < 10) return "just now";
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  // A short reason for a failure, shown as a tag; the whole sentence is under the row.
  function failTag(j) {
    var e = String(j.error || "").toLowerCase();
    if (/shrank|moved or gone/.test(e)) return "source shrank";
    if (/size cap/.test(e)) return "size cap";
    if (/disk reserve|free space/.test(e)) return "disk space";
    if (/changed while being read/.test(e)) return "files changed";
    if (/anchor|not confirmed/.test(e)) return "iCloud not confirmed";
    return "error";
  }
  // "12 added \u00b7 18s ago", "Nothing new \u00b7 55m ago", "Failed \u00b7 2h ago" and a tag with the reason.
  function lastRunBody(j) {
    if (!j.last_run) return { title: "Not run yet", nodes: [h("span", { class: "lr" }, "Not run yet")] };
    var what, tag = "";
    if (j.result !== "ok") { what = j.status === "paused" ? "Paused" : "Failed"; if (what === "Failed") tag = failTag(j); }
    else if (j.changes && j.changes.summary && j.changes.summary !== "nothing changed") what = j.changes.summary;
    else what = "Nothing new";
    var full = what + ", " + rel(j.last_run) + " (" + absTime(j.last_run) + ")" + (tag ? ", " + tag : "");
    return { title: full, nodes: [h("span", { class: "lr", "aria-hidden": "true" }, [h("span", { class: "what" }, what), " \u00b7 " + shortAgo(j.last_run)]),
      tag ? h("span", { class: "tag", "aria-hidden": "true" }, tag) : null, h("span", { class: "sr" }, full)] };
  }
  // "\u2713 40/40 \u00b7 Tue 14:24", "\u2717 2 differ" or "Not checked": one line; the whole sentence, with the next check, is the title.
  function verifiedCell(j) {
    var v = j.verify, bad = checkBad(j), full, short;
    if (!v) { full = "Not checked yet. " + nextCheckText(j) + "."; short = "Not checked"; }
    else {
      full = (bad ? v.mismatched + " of " + v.checked + " files differ" : v.matched + " of " + v.checked + " files match") + ", checked " + when(v.at) + ". " + nextCheckText(j) + ".";
      short = bad ? "\u2717 " + v.mismatched + " differ" : "\u2713 " + v.matched + "/" + v.checked + " \u00b7 " + when(v.at);
    }
    return h("span", { class: "ver" + (bad ? " bad" : !v ? " quiet" : ""), title: full }, [h("span", { "aria-hidden": "true" }, short), h("span", { class: "sr" }, full)]);
  }
  // Files kept in .deleted by the last run: the number only when there is one.
  function keptCell(j) {
    if (j.moved_to_deleted > 0) return String(j.moved_to_deleted);
    return h("span", { class: "quiet" }, [h("span", { "aria-hidden": "true" }, "\u2013"), h("span", { class: "sr" }, "None kept")]);
  }
  // A long path in the middle: "NasSharedDrive / Photos / 2026 / Holi\u2026day". The whole path stays in the title.
  function midEllipsis(text, max) {
    if (text.length <= max) return text;
    var keep = max - 1, head = Math.ceil(keep / 2), tail = Math.floor(keep / 2);
    return text.slice(0, head) + "\u2026" + text.slice(text.length - tail);
  }
  function nextSyncText(jobs) {
    var next = jobs.filter(function (j) { return j.next_run && j.enabled; }).map(function (j) { return j.next_run; }).sort()[0];
    if (!next) return "";
    var d = new Date(next), now = new Date(), hm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    if (d <= now) return "next sync due now";
    var days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
    return "next sync " + (days === 0 ? "today " : days === 1 ? "tomorrow " : d.toLocaleDateString(undefined, { weekday: "short" }) + " ") + hm;
  }
  function summaryLine(jobs, pause) {
    if (!jobs.length) return "Nothing is being copied yet.";
    var n = function (f) { return jobs.filter(f).length; };
    var parts = [];
    var ok = n(function (j) { return j.status === "ok"; }), bad = n(function (j) { return j.status === "failed"; });
    var paused = n(function (j) { return j.status === "paused"; }), busy = n(busyOf);
    var waiting = n(function (j) { return j.status === "skipped" || j.status === "never"; });
    if (ok) parts.push(ok + " up to date");
    if (bad) parts.push(bad + (bad === 1 ? " needs" : " need") + " attention");
    if (busy) parts.push(busy + " copying");
    if (waiting) parts.push(waiting + " waiting");
    if (paused) parts.push(paused + " paused");
    var next = nextSyncText(jobs);
    if (pause && pause.paused) parts.push(pauseText(pause));
    else if (next) parts.push(next);
    return parts.join(" · ");
  }

  // ---- actions shared by the Folders page and the folder page ---------------
  function pluralN(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }

  // ---- live progress -------------------------------------------------------
  // What a preview or a copy is doing right now, from the server: a bar, the phase in
  // words, counters, the folder it is in, and a plain sentence when nothing has
  // happened for a while. Built once and then updated in place, so the page does not
  // redraw (and a screen reader does not re-read it) every second.
  function fmtClock(sec) {
    sec = Math.max(0, Math.round(sec));
    var hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
    function two(n) { return (n < 10 ? "0" : "") + n; }
    return hh ? hh + ":" + two(mm) + ":" + two(ss) : two(mm) + ":" + two(ss);
  }
  function num(n) { return Number(n || 0).toLocaleString(); }
  function progressCounters(d) {
    var clock = fmtClock(d.elapsed_s);
    if (d.phase === "copying") {
      return (d.op === "restore" ? "Restored " : "Copied ") + num(d.files) + " of " + num(d.total_files) + " files \u00b7 " + fmtBytes(d.bytes) + " of " + fmtBytes(d.total_bytes) + " \u00b7 " + clock;
    }
    if (d.phase === "checking") {
      return "Compared " + num(d.files) + " of " + num(d.total_files) + " files \u00b7 " + fmtBytes(d.bytes) + " read \u00b7 " + clock;
    }
    if (d.reading) {
      return "Read " + num(d.files) + (d.total_files ? (d.estimated ? " of about " : " of ") + num(d.total_files) : "") + " files \u00b7 " + fmtBytes(d.bytes) + " \u00b7 " + clock;
    }
    return clock;
  }
  function stallText(d) {
    var secs = Math.round(d.idle_s);
    var text = "No progress for " + (secs >= 120 ? Math.round(secs / 60) + " minutes" : secs + " seconds") + ". A slow network share can pause for a while; this is not an error yet.";
    if (d.can_cancel) text = "Nothing has moved for " + Math.round(secs / 60) + " minutes. Check that the NAS is switched on and its share still opens in Finder. You can stop this run: nothing is left half-written and your originals are not touched.";
    return text;
  }
  var OP_NOUN = { preview: "Preview of ", sync: "Copy of ", check: "Check of ", restore: "Restore of " };
  // `opts.stage` = { of: 2 } turns it into one overall bar over named stages: the copy
  // is stage 1 and the check that follows it stage 2 ("Copying 1 of 2", "Checking 2 of 2").
  function progressWidget(j, opts) {
    var what = OP_NOUN[j.operation || (j.previewing ? "preview" : "sync")] || "Work on ";
    var fill = h("span", { class: "fill" });
    var bar = h("div", { class: "bar", role: "progressbar", "aria-label": what + j.name, "aria-valuemin": "0", "aria-valuemax": "100" }, fill);
    var phase = h("p", { class: "phase" });
    var count = h("p", { class: "count" });
    var where = h("p", { class: "hint where" });
    var stall = h("div", { class: "notice", role: "status" });
    var stop = h("button", { type: "button", class: "btn" }, "Stop this run");
    var live = h("div", { class: "sr", role: "status", "aria-live": "polite" });
    var root = h("div", { class: "progress", "data-progress": j.name }, [bar, phase, count, where, stall, live]);
    var lastSaid = 0, lastPhase = "", stopping = false;
    stop.addEventListener("click", function () {
      stop.disabled = true; stopping = true;
      api("POST", "/api/jobs/" + encodeURIComponent(j.name) + "/cancel").then(function (r) { toast(r.message); }, fail);
    });
    root._update = function (job) {
      var d = job.progress_detail;
      var preview = job.previewing || (d && d.op === "preview");
      var staged = opts && opts.stage ? opts.stage : null;
      if (!d) {
        bar.removeAttribute("aria-valuenow"); bar.setAttribute("aria-busy", "true"); bar.classList.remove("det");
        fill.style.width = "";
        phase.textContent = "Waiting for its turn";
        count.textContent = preview ? "A preview changes nothing." : "";
        where.textContent = ""; stall.hidden = true;
        return;
      }
      var which = staged ? (d.op === "check" ? 2 : 1) : 0;
      var pct = d.percent;
      if (staged) pct = ((which - 1) * 100 + (pct === null || pct === undefined ? 0 : pct)) / staged.of;
      var det = staged ? true : (pct !== null && pct !== undefined);
      var stageName = staged ? (which === 2 ? "Checking 2 of " + staged.of : "Copying 1 of " + staged.of) : "";
      bar.classList.toggle("det", det);
      if (det) {
        bar.setAttribute("aria-valuenow", String(Math.round(pct)));
        bar.setAttribute("aria-valuetext", Math.round(pct) + " percent, " + (stageName ? stageName + ", " : "") + d.label);
        bar.removeAttribute("aria-busy");
        fill.style.width = pct + "%";
      } else {
        bar.removeAttribute("aria-valuenow"); bar.removeAttribute("aria-valuetext");
        bar.setAttribute("aria-busy", "true");
        fill.style.width = "";
      }
      phase.textContent = (stageName ? stageName + ": " : "") + (preview ? "Preview (nothing is changed): " : "") + (d.cancelling ? "Stopping" : d.label);
      count.textContent = progressCounters(d);
      where.textContent = d.path ? "Now in: " + d.path : "";
      if (d.cancelling || stopping) {
        stall.hidden = false; stall.className = "notice info";
        stall.textContent = "Stopping: it stops at the next file. If the share is not answering this can take a moment.";
      } else if (d.stalled) {
        stall.hidden = false; stall.className = "notice";
        stall.textContent = stallText(d);
        if (d.can_cancel) { stop.disabled = false; stall.appendChild(h("div", { class: "row" }, stop)); }
      } else { stall.hidden = true; }
      // Tell a screen reader at most every five seconds, or at once when the phase changes.
      var now = Date.now();
      if (d.label !== lastPhase || now - lastSaid > 5000) {
        live.textContent = d.label + ". " + progressCounters(d) + (d.stalled ? ". " + stallText(d) : "");
        lastSaid = now; lastPhase = d.label;
      }
    };
    root._update(j);
    return root;
  }
  function hasWork(j) { return busyOf(j) || j.previewing || !!j.operation || !!j.progress_detail; }
  // Update every progress widget on the page from fresh status, without redrawing.
  function updateLiveProgress(s) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-progress]"), function (el) {
      var name = el.getAttribute("data-progress");
      var j = s.jobs.filter(function (x) { return x.name === name; })[0];
      if (j && el._update) el._update(j);
    });
  }
  // A page that has no polling loop of its own (a folder's settings, the deleted files)
  // watches one folder: when the server says something is happening to it, from a
  // click on this page or anywhere else, the bar appears in `holder` and follows it.
  // `kick()` asks again at once and keeps asking every second for a while.
  function pageWatch(name, holder) {
    var stopped = false, widget = null, timer = null, burstUntil = 0;
    function tick() {
      if (stopped) return;
      api("GET", "/api/jobs/" + encodeURIComponent(name)).then(function (j) {
        if (stopped) return;
        if (hasWork(j)) { if (!widget) { widget = progressWidget(j); holder.appendChild(widget); } widget._update(j); }
        else if (widget) { holder.removeChild(widget); widget = null; }
        return hasWork(j);
      }, function () { return false; }).then(function (busy) {
        if (stopped) return;
        timer = setTimeout(tick, busy || Date.now() < burstUntil ? 1000 : 5000);
      });
    }
    tick();
    var stop = function () { stopped = true; clearTimeout(timer); };
    state.stops.push(stop);
    return { stop: stop, kick: function () { burstUntil = Date.now() + 20000; clearTimeout(timer); tick(); } };
  }
  // How often to look: every second while something is running and the tab is
  // showing, every five seconds when it is hidden or nothing is running.
  function pollDelay(busy) { return busy && !document.hidden ? 1000 : 5000; }
  function jobsBusy(s) { return s.jobs.some(hasWork); }
  function jobsSig(s) {
    return JSON.stringify(s.jobs.map(function (j) { return [j.name, j.status, j.previewing, j.operation, !!j.preview, j.last_run, j.result, j.error, j.enabled, !!j.progress_detail, j.verify && j.verify.at]; }));
  }
  function startCheck(j, btn, after) {
    btn.disabled = true;     // until the server answers; what is shown is the server's own state
    return api("POST", "/api/jobs/" + encodeURIComponent(j.name) + "/verify")
      .then(function (r) { toast(r.message); }, fail)
      .then(function () { btn.disabled = false; return after(); }, function () {});
  }
  function startSync(j, accept, after) {
    var go = accept ? confirmDialog("Sync even though the source shrank?", "Files that are no longer in the source will be moved to the .deleted folder in iCloud (or removed, in plain mirror mode). Only do this if you meant to delete them.", "Sync anyway", true,
      { label: "Preview first", run: function () { startPreview(j, true, after); } }) : Promise.resolve(true);
    go.then(function (yes) {
      if (!yes) return;
      return api("POST", "/api/jobs/" + encodeURIComponent(j.name) + "/sync", { accept_shrink: accept }).then(function (r) { toast(r.message); return after(); });
    }).catch(function (e) { fail(e); after().catch(function () {}); });
  }
  var GUARDS = { shrink: "shrink guard", "size cap": "size cap", "disk reserve": "disk reserve", "first-run": "first-run rule", source: "source check", destination: "iCloud folder check", mount: "mount check", anchor: "iCloud confirmation" };
  // Preview next sync: queue it, wait for the worker, show what it found. It
  // changes nothing; the dialog says so.
  function startPreview(j, accept, after) {
    api("POST", "/api/jobs/" + encodeURIComponent(j.name) + "/preview", { accept_shrink: accept }).then(function (r) {
      toast(r.message);
      return after();
    }).then(function () { return waitPreview(j, accept, after); }).catch(function (e) { fail(e); after().catch(function () {}); });
  }
  function waitPreview(j, accept, after) {
    var url = "/api/jobs/" + encodeURIComponent(j.name) + "/preview", tries = 0, where = location.hash;
    function poll() {
      if (location.hash !== where) return Promise.resolve();   // the person went elsewhere: no dialog out of the blue
      return api("GET", url).then(function (r) {
        if (r.state !== "idle") { tries = 0; return after().catch(function () {}).then(function () { return new Promise(function (go) { setTimeout(go, 900); }).then(poll); }); }
        if (!r.preview) { if (++tries < 4) return new Promise(function (go) { setTimeout(go, 400); }).then(poll); throw new Error("the preview did not finish; try again"); }
        return after().catch(function () {}).then(function () { return showPreview(j, r.preview, after); });
      });
    }
    return poll();
  }
  function showPreview(j, p, after) {
    var rows = [h("dt", null, "Files in the source"), h("dd", null, String(p.files_total))];
    if (!p.would_refuse || p.accept_shrink) rows = rows.concat([
      h("dt", null, "Would be added"), h("dd", null, String(p.added)),
      h("dt", null, "Would be updated"), h("dd", null, String(p.updated)),
      h("dt", null, p.removed_how === "removed" ? "Would be removed" : "Would be moved to .deleted"), h("dd", null, String(p.removed)),
      h("dt", null, "To copy"), h("dd", null, fmtBytes(p.bytes))]);
    var total = p.added + p.updated + p.removed;
    var nodes = [
      h("p", null, [h("span", { class: "badge" }, "Preview"), " Nothing was changed: not iCloud, not the source, not Keepsake's records."]),
      p.would_refuse
        ? h("div", { class: "notice bad", role: "alert" }, [h("strong", null, "A sync started now would be refused" + (GUARDS[p.guard] ? " by the " + GUARDS[p.guard] : "") + ". "), p.message])
        : h("div", { class: "notice info" }, p.message),
      p.shrink_note ? h("div", { class: "notice" }, p.shrink_note) : null,
      p.first_run && !p.would_refuse ? h("p", { class: "hint" }, "This would be a safe first run: it copies and checks, and removes nothing that is already in iCloud." + (p.left_alone ? " " + pluralN(p.left_alone, "file") + " already there would be left alone." : "")) : null,
      h("dl", { class: "counts", "aria-label": "What a sync would do" }, rows),
      p.samples.length ? h("details", { open: true }, [h("summary", null, total > p.samples.length ? "First " + p.samples.length + " of " + total + " changed files" : "Changed files"),
        h("ul", { class: "changes", "aria-label": "Files a sync would change" }, p.samples.map(function (x) { return h("li", null, x.kind + ": " + x.path); }))]) : null,
      p.notes.map(function (n) { return h("p", { class: "hint" }, n[0].toUpperCase() + n.slice(1) + "."); }),
      h("p", { class: "hint" }, "Taken " + absTime(p.at) + ". It reads the source and the iCloud folder now; files can change before a real sync. It does not copy or read anything back, so it cannot tell whether a copy will fail.")];
    var buttons = [];
    if (p.would_refuse && p.guard === "shrink" && !p.accept_shrink) buttons.push({ label: "Preview accepting the smaller source", run: function () { startPreview(j, true, after); } });
    if (p.accept_shrink && !p.would_refuse) buttons.push({ label: "Sync anyway", run: function () { startSync(j, true, after); } });
    else if (!p.would_refuse) buttons.push({ label: "Sync now", primary: true, run: function () { startSync(j, false, after); } });
    return infoDialog("Preview of " + j.name, nodes, buttons, "preview:" + j.name);
  }

  // ---- the first-run checklist ---------------------------------------------
  // Three steps, each ticked by the server from what is really there.
  function firstRunCard(s, compact) {
    var fr = s.first_run;
    if (!fr || fr.done) return null;
    var done = {}; fr.steps.forEach(function (x) { done[x.id] = x.done; });
    var first = s.jobs[0];
    var current = !done.confirm ? "confirm" : !done.folder ? "folder" : "sync-check";
    var steps = [
      { id: "confirm", title: "Confirm the iCloud folder",
        text: done.confirm ? "Confirmed: Keepsake wrote its marker file in the iCloud folder." : !s.icloud_available ? "Keepsake cannot see the iCloud Drive folder yet, so nothing can be copied. Help, step 2 explains how to share it." : "Check in Finder that iCloud Drive shows a folder called Keepsake, then confirm it once. Nothing is copied before you do.",
        link: done.confirm ? null : !s.icloud_available ? ["#/settings/help", "Open Help"] : ["#/settings/locations", "Open Settings"] },
      { id: "folder", title: "Add your first folder",
        text: done.folder ? pluralN(s.jobs.length, "folder") + " set up." : "Choose a folder to keep. Your original is only ever read.",
        link: done.folder ? null : ["#/add", "Add a folder"] },
      { id: "sync-check", title: "Run a first sync and check a copy",
        text: done.folder ? "Sync copies the folder into iCloud; Check reads some files back and compares them. Sync is on the folder's row, Check is in its \u201c\u2026\u201d menu, and both are on its page. This ticks when a run has finished and a check matched." : "Waits for the folder in step 2.",
        link: done.folder && first ? ["#/job/" + encodeURIComponent(first.name), "Open " + first.name] : null }];
    return h("section", { class: "card checklist", "aria-labelledby": "fr-h" }, [
      h("h2", { id: "fr-h", class: "mt0" }, compact ? "Finish setting up" : "Get started in three steps"),
      h("ol", null, steps.map(function (st, i) {
        var d = done[st.id];
        return h("li", { class: d ? "done" : "", "aria-current": st.id === current ? "step" : null, "data-step": st.id }, [
          h("span", { class: "tick", "aria-hidden": "true" }, d ? "\u2713" : String(i + 1)),
          h("div", null, [h("strong", null, st.title), h("span", { class: "sr" }, d ? " (done)" : " (to do)"),
            h("p", { class: "hint" }, st.text),
            st.link ? h("a", { class: "btn" + (st.id === current ? " primary" : ""), href: st.link[0] }, st.link[1]) : null])]);
      })),
      h("p", { class: "hint" }, "Steps tick by themselves from what Keepsake can see, and this list goes away when all three are done."),
      h("p", { class: "hint" }, h("a", { href: "#/settings/help" }, "Read the help"))]);
  }

  // A small "more actions" menu: click, Enter, Space or ArrowDown opens it, the arrow keys move,
  // Escape or Tab closes it and Escape returns to the button. It is placed with position: fixed so a
  // scrolling table never clips it, and the page does not redraw under it (see tick()).
  var openMenu = null;
  function closeMenu(focusBack) {
    var m = openMenu;
    if (!m) return;
    openMenu = null;
    m.menu.hidden = true; m.btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", m.out, true);
    window.removeEventListener("resize", m.follow); window.removeEventListener("scroll", m.follow, true);
    if (focusBack) m.btn.focus({ preventScroll: true });
    if (afterMenu) afterMenu();
  }
  // The Folders page sets this: a redraw that came in while a menu was open is drawn when it closes.
  var afterMenu = null;
  window.addEventListener("hashchange", function () { closeMenu(false); });
  function menuOf(btn, menu) {
    function items() { return Array.prototype.filter.call(menu.querySelectorAll('[role="menuitem"]'), function (el) { return !el.disabled; }); }
    function place() {
      var r = btn.getBoundingClientRect(), wasHidden = menu.hidden;
      if (wasHidden) { menu.style.visibility = "hidden"; menu.hidden = false; }
      var w = menu.offsetWidth, ht = menu.offsetHeight;
      var left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
      var below = r.bottom + 4 + ht <= window.innerHeight || r.top - 4 - ht < 0;
      menu.style.left = left + "px"; menu.style.top = (below ? r.bottom + 4 : r.top - 4 - ht) + "px";
      if (wasHidden) menu.style.visibility = "";
    }
    function open() {
      closeMenu(false);
      place();
      btn.setAttribute("aria-expanded", "true");
      var m = { btn: btn, menu: menu };
      m.out = function (e) { if (!menu.contains(e.target) && !btn.contains(e.target)) closeMenu(false); };
      // The menu follows its button when the page scrolls or resizes, and goes when the button leaves the screen.
      m.follow = function () { var r = btn.getBoundingClientRect(); if (r.bottom < 0 || r.top > window.innerHeight) closeMenu(false); else place(); };
      document.addEventListener("pointerdown", m.out, true);
      window.addEventListener("resize", m.follow); window.addEventListener("scroll", m.follow, true);
      openMenu = m;
      var first = items()[0];
      if (first) first.focus({ preventScroll: true });
    }
    btn.addEventListener("click", function () { if (openMenu && openMenu.btn === btn) closeMenu(true); else open(); });
    btn.addEventListener("keydown", function (e) { if (e.key === "ArrowDown") { e.preventDefault(); open(); } });
    menu.addEventListener("keydown", function (e) {
      var list = items(), at = list.indexOf(document.activeElement);
      if (!list.length) return;
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
      else if (e.key === "ArrowDown") { e.preventDefault(); list[(at + 1) % list.length].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); list[(at - 1 + list.length) % list.length].focus(); }
      else if (e.key === "Home") { e.preventDefault(); list[0].focus(); }
      else if (e.key === "End") { e.preventDefault(); list[list.length - 1].focus(); }
      else if (e.key === "Tab") closeMenu(false);
    });
    // Choosing an item closes the menu and puts focus back on its button before the item's own action runs.
    menu.addEventListener("click", function (e) { if (e.target.closest('[role="menuitem"]')) closeMenu(true); }, true);
  }
  // A one-line note under a row. The whole sentence is in the page, cut by CSS; press it to read all of it.
  function lineNote(text, cls) {
    return h("details", { class: "notice line" + (cls ? " " + cls : "") }, h("summary", null, text));
  }

  function pageFolders() {
    var box = h("div", null);
    var summary = h("p", { class: "lead" }, "Kept safe in iCloud Drive, one way: your originals are only ever read.");
    setView("Folders", [h("div", { class: "row between" }, [
      h("div", null, [h("h1", null, "Folders"), summary]),
      h("a", { class: "btn primary", href: "#/add" }, "Add folder")]), box]);
    var recent = [];
    var pendingDraw = null;
    afterMenu = function () { if (pendingDraw) { var s = pendingDraw; pendingDraw = null; draw(s); } };
    function draw(s) {
      // Never redraw under an open menu (an action that finished a moment ago asks for one too):
      // keep the newest state and draw it when the menu closes.
      if (openMenu) { pendingDraw = s; return; }
      pendingDraw = null;
      var held = document.activeElement && document.activeElement.getAttribute && document.activeElement.getAttribute("data-focus");
      while (box.firstChild) box.removeChild(box.firstChild);
      summary.textContent = summaryLine(s.jobs, s.pause);
      var guide = firstRunCard(s, s.jobs.length > 0);
      if (guide) box.appendChild(guide);
      if (!s.jobs.length) return;
      box.appendChild(tableView(s));
      box.appendChild(h("div", { class: "key" }, [
        h("span", { class: "kc" }, "Copied"), h("span", { class: "kn" }, "Nothing new"), h("span", { class: "kw" }, "Paused or failed"), h("span", { class: "e" }, "No run")]));
      box.appendChild(h("section", { class: "recent", "aria-labelledby": "recent-h" }, [
        h("h2", { id: "recent-h" }, "Recent activity"),
        recent.length ? recent.map(function (e) {
          return h("div", { class: "l" }, [h("time", { datetime: e.at, title: absTime(e.at) }, when(e.at)),
            h("span", { class: e.level === "error" ? "error" : "" }, (e.job ? e.job + ": " : "") + (e.level === "error" ? "problem, " : "") + e.message)]);
        }) : h("p", { class: "muted" }, "Nothing has happened yet."),
        h("p", null, h("a", { class: "tap", href: "#/activity" }, "All activity"))]));
      // The page redraws every few seconds: keep the keyboard where it was.
      if (held) {
        var again = box.querySelector('[data-focus="' + held + '"]');
        if (again && !again.disabled) {
          if (again.classList.contains("nb")) {   // keep the strip's one tab stop where the person left it
            Array.prototype.forEach.call(again.parentNode.children, function (b) { b.setAttribute("tabindex", "-1"); });
            again.setAttribute("tabindex", "0");
          }
          again.focus({ preventScroll: true });
        }
      }
    }
    function folderBody(s, j) {
      var busy = hasWork(j);
      var syncBtn = shrank(j)
        ? h("button", { type: "button", class: "lnk sync", "data-focus": "sync:" + j.name, "aria-label": "Review " + j.name + ": the source shrank", onclick: function () { startSync(j, true, refresh); } }, "Review")
        : h("button", { type: "button", class: "lnk sync", "data-focus": "sync:" + j.name, disabled: busy || !j.enabled, "aria-label": "Sync " + j.name + " now", onclick: function (e) { e.currentTarget.disabled = true; startSync(j, false, refresh); } }, busy ? "Working" : "Sync");
      var moreBtn = h("button", { type: "button", class: "lnk morebtn", "data-focus": "more:" + j.name, "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": "More actions for " + j.name }, "\u2026");
      var menu = h("div", { class: "menu", role: "menu", "aria-label": "More actions for " + j.name, hidden: true }, [
        h("button", { type: "button", role: "menuitem", tabindex: "-1", class: "lnk mi", "data-focus": "preview:" + j.name, disabled: busy || j.previewing, "aria-label": "Preview the next sync of " + j.name, onclick: function (e) { e.currentTarget.disabled = true; startPreview(j, false, refresh); } }, j.previewing ? "Previewing" : "Preview"),
        h("button", { type: "button", role: "menuitem", tabindex: "-1", class: "lnk mi", "data-focus": "check:" + j.name, disabled: busy || j.previewing, "aria-label": "Check a copy of " + j.name, onclick: function (e) { startCheck(j, e.currentTarget, refresh); } }, j.operation === "check" ? "Checking" : "Check"),
        h("a", { role: "menuitem", tabindex: "-1", class: "lnk mi", href: "#/job/" + encodeURIComponent(j.name), "aria-label": "Edit " + j.name }, "Edit")]);
      menuOf(moreBtn, menu);
      var samples = j.changes && j.changes.samples && j.changes.samples.length ? j.changes.samples : null;
      var run = lastRunBody(j);
      var lastRun = samples
        ? h("details", { class: "when" }, [h("summary", { title: run.title }, run.nodes),
            h("ul", { class: "changes", "aria-label": "Changed files in the last run of " + j.name }, samples.map(function (x) { return h("li", null, x.kind + ": " + x.path); }))])
        : h("span", { title: run.title }, run.nodes);
      var extra = [];
      if (hasWork(j)) extra.push(progressWidget(j));
      if (checkBad(j)) extra.push(lineNote(checkLine(j), "bad"));
      if (showErr(j)) extra.push(lineNote(j.error, j.status === "failed" ? "bad" : ""));
      j.warnings.forEach(function (w) { extra.push(lineNote(w)); });
      if (noteOf(j)) extra.push(lineNote(noteOf(j), "plain"));
      if (j.icloud && j.icloud.evicted) extra.push(h("details", { class: "evicted" }, [
        h("summary", null, j.icloud.evicted + (j.icloud.evicted === 1 ? " file" : " files") + " evicted by macOS Optimize Storage"),
        h("p", { class: "hint" }, j.icloud.note)]));
      var where = j.root + " / " + j.source, label = displayName(j.name);
      return h("tbody", { class: "job s-" + j.status, "data-job": j.name }, [
        h("tr", { class: "f" }, [
          h("th", { scope: "row", class: "c-folder" }, [h("a", { class: "fname", href: "#/job/" + encodeURIComponent(j.name), title: label === j.name ? null : j.name, "aria-label": label + ", edit or remove" }, label),
            h("span", { class: "src", title: where }, [h("span", { "aria-hidden": "true" }, midEllipsis(displayName(j.root) + " / " + j.source, 30)), h("span", { class: "sr" }, where)])]),
          h("td", { class: "status c-status" }, pill(j)),
          h("td", { class: "nights c-nights" }, nightsStrip(j, s.today)),
          h("td", { class: "col c-last", "data-label": "Last run" }, lastRun),
          h("td", { class: "col c-ver", "data-label": "Verified" }, verifiedCell(j)),
          h("td", { class: "col c-kept" + (j.moved_to_deleted > 0 ? "" : " zero"), "data-label": "Kept", title: "Files kept in .deleted by the last run" }, keptCell(j)),
          h("td", { class: "act c-act" }, [syncBtn, h("span", { class: "menuwrap" }, [moreBtn, menu])])]),
        extra.length ? h("tr", { class: "more" }, h("td", { colspan: "7" }, h("div", { class: "moreinner" }, extra))) : null]);
    }
    function tableView(s) {
      return h("div", { class: "tablewrap" }, h("table", { class: "folders" }, [
        h("thead", null, h("tr", null, [
          h("th", { scope: "col", class: "c-folder" }, "Folder"), h("th", { scope: "col", class: "c-status" }, "Status"), h("th", { scope: "col", class: "c-nights" }, "Last 14 nights"),
          h("th", { scope: "col", class: "c-last" }, "Last run"), h("th", { scope: "col", class: "c-ver" }, "Verified"),
          h("th", { scope: "col", class: "c-kept", title: "Files kept in .deleted by the last run" }, "Kept"), h("th", { scope: "col", class: "c-act" }, h("span", { class: "sr" }, "Actions"))]))]
        .concat(s.jobs.map(function (j) { return folderBody(s, j); }))));
    }
    function loadRecent() { return api("GET", "/api/activity?limit=5").then(function (d) { recent = d.entries; }, function () {}); }
    var lastSig = "", lastJobs = "", token = ++state.pollToken;
    // What a person can see: the jobs, the recent activity and the minute (relative
    // times). A redraw that changes none of these only churns focus and keypresses.
    function seen(s) { return jobsSig(s) + "|" + JSON.stringify([s.pause, s.first_run, s.icloud_available]) + "|" + recent.length + "|" + (recent[0] ? recent[0].at : "") + "|" + Math.floor(Date.now() / 60000); }
    function refresh() { return Promise.all([loadStatus(), loadRecent()]).then(function (r) { lastSig = seen(r[0]); lastJobs = jobsSig(r[0]); draw(r[0]); }); }
    // While something runs, update its progress in place every second; redraw the page
    // only when something other than the numbers changed, and never when nothing did.
    function tick() {
      return Promise.all([loadStatus(), loadRecent()]).then(function (r) {
        var s = r[0];
        if (jobsBusy(s) && jobsSig(s) === lastJobs && box.querySelector("[data-progress]")) { updateLiveProgress(s); return; }
        if (seen(s) === lastSig || keyBusy() || openMenu) return;      // nothing new to show, or the person is in the middle of something (a menu open too): next tick
        lastSig = seen(s); lastJobs = jobsSig(s); draw(s);
      });
    }
    function loop() {
      var busy = !!state.status && jobsBusy(state.status);
      state.timer = setTimeout(function () {
        if (token !== state.pollToken) return;
        ((!document.hidden || busy) ? tick() : Promise.resolve()).catch(function () {}).then(loop);
      }, pollDelay(busy));
    }
    refresh().catch(fail).then(loop);
  }

  // folder picker -----------------------------------------------------------
  // `sel` is the selection shared with the page: it outlives moving into a
  // subfolder or to another location, so folders can be gathered from several places.
  function makeSelection() {
    var sel = { items: [], listeners: [], locked: false, notice: null };
    sel.has = function (root, path) {
      return sel.items.some(function (i) { return i.root === root && i.path === path; });
    };
    sel.set = function (root, path, on) {
      if (sel.locked) return;
      if (on && !sel.has(root, path)) sel.items.push({ root: root, path: path });
      if (!on) sel.items = sel.items.filter(function (i) { return !(i.root === root && i.path === path); });
      sel.listeners.forEach(function (fn) { fn(); });
    };
    return sel;
  }
  // `bases` made unique against `used`: name, then name-2, name-3. Always a valid
  // folder name (40 characters at most).
  function uniqueFrom(bases, used) {
    var seen = {};
    used.forEach(function (n) { seen[String(n).toLowerCase()] = true; });
    return bases.map(function (base) {
      var name = base, n = 2;
      while (seen[name.toLowerCase()]) { var tail = "-" + n; name = base.slice(0, 40 - tail.length) + tail; n++; }
      seen[name.toLowerCase()] = true;
      return name;
    });
  }
  function baseNames(items) { return items.map(function (it) { return camel(it.path.split("/").pop()); }); }
  function updatedText(at) {
    var s = Math.round((Date.now() - at) / 1000);
    if (s < 10) return "Updated just now";
    if (s < 60) return "Updated " + s + " seconds ago";
    var m = Math.round(s / 60);
    return "Updated " + m + (m === 1 ? " minute ago" : " minutes ago");
  }
  // A Refresh button with a spinner while it reads, and how long ago it last did.
  function refreshControl(label, onclick) {
    var icon = h("span", { class: "ricon", "aria-hidden": "true" }, "↻");
    var spin = h("span", { class: "spin", "aria-hidden": "true", hidden: true });
    var btn = h("button", { type: "button", class: "btn ghost refresh", "aria-label": label, onclick: onclick }, [icon, spin, h("span", null, "Refresh")]);
    var when = h("span", { class: "muted updated" }, "");
    var said = h("span", { class: "sr", role: "status" }, "");
    var ctl = { el: h("div", { class: "refresh-row" }, [btn, when, said]), at: 0 };
    ctl.busy = function (on) { btn.disabled = on; spin.hidden = !on; icon.hidden = on; if (on) when.textContent = "Refreshing"; };
    ctl.tick = function () { if (ctl.at && !btn.disabled) when.textContent = updatedText(ctl.at); };
    ctl.done = function () { ctl.at = Date.now(); ctl.tick(); said.textContent = "List refreshed"; };
    return ctl;
  }
  function picker(roots, sel) {
    var box = h("div", { class: "picker" });
    var listEl = h("div", { class: "plist" });
    var cur = { root: roots[0] ? roots[0].name : "", path: "" };
    var api2 = { el: box, ctl: null, onRefresh: null };
    var ctl = refreshControl("Refresh the folder list", function () { if (api2.onRefresh) api2.onRefresh(); else load(true); });
    api2.ctl = ctl;
    box.appendChild(h("div", { class: "pbar" }, ctl.el));
    box.appendChild(listEl);
    function browseUrl(root, path) { return "/api/browse?root=" + encodeURIComponent(root) + "&path=" + encodeURIComponent(path); }
    function clearList() { while (listEl.firstChild) listEl.removeChild(listEl.firstChild); }
    // keepScroll is a refresh: the list is read again, you stay in this folder at the
    // same place, and if the folder itself has gone you are taken to the nearest one
    // that is still there, and told so.
    function load(keepScroll) {
      var old = listEl.querySelector("ul"), top = keepScroll && old ? old.scrollTop : 0;
      ctl.busy(true);
      return api("GET", browseUrl(cur.root, cur.path)).then(function (d) {
        draw(d);
        var ul = listEl.querySelector("ul");
        if (ul) ul.scrollTop = top;
        ctl.busy(false); ctl.done();
      }, function (e) {
        ctl.busy(false);
        if (keepScroll && cur.path && /does not exist/.test(e.message || "")) {
          var gone = cur.path;
          cur.path = gone.indexOf("/") >= 0 ? gone.slice(0, gone.lastIndexOf("/")) : "";
          if (sel.notice) sel.notice("The folder " + cur.root + " / " + gone + " is no longer there, so you are back in " + (cur.path ? cur.root + " / " + cur.path : cur.root) + ".");
          return load(true);
        }
        clearList();
        listEl.appendChild(h("div", { class: "notice bad" }, e.message));
      });
    }
    function crumb(label, path) {
      return h("button", { type: "button", class: "btn ghost", onclick: function () { cur.path = path; load(false); } }, label);
    }
    // One row: a tick box that chooses the folder (the whole row is its label) and,
    // separately, Open, which looks inside it. The row stays marked while it is chosen.
    function row(name, path, extra, here) {
      var cb = h("input", { type: "checkbox", checked: sel.has(cur.root, path) });
      cb.addEventListener("change", function () { sel.set(cur.root, path, cb.checked); });
      return h("li", { class: "prow" + (here ? " here" : ""), "data-path": path }, [
        h("label", { class: "pick" }, [cb, h("span", { class: "ficon", "aria-hidden": "true" }),
          h("span", { class: "pname" }, here ? "This whole folder (" + name + ")" : name), extra]),
        here ? null : h("button", { type: "button", class: "open", "aria-label": "Open " + name,
          onclick: function () { cur.path = path; load(false); } }, "Open ›")]);
    }
    function mark() {
      Array.prototype.forEach.call(box.querySelectorAll("li.prow"), function (li) {
        var on = sel.has(cur.root, li.getAttribute("data-path"));
        li.classList.toggle("on", on);
        li.querySelector("input").checked = on;
      });
    }
    function draw(d) {
      clearList();
      var parts = d.path ? d.path.split("/") : [], acc = "", crumbs = [crumb(cur.root, "")];
      parts.forEach(function (p) { acc = acc ? acc + "/" + p : p; crumbs.push(h("span", { "aria-hidden": "true", class: "muted sep" }, "/")); crumbs.push(crumb(p, acc)); });
      listEl.appendChild(h("div", { class: "crumbs", role: "navigation", "aria-label": "Folder path" }, crumbs));
      var ul = h("ul", { "aria-label": "Folders in " + (d.path || cur.root) });
      if (d.path) ul.appendChild(row(parts[parts.length - 1], d.path, null, true));
      d.folders.forEach(function (f) {
        ul.appendChild(row(f.name, f.path, f.warn ? h("span", { class: "badge warn" }, "looks secret") : null, false));
      });
      if (!d.folders.length) ul.appendChild(h("li", { class: "pad muted" }, "No subfolders here."));
      listEl.appendChild(ul);
      if (d.truncated) listEl.appendChild(h("p", { class: "hint pad" }, "Only the first folders are shown."));
      mark();
    }
    // Which chosen folders are no longer on disk (one read per parent folder).
    api2.vanished = function () {
      var groups = {};
      sel.items.forEach(function (it) {
        var parent = it.path.indexOf("/") >= 0 ? it.path.slice(0, it.path.lastIndexOf("/")) : "";
        var key = it.root + "\u0000" + parent;
        (groups[key] = groups[key] || { root: it.root, parent: parent, items: [] }).items.push(it);
      });
      return Promise.all(Object.keys(groups).map(function (k) {
        var g = groups[k];
        return api("GET", browseUrl(g.root, g.parent)).then(function (d) {
          var have = {};
          d.folders.forEach(function (f) { have[f.path] = true; });
          return d.truncated ? [] : g.items.filter(function (it) { return !have[it.path]; });
        }, function () { return g.items; });
      })).then(function (lists) { return [].concat.apply([], lists); });
    };
    api2.refresh = function () { return load(true); };
    api2.setRoot = function (r) { cur.root = r; cur.path = ""; load(false); };
    sel.listeners.push(mark);
    load(false);
    return api2;
  }

  // ---- locations: the folders Keepsake may read ----------------------------------
  // A container cannot mount a new folder, so the deployment mounts broad parents
  // read-only and this chooses which folders under them count as a location. The
  // server enforces every rule (inside a read-only mount, no symlinks, never the
  // iCloud or data folder); the page only asks. locationsCard() is self-contained:
  // put its .el anywhere (Settings, the first-run steps) and call .reload() after a
  // change made elsewhere.
  function addLocationDialog(done) {
    var chosen = [], cur = "", existing = [], changed = false;
    var listEl = h("div", { class: "plist" });
    var noticeBox = h("ul", { class: "notices", role: "status" });
    var chosenBox = h("section", { class: "chosen", "aria-label": "Chosen locations" });
    var resultsBox = h("div", { class: "results", role: "status" });
    var addBtn = h("button", { type: "button", class: "btn primary", id: "loc-do-add", disabled: true, onclick: submit }, "Add location");
    var ctl = refreshControl("Refresh the folder list", function () { say(null); load(true); });
    var names = {};                                                // path -> name typed
    var picker2 = h("div", { class: "picker" }, [h("div", { class: "pbar" }, ctl.el), listEl]);
    function say(text) { if (text) noticeBox.appendChild(h("li", null, text)); else while (noticeBox.firstChild) noticeBox.removeChild(noticeBox.firstChild); }
    function url(path) { return "/api/locations/browse?path=" + encodeURIComponent(path); }
    function parentOf(p) { return p.lastIndexOf("/") > 0 ? p.slice(0, p.lastIndexOf("/")) : ""; }
    function derived() {
      var used = existing.slice();
      return chosen.map(function (p) {
        var name = names[p] !== undefined ? names[p] : uniqueFrom([camel(p.split("/").pop())], used)[0];
        used.push(name);
        return name;
      });
    }
    function mark() {
      Array.prototype.forEach.call(listEl.querySelectorAll("li.prow"), function (li) {
        var on = chosen.indexOf(li.getAttribute("data-path")) >= 0;
        li.classList.toggle("on", on);
        li.querySelector("input").checked = on;
      });
    }
    function toggle(path, on) {
      var at = chosen.indexOf(path);
      if (on && at < 0) chosen.push(path);
      if (!on && at >= 0) chosen.splice(at, 1);
      mark(); drawChosen();
    }
    function drawChosen() {
      while (chosenBox.firstChild) chosenBox.removeChild(chosenBox.firstChild);
      var n = chosen.length, ns = derived();
      chosenBox.appendChild(h("h3", { class: "chosen-h" }, n === 0 ? "No folders chosen yet" : n === 1 ? "1 folder chosen" : n + " folders chosen"));
      if (!n) chosenBox.appendChild(h("p", { class: "hint pad" }, "Tick the box next to a folder to make it a location. Open looks inside. Keepsake only ever reads it."));
      else chosenBox.appendChild(h("ul", { class: "chosen-list" }, chosen.map(function (p, i) {
        var input = h("input", { type: "text", class: "locname", maxlength: "40", autocomplete: "off", value: ns[i], "aria-label": "Name for " + p, placeholder: "HomeFolder" });
        input.addEventListener("input", function () { names[p] = input.value.trim(); });
        return h("li", null, [h("span", { class: "cpath" }, [h("strong", null, p), input]),
          h("button", { type: "button", class: "x", "aria-label": "Remove " + p, onclick: function () { toggle(p, false); } }, "×")]);
      })));
      addBtn.disabled = !n;
      addBtn.textContent = n > 1 ? "Add " + n + " locations" : "Add location";
    }
    function row(f, extra) {
      var cb = h("input", { type: "checkbox", checked: chosen.indexOf(f.path) >= 0 });
      cb.addEventListener("change", function () { toggle(f.path, cb.checked); });
      return h("li", { class: "prow", "data-path": f.path }, [
        h("label", { class: "pick" }, [cb, h("span", { class: "ficon", "aria-hidden": "true" }), h("span", { class: "pname" }, f.name), extra]),
        h("button", { type: "button", class: "open", "aria-label": "Open " + f.name, onclick: function () { cur = f.path; load(false); } }, "Open ›")]);
    }
    function draw(d) {
      while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
      var crumbs = [h("button", { type: "button", class: "btn ghost", onclick: function () { cur = ""; load(false); } }, "Mounted places")], acc = "";
      (d.path ? d.path.split("/").filter(Boolean) : []).forEach(function (p) {
        acc += "/" + p;
        var at = acc;
        crumbs.push(h("span", { "aria-hidden": "true", class: "muted sep" }, "/"));
        crumbs.push(h("button", { type: "button", class: "btn ghost", onclick: function () { cur = at; load(false); } }, p));
      });
      listEl.appendChild(h("div", { class: "crumbs", role: "navigation", "aria-label": "Folder path" }, crumbs));
      var ul = h("ul", { "aria-label": "Folders in " + (d.path || "the mounted places") });
      d.folders.forEach(function (f) {
        var badge = d.path === "" ? (f.readonly ? h("span", { class: "badge ok" }, "read-only") : h("span", { class: "badge warn" }, "not read-only")) :
          (f.warn ? h("span", { class: "badge warn" }, "looks secret") : null);
        ul.appendChild(row(f, badge));
      });
      if (!d.folders.length) ul.appendChild(h("li", { class: "pad muted" }, d.path === "" ? "Nothing is mounted for choosing yet." : "No subfolders here."));
      listEl.appendChild(ul);
      if (d.truncated) listEl.appendChild(h("p", { class: "hint pad" }, "Only the first folders are shown."));
      mark();
    }
    function load(keepScroll) {
      var old = listEl.querySelector("ul"), top = keepScroll && old ? old.scrollTop : 0;
      ctl.busy(true);
      return api("GET", url(cur)).then(function (d) {
        draw(d);
        var ul = listEl.querySelector("ul");
        if (ul) ul.scrollTop = top;
        ctl.busy(false); ctl.done();
        // Chosen folders that have gone since are unselected, in words.
        if (keepScroll) return Promise.all(chosen.map(function (p) {
          return api("GET", url(parentOf(p))).then(function (pd) { return pd.folders.some(function (f) { return f.path === p; }) || pd.truncated ? null : p; }, function () { return p; });
        })).then(function (gone) {
          gone.filter(Boolean).forEach(function (p) { toggle(p, false); say("The folder " + p + " is no longer there, so it was unselected."); });
        });
      }, function (e) {
        ctl.busy(false);
        if (keepScroll && cur) {
          var gone = cur;
          cur = parentOf(gone);
          say("The folder " + gone + " is no longer there, so you are back in " + (cur || "the mounted places") + ".");
          return load(true);
        }
        while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
        listEl.appendChild(h("div", { class: "notice bad" }, e.message));
      });
    }
    function submit() {
      if (!chosen.length) return;
      var rows = chosen.slice(), ns = derived(), done2 = [];
      addBtn.disabled = true;
      // One at a time: a refusal for one folder must not hide the ones that were added.
      rows.reduce(function (p, path, i) {
        return p.then(function () {
          return api("POST", "/api/locations", { name: ns[i], path: path }).then(function (r) {
            done2.push({ ok: true, label: ns[i], text: r.message, path: path }); existing.push(ns[i]); changed = true;
          }, function (e) { done2.push({ ok: false, label: path, text: e.message || "something went wrong", path: path }); });
        });
      }, Promise.resolve()).then(function () {
        done2.filter(function (d) { return d.ok; }).forEach(function (d) { chosen.splice(chosen.indexOf(d.path), 1); });
        while (resultsBox.firstChild) resultsBox.removeChild(resultsBox.firstChild);
        resultsBox.appendChild(h("ul", { class: "result-list" }, done2.map(function (d) {
          return h("li", { class: d.ok ? "ok" : "bad" }, [h("strong", null, (d.ok ? "Added " : "Not added: ") + d.label), " " + d.text]);
        })));
        mark(); drawChosen(); if (done) done();
      });
    }
    var body = h("div", { class: "stack" }, [
      h("p", { class: "hint" }, "Choose folders inside the places mounted read-only into Keepsake. A location can never be written to, and never overlaps where copies go."),
      picker2, noticeBox, chosenBox, resultsBox, h("div", { class: "row" }, addBtn)]);
    api("GET", "/api/locations").then(function (d) { existing = d.locations.map(function (l) { return l.name; }); drawChosen(); }).catch(function () {});
    drawChosen();
    load(false);
    infoDialog("Add location", body).then(function () { if (changed && done) done(true); });
  }

  function locationsCard(opts) {
    opts = opts || {};
    var card = h("section", { class: "card locations", "aria-labelledby": "loc-h" });
    function draw(d) {
      while (card.firstChild) card.removeChild(card.firstChild);
      add(card, [h("h2", { id: "loc-h", class: "mt0" }, "Locations"),
        h("p", { class: "hint" }, "The places Keepsake may read from. Always read-only: Keepsake can never change what is in them."),
        h("ul", { class: "plain", "aria-label": "Locations" }, d.locations.map(function (l) {
          var shown = displayName(l.name);
          return h("li", { class: "locrow", "data-location": l.name }, [
            h("div", { class: "lochead" }, [h("strong", null, shown), shown !== l.name ? h("small", { class: "muted real" }, l.name) : null,
              l.builtin ? h("span", { class: "badge" }, "Built in") : null,
              l.available ? null : h("span", { class: "badge warn", title: l.why }, "Unavailable")]),
            h("div", { class: "muted locpath" }, l.path + (l.jobs.length ? " · used by " + l.jobs.length + (l.jobs.length === 1 ? " folder" : " folders") : "")),
            l.builtin ? null : h("button", { type: "button", class: "btn", "aria-label": "Remove " + shown, onclick: function () {
              confirmDialog("Remove " + shown + "?", "Keepsake stops reading from it. Your folders there are not touched.", "Remove", true).then(function (yes) {
                if (!yes) return;
                return api("POST", "/api/locations/" + encodeURIComponent(l.name) + "/remove", {}).then(function (r) { toast(r.message); reload(); if (opts.onChange) opts.onChange(); });
              }).catch(fail);
            } }, "Remove")]);
        })),
        h("div", { class: "row" }, [h("button", { type: "button", class: "btn primary", id: "loc-add", disabled: !d.can_add, onclick: function () {
          addLocationDialog(function (final) { reload(); if (final && opts.onChange) opts.onChange(); });
        } }, "Add location")]),
        d.can_add ? null : h("p", { class: "hint" }, d.why)]);
    }
    function reload() { return api("GET", "/api/locations").then(draw).catch(fail); }
    reload();
    return { el: card, reload: reload };
  }
  window.keepsake = window.keepsake || {};
  window.keepsake.locationsCard = locationsCard;
  window.keepsake.displayName = displayName;

  function pageLocations() {
    loadStatus().then(function () {
      setView("Locations", [backLink("#/settings/locations", "Back to Settings"), h("h1", null, "Locations"),
        h("p", { class: "lead" }, "Choose which folders Keepsake may read. Add one here, then pick folders inside it on the Add folder page."),
        locationsCard().el]);
    }).catch(fail);
  }

  function pageAdd(opts) {
    opts = opts || {};     // the guided setup embeds this form: { embedded, mount(nodes), after() }
    loadStatus().then(function (s) {
      var roots = s.roots;
      if (!roots.length) { setView("Add folder", [h("h1", null, "Add folder"), h("div", { class: "notice bad" }, opts.embedded ? "No location is available. Go back and check the locations." : "No source locations are set up. See Settings.")]); return; }
      var sel = makeSelection();
      var taken = s.jobs.map(function (j) { return j.name; });
      var dest = { folders: [] };
      var NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,39}$/;
      // ---- destination: an existing folder in iCloud Drive / Keepsake, or a new one
      var destSel = h("select", { id: "f-dest" });
      var destCtl = refreshControl("Refresh the list of destination folders", function () { loadDest(true); });
      var destNote = h("p", { class: "hint", role: "status" });
      var nameIn = h("input", { type: "text", id: "f-name", maxlength: "40", autocomplete: "off", placeholder: "PhotoLibrary" });
      var nameTouched = false;
      nameIn.addEventListener("input", function () { nameTouched = true; drawChosen(); });
      var nameWrap = h("div", { class: "field" }, [h("label", { for: "f-name" }, "New folder name"), nameIn,
        h("p", { class: "hint" }, "Letters, digits and hyphens, no spaces, for example PhotoLibrary or DocumentsBackup. This is also the folder name in iCloud Drive, and cannot change later.")]);
      var ackBox = h("input", { type: "checkbox", id: "f-ack" });
      ackBox.addEventListener("change", drawChosen);
      var ackWrap = h("div", { class: "field", hidden: true }, [h("label", { class: "check", for: "f-ack" }, [ackBox,
        h("span", null, "Keepsake did not create this folder. Use it anyway: new folders are added inside it and nothing already there is changed.")])]);
      var destLine = h("p", { class: "hint", id: "f-destline" });
      function destChoice() { return destSel.value; }
      function destFolder(name) { return dest.folders.filter(function (f) { return f.name === name; })[0] || null; }
      // The same name in another case is the same folder on a Mac.
      function destClash(name) { return dest.folders.filter(function (f) { return f.name.toLowerCase() === name.toLowerCase(); })[0] || null; }
      function drawDestOptions() {
        var before = destSel.value;
        while (destSel.firstChild) destSel.removeChild(destSel.firstChild);
        destSel.appendChild(h("option", { value: "" }, "New folder"));
        dest.folders.forEach(function (f) {
          var tag = f.kind === "group" ? " (made by Keepsake)" : f.kind === "job" ? " (holds one folder's copy, cannot be shared)" : " (not made by Keepsake)";
          destSel.appendChild(h("option", { value: f.name, disabled: f.kind === "job" }, nameLabel(f.name) + tag));
        });
        if (before && !destFolder(before)) {
          destNote.textContent = "The folder " + before + " is no longer in iCloud Drive / Keepsake, so New folder is selected.";
          destSel.value = "";
        } else destSel.value = before;
      }
      function loadDest(manual) {
        destCtl.busy(true);
        if (manual) destNote.textContent = "";
        return api("GET", "/api/destinations").then(function (d) {
          dest.folders = d.folders;
          drawDestOptions();
          destCtl.busy(false); destCtl.done();
          drawChosen();
        }, function (e) { destCtl.busy(false); destNote.textContent = e.message; });
      }
      destSel.addEventListener("change", function () { destNote.textContent = ""; drawChosen(); });
      // ---- the chosen source folders, and where each one will land
      var pk = picker(roots, sel);
      var rootSel = h("select", { id: "f-root", onchange: function (e) { pk.setRoot(e.target.value); } },
        roots.map(function (r) { return h("option", { value: r.name }, nameLabel(r.name) + (r.available ? "" : " (unavailable)")); }));
      // A location added from here shows up in the list without leaving the page.
      function reloadRoots() {
        return loadStatus().then(function (s2) {
          var keep = rootSel.value;
          while (rootSel.firstChild) rootSel.removeChild(rootSel.firstChild);
          s2.roots.forEach(function (r) { rootSel.appendChild(h("option", { value: r.name }, nameLabel(r.name) + (r.available ? "" : " (unavailable)"))); });
          if (s2.roots.some(function (r) { return r.name === keep; })) rootSel.value = keep; else pk.setRoot(rootSel.value);
        }).catch(fail);
      }
      var noticeBox = h("ul", { class: "notices", role: "status" });
      var chosenBox = h("section", { class: "chosen", "aria-label": "Chosen folders" });
      var resultsBox = h("div", { class: "results", role: "status" });
      var addBtn = h("button", { type: "submit", class: "btn primary", id: "f-add", "aria-describedby": "f-reason" }, "Add folder");
      var reason = h("p", { class: "hint", id: "f-reason" }, "");
      function say(text) { if (text) noticeBox.appendChild(h("li", null, text)); else while (noticeBox.firstChild) noticeBox.removeChild(noticeBox.firstChild); }
      sel.notice = say;
      // Where each chosen folder lands: [{it, name, dest, group}]. One folder into a new
      // destination is a folder of its own, as before; anything else goes into a
      // destination folder, each source in a subfolder named after it.
      function plan() {
        var items = sel.items.slice(), choice = destChoice(), typed = nameIn.value.trim();
        if (choice === "" && items.length === 1) return [{ it: items[0], name: typed, dest: typed, group: "" }];
        var group = choice || typed, existing = choice ? (destFolder(choice) || { folders: [] }).folders : [];
        var subs = uniqueFrom(baseNames(items), existing);
        var names = uniqueFrom(subs, taken);
        return items.map(function (it, i) { return { it: it, name: names[i], dest: subs[i], group: group }; });
      }
      function problem() {
        var n = sel.items.length, choice = destChoice(), typed = nameIn.value.trim();
        if (!n) return "Choose at least one folder first: tick the box next to it.";
        if (choice === "") {
          if (!typed) return "Type a name for the new destination folder.";
          if (!NAME.test(typed)) return "The destination folder name can only use letters, digits and hyphens, with no spaces.";
          var clash = destClash(typed);
          if (clash) return "A folder called " + clash.name + " already exists in iCloud Drive / Keepsake (names that differ only by case count as the same). Choose it from the list, or type a different name.";
          if (n === 1 && taken.some(function (t) { return t.toLowerCase() === typed.toLowerCase(); })) return "A folder called " + typed + " is already being kept. Type a different name.";
          return "";
        }
        var f = destFolder(choice);
        if (f && f.kind === "other" && !ackBox.checked) return "Tick the box to confirm you want to use a folder that Keepsake did not create.";
        return "";
      }
      function drawChosen() {
        while (chosenBox.firstChild) chosenBox.removeChild(chosenBox.firstChild);
        var n = sel.items.length, choice = destChoice(), f = choice ? destFolder(choice) : null;
        if (choice === "" && !nameTouched) {
          var base = n === 1 ? baseNames(sel.items)[0] : "Backup";
          nameIn.value = uniqueFrom([base], taken.concat(dest.folders.map(function (x) { return x.name; })))[0];
        }
        var rows = plan();
        chosenBox.appendChild(h("h3", { class: "chosen-h" }, n === 0 ? "No folders chosen yet" : n === 1 ? "1 folder chosen" : n + " folders chosen"));
        if (!n) chosenBox.appendChild(h("p", { class: "hint pad" }, "Tick the box next to a folder to choose it. Use Open to look inside a folder first. You can choose folders from different places."));
        else chosenBox.appendChild(h("ul", { class: "chosen-list" }, rows.map(function (r) {
          return h("li", null, [
            h("span", { class: "cpath" }, [h("strong", null, r.it.root + " / " + r.it.path), h("span", { class: "muted cname" }, "iCloud Drive / Keepsake / " + (r.group ? r.group + " / " : "") + (r.dest || "…"))]),
            h("button", { type: "button", class: "x", "aria-label": "Remove " + r.it.root + " / " + r.it.path, onclick: function () { sel.set(r.it.root, r.it.path, false); } }, "×")]);
        })));
        var why = problem();
        addBtn.disabled = !!why;
        addBtn.textContent = n > 1 ? "Add " + n + " folders" : "Add folder";
        reason.textContent = why;
        reason.hidden = !why;
        nameWrap.hidden = choice !== "";
        ackWrap.hidden = !(f && f.kind === "other");
        var where = choice ? choice : (nameIn.value.trim() || "<this name>");
        destLine.textContent = "Copied into iCloud Drive / Keepsake / " + where + "." + (n > 1 || choice ? " Each folder you chose goes in its own subfolder named after it." : "");
      }
      sel.listeners.push(drawChosen);
      // Refresh: the source list is read again (staying in the same folder and place),
      // and any chosen folder that has since gone is unselected, in words.
      pk.onRefresh = function () {
        say(null);
        pk.refresh().then(function () { return pk.vanished(); }).then(function (gone) {
          gone.forEach(function (it) { sel.set(it.root, it.path, false); say("The folder " + it.root + " / " + it.path + " is no longer there, so it was unselected."); });
        });
      };
      state.timer = setInterval(function () { pk.ctl.tick(); destCtl.tick(); }, 15000);
      var every = h("input", { type: "number", id: "f-every", min: "1", max: "168", value: "24" });
      var atIn = h("input", { type: "time", id: "f-at", value: "03:30" });
      var kind = h("select", { id: "f-kind" }, [h("option", { value: "every" }, "Every N hours"), h("option", { value: "at" }, "Every day at a time")]);
      var auto = autoSelect("f-auto", "weekly");
      var keep = h("input", { type: "number", id: "f-keep", min: "1", max: "365", value: "30" });
      var maxgb = h("input", { type: "number", id: "f-max", min: "1", max: "2000", placeholder: "Default: " + s.caps.max_dest_gb });
      var excl = h("textarea", { id: "f-excl", placeholder: "*.tmp\ncache" });
      var mirror = h("input", { type: "checkbox", id: "f-mirror" });
      var adopt = h("input", { type: "checkbox", id: "f-adopt" });
      function bodyFor(r) {
        var f = r.group ? destFolder(r.group) : null;
        return { name: r.name, dest: r.dest, group: r.group, root: r.it.root, source: r.it.path,
          schedule: kind.value === "at" ? { at: atIn.value } : { every_hours: parseInt(every.value, 10) },
          mode: mirror.checked ? "mirror" : "mirror-keep-deleted", keep_days: parseInt(keep.value, 10),
          auto_check: auto.value,
          max_gb: maxgb.value ? parseInt(maxgb.value, 10) : null,
          excludes: excl.value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean),
          adopt_existing: adopt.checked || !!(f && f.kind === "other" && ackBox.checked) };
      }
      var form = h("form", { class: "stack", novalidate: true, onsubmit: function (e) {
        e.preventDefault();
        if (!sel.items.length) { toast("Choose a folder first."); return; }
        if (problem()) { toast(problem()); return; }
        if (sel.locked) return;
        var rows = plan();
        sel.locked = true; addBtn.disabled = true;
        var done = [];
        // One at a time, in the order chosen: the engine takes one job at a time anyway,
        // and a refusal for one folder must not hide the folders that were added.
        rows.reduce(function (p, r) {
          return p.then(function () {
            return api("POST", "/api/jobs", bodyFor(r)).then(function (res) {
              done.push({ ok: true, label: r.name, text: res.message, r: r });
              taken.push(r.name);
            }, function (err) {
              done.push({ ok: false, label: r.it.root + " / " + r.it.path, text: err.message || "something went wrong", r: r });
            });
          });
        }, Promise.resolve()).then(function () {
          sel.locked = false;
          var added = done.filter(function (d) { return d.ok; }), refused = done.filter(function (d) { return !d.ok; });
          added.forEach(function (d) { sel.set(d.r.it.root, d.r.it.path, false); });
          if (!refused.length) { toast(rows.length === 1 ? added[0].text : "Added " + added.length + " folders."); if (opts.after) return opts.after(); location.hash = "#/folders"; return; }
          toast(rows.length === 1 ? refused[0].text : added.length + " added, " + refused.length + " not added. See the list below.");
          while (resultsBox.firstChild) resultsBox.removeChild(resultsBox.firstChild);
          if (rows.length > 1) {
            resultsBox.appendChild(h("ul", { class: "result-list" }, done.map(function (d) {
              return h("li", { class: d.ok ? "ok" : "bad" }, [h("strong", null, (d.ok ? "Added " : "Not added: ") + d.label), " " + d.text]);
            })));
            if (added.length && !opts.embedded) resultsBox.appendChild(h("p", null, h("a", { href: "#/folders" }, "Go to Folders")));
          }
          loadDest(false);
        });
      } }, [
        h("div", { class: "field" }, [h("label", { for: "f-root" }, "Where is it?"), h("div", { class: "row" }, [rootSel,
          h("button", { type: "button", class: "btn", id: "f-addloc", onclick: function () { addLocationDialog(function () { reloadRoots(); }); } }, "Add location"),
          opts.embedded ? null : h("a", { class: "btn ghost", href: "#/locations" }, "Manage locations")])]),
        h("div", { class: "field" }, [h("span", { class: "label", id: "pick-l" }, "Folders to keep"), pk.el, noticeBox, chosenBox]),
        h("div", { class: "field destfield" }, [h("label", { for: "f-dest" }, "Destination folder"), destSel, destCtl.el, destNote, nameWrap, ackWrap, destLine]),
        h("fieldset", null, [h("legend", null, "Schedule"), h("div", { class: "grid2" }, [field("How often", kind, null, "f-kind"), field("Hours between runs", every, "1 to 168", "f-every"), field("Time of day", atIn, "Used for the daily choice", "f-at")])]),
        field("Check a copy automatically", auto, "Reads a rotating sample of up to 40 files back from the source and from the iCloud folder on this Mac and compares them. Off, daily, weekly or monthly.", "f-auto"),
        h("details", null, [h("summary", null, "More options"), h("div", { class: "stack" }, [
          field("Skip files matching (one per line)", excl, "Patterns such as *.tmp or cache.", "f-excl"),
          h("div", { class: "grid2" }, [field("Keep deleted files for (days)", keep, null, "f-keep"), field("Size limit for this folder (GB)", maxgb, null, "f-max")]),
          h("div", { class: "field" }, [h("label", { class: "check", for: "f-mirror" }, [mirror, h("span", null, "Plain mirror: delete from iCloud when I delete at the source (nothing is kept)")])]),
          h("div", { class: "field" }, [h("label", { class: "check", for: "f-adopt" }, [adopt, h("span", null, "Take over an iCloud folder with this name that already has files")])])])]),
        resultsBox,
        h("div", { class: "row" }, [addBtn, opts.embedded ? null : h("a", { class: "btn", href: "#/folders" }, "Cancel")]),
        reason]);
      drawChosen();
      loadDest(false);
      if (opts.mount) { opts.mount([form]); return; }
      setView("Add folder", [backLink("#/folders", "Back to Folders"), h("h1", null, "Add folder"), h("p", { class: "lead" }, "Choose one or more folders from the locations this Keepsake may read. Your original is never changed. The same schedule and options apply to each folder you add."), form]);
    }).catch(fail);
  }

  function pageJob(name) {
    loadStatus().then(function () { return api("GET", "/api/jobs/" + encodeURIComponent(name)); }).then(function (j) {
      var kind = h("select", { id: "j-kind" }, [h("option", { value: "every", selected: !j.schedule.at }, "Every N hours"), h("option", { value: "at", selected: !!j.schedule.at }, "Every day at a time")]);
      var every = h("input", { type: "number", id: "j-every", min: "1", max: "168", value: String(j.schedule.every_hours || 24) });
      var atIn = h("input", { type: "time", id: "j-at", value: j.schedule.at || "03:30" });
      var auto = autoSelect("j-auto", j.auto_check);
      var stale = h("input", { type: "number", id: "j-stale", min: "0", max: "365", value: String(j.stale_days) });
      var keep = h("input", { type: "number", id: "j-keep", min: "1", max: "365", value: String(j.keep_days) });
      var maxgb = h("input", { type: "number", id: "j-max", min: "1", max: "2000", value: j.max_gb ? String(j.max_gb) : "", placeholder: "Default: " + state.status.caps.max_dest_gb });
      var excl = h("textarea", { id: "j-excl" }); excl.value = (j.excludes || []).join("\n");
      var mirror = h("input", { type: "checkbox", id: "j-mirror", checked: j.mode === "mirror" });
      var enabled = h("input", { type: "checkbox", id: "j-enabled", checked: j.enabled });
      var form = h("form", { class: "stack", novalidate: true, onsubmit: function (e) {
        e.preventDefault();
        api("PATCH", "/api/jobs/" + encodeURIComponent(j.name), {
          schedule: kind.value === "at" ? { at: atIn.value } : { every_hours: parseInt(every.value, 10) },
          mode: mirror.checked ? "mirror" : "mirror-keep-deleted", keep_days: parseInt(keep.value, 10),
          auto_check: auto.value, stale_days: parseInt(stale.value, 10),
          max_gb: maxgb.value ? parseInt(maxgb.value, 10) : null, enabled: enabled.checked,
          excludes: excl.value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean) }).then(function (r) { toast(r.message); location.hash = "#/folders"; }).catch(fail);
      } }, [
        h("p", null, [h("strong", null, "Source: "), j.root + " / " + j.source, h("br"), h("strong", null, "In iCloud: "), "Keepsake / " + (j.group ? j.group + " / " : "") + j.dest]),
        h("fieldset", null, [h("legend", null, "Schedule"), h("div", { class: "grid2" }, [field("How often", kind, null, "j-kind"), field("Hours between runs", every, "1 to 168", "j-every"), field("Time of day", atIn, null, "j-at")])]),
        h("fieldset", null, [h("legend", null, "Trust"), h("div", { class: "grid2" }, [
          field("Check a copy automatically", auto, "Reads a rotating sample of up to 40 files back and compares them. Files over 256 MB are never compared.", "j-auto"),
          field("Alert after this many days without a successful run", stale, "0 switches it off. Never sooner than twice the schedule above.", "j-stale")]),
          h("p", { class: "hint checkline" }, [(j.verify ? "Last check: " + j.verify.message + ", " + when(j.verify.at) + ". " : "Not checked yet. "), nextCheckText(j) + ". ", coverageText(j)]),
          j.icloud && j.icloud.evicted ? h("div", { class: "notice info evicted" }, [h("strong", null, j.icloud.evicted + " files evicted by macOS Optimize Storage. "), j.icloud.note]) : null]),
        field("Skip files matching (one per line)", excl, "Patterns such as *.tmp or cache.", "j-excl"),
        h("div", { class: "grid2" }, [field("Keep deleted files for (days)", keep, "Only in the default mode.", "j-keep"), field("Size limit for this folder (GB)", maxgb, null, "j-max")]),
        h("div", { class: "field" }, [h("label", { class: "check", for: "j-mirror" }, [mirror, h("span", null, "Plain mirror: delete from iCloud when I delete at the source (nothing is kept)")])]),
        h("div", { class: "field" }, [h("label", { class: "check", for: "j-enabled" }, [enabled, h("span", null, "Enabled (untick to pause)")])]),
        h("div", { class: "row" }, [h("button", { type: "submit", class: "btn primary" }, "Save"), h("a", { class: "btn", href: "#/folders" }, "Cancel")])]);
      var delCopy = h("input", { type: "checkbox", id: "j-del" });
      var danger = h("section", { class: "card mt24" }, [h("h2", { class: "mt0" }, "Remove this folder"),
        h("p", null, "Stops keeping it up to date. By default the copy already in iCloud stays where it is."),
        h("div", { class: "field" }, [h("label", { class: "check", for: "j-del" }, [delCopy, h("span", null, "Also move the iCloud copy to .removed (deleted for good after 30 days)")])]),
        h("button", { type: "button", class: "btn danger", onclick: function () {
          confirmDialog("Remove " + j.name + "?", "Your original files are not touched.", "Remove", true).then(function (yes) {
            if (!yes) return;
            return api("POST", "/api/jobs/" + encodeURIComponent(j.name) + "/remove", { delete_copy: delCopy.checked }).then(function (r) { toast(r.message); location.hash = "#/folders"; });
          }).catch(fail);
        } }, "Remove folder")]);
      var none = function () { return Promise.resolve(); };
      var work = h("div", { class: "stack" });          // the progress bar, from the server
      var watch = pageWatch(j.name, work);
      var kick = function () { setTimeout(watch.kick, 300); };
      var runCard = h("section", { class: "card", "aria-labelledby": "ja-h" }, [h("h2", { id: "ja-h", class: "mt0" }, "Run now"), work,
        h("div", { class: "row" }, [
          h("button", { type: "button", class: "btn primary", id: "job-sync", "data-focus": "sync:" + j.name, disabled: hasWork(j) || !j.enabled, onclick: function () { kick(); startSync(j, shrank(j), none); } }, shrank(j) ? "Review: the source shrank" : "Sync now"),
          h("button", { type: "button", class: "btn", id: "job-preview", "data-focus": "preview:" + j.name, disabled: hasWork(j), onclick: function () { kick(); startPreview(j, false, none); } }, "Preview next sync"),
          h("button", { type: "button", class: "btn", id: "job-check", "data-focus": "check:" + j.name, disabled: hasWork(j), onclick: function (e) { kick(); startCheck(j, e.currentTarget, none); } }, "Check a copy")]),
        h("p", { class: "hint" }, "Preview reads the source and the iCloud folder and tells you what a sync would add, update and move to .deleted, and whether a guard would refuse it. It changes nothing."),
        j.enabled ? null : h("p", { class: "hint" }, "This folder is paused, so it cannot be synced until it is enabled below. A preview still works.")]);
      setView(j.name + " \u00b7 Folder settings", [backLink("#/folders", "Back to Folders"), h("h1", null, j.name), h("p", { class: "lead" }, "Folder settings"), runCard, form, danger]);
    }).catch(function (e) { fail(e); location.hash = "#/folders"; });
  }

  // The top-bar entry: pick the folder whose kept files you want to see.
  function pageDeletedIndex() {
    var box = h("div", { class: "stack" });
    setView("Deleted files", [h("h1", null, "Deleted files"), h("p", { class: "lead" }, "Pick a folder to see what Keepsake kept when a file was replaced or removed at the source, and to restore it."), box]);
    loadStatus().then(function (s) {
      if (!s.jobs.length) { box.appendChild(h("p", { class: "muted" }, "No folders yet. Add one on the Folders page.")); return; }
      box.appendChild(h("ul", { class: "plain", "aria-label": "Folders" }, s.jobs.map(function (j) {
        return h("li", null, [h("strong", null, j.name), h("a", { class: "btn", href: "#/deleted/" + encodeURIComponent(j.name), "aria-label": "Deleted files of " + j.name }, "Deleted files")]);
      })));
    }).catch(fail);
  }

  function pageDeleted(name) {
    var box = h("div", { class: "stack" });
    var result = h("div", { id: "restore-result", role: "status", "aria-live": "polite" });
    var work = h("div", { class: "stack" });          // the progress bar of a restore, from the server
    var watch = pageWatch(name, work);
    var base = "/api/jobs/" + encodeURIComponent(name);
    setView("Deleted files \u00b7 " + name, [backLink("#/deleted", "Back to Deleted files"), h("h1", null, "Deleted files"), h("p", { class: "lead" }, "Kept for " + name + " when a file was replaced or removed at the source. Restore puts a copy back in iCloud, in a folder called .restored inside this folder's copy. Your original files are never written."), work, result, box]);
    function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
    function say(text, bad) {
      while (result.firstChild) result.removeChild(result.firstChild);
      result.appendChild(h("div", { class: "notice " + (bad ? "bad" : "info") }, text));
    }
    function draw(d) {
      while (box.firstChild) box.removeChild(box.firstChild);
      if (!d.entries.length) { box.appendChild(h("p", { class: "muted" }, "Nothing has been kept yet. Replaced and deleted files appear here after a run.")); return; }
      if (d.truncated) box.appendChild(h("div", { class: "notice" }, "Only the first " + d.entries.length + " files are listed. Restore all still works on everything that is kept."));
      if (d.mode !== "mirror-keep-deleted") box.appendChild(h("div", { class: "notice info" }, "This folder is in plain mirror mode: nothing new is kept. These are older files that have not expired yet."));
      var groups = [], byDate = {};
      d.entries.forEach(function (e) {
        if (!byDate[e.date]) { byDate[e.date] = { date: e.date, entries: [], bytes: 0 }; groups.push(byDate[e.date]); }
        byDate[e.date].entries.push(e); byDate[e.date].bytes += e.size;
      });
      box.appendChild(h("div", { class: "row between" }, [
        h("p", { class: "muted mt0" }, plural(groups.length, "date") + " with kept files."),
        h("button", { type: "button", class: "btn", id: "restore-everything", onclick: function (ev) { restoreMany(null, ev.currentTarget); } }, "Restore everything kept")]));
      groups.forEach(function (g) {
        var rows = g.entries.map(function (e) {
          return h("li", null, [
            h("span", { class: "muted" }, fmtBytes(e.size) + ", " + e.days_left + (e.days_left === 1 ? " day" : " days") + " left"),
            h("div", { class: "row between" }, [h("strong", null, e.path),
              h("button", { type: "button", class: "btn", "aria-label": "Restore " + e.path + " from " + e.date, onclick: function (ev) { restore(e, ev.currentTarget); } }, "Restore")])]);
        });
        box.appendChild(h("section", { class: "group", "data-date": g.date, "aria-labelledby": "g-" + g.date }, [
          h("div", { class: "row between" }, [
            h("div", null, [h("h2", { id: "g-" + g.date, class: "mt0" }, h("time", { datetime: g.date }, "Kept on " + g.date)),
              h("p", { class: "muted mt0" }, plural(g.entries.length, "file") + ", " + fmtBytes(g.bytes) + (d.truncated ? " listed" : ""))]),
            h("button", { type: "button", class: "btn", "aria-label": "Restore all from " + g.date, onclick: function (ev) { restoreMany(g.date, ev.currentTarget); } }, "Restore all from this date")]),
          h("ul", { class: "log", "aria-label": "Deleted files kept on " + g.date }, rows)]));
      });
    }
    function restore(e, btn) {
      confirmDialog("Restore this file?", "A copy of " + e.path + " from " + e.date + " goes into .restored/" + e.date + " in this folder's iCloud copy. The original stays in .deleted and your source is not touched.", "Restore").then(function (yes) {
        if (!yes) return;
        btn.disabled = true;
        setTimeout(watch.kick, 300);
        return api("POST", base + "/restore", { date: e.date, path: e.path }).then(function (r) { toast(r.message); say(r.message[0].toUpperCase() + r.message.slice(1) + "."); });
      }).catch(fail).then(function () { btn.disabled = false; });
    }
    // One date, or everything kept: the dialog shows the count and size the server
    // will act on, and the server refuses if that count has changed by then.
    function restoreMany(date, btn) {
      btn.disabled = true;
      api("GET", base + "/restore-plan" + (date ? "?date=" + encodeURIComponent(date) : "")).then(function (plan) {
        var what = date ? "from " + date : "everything kept";
        var body = h("div", null, [
          h("p", null, "This copies " + plural(plan.files, "file") + " (" + fmtBytes(plan.bytes) + ") into " + (date ? ".restored/" + date : ".restored/<date>, one folder per date") + " in this folder's iCloud copy, with the same sub-folders. Nothing is overwritten: a name that is taken gets a number. The originals stay in .deleted and your source is not touched."),
          plan.skipped.length ? h("p", { class: "hint" }, plural(plan.skipped.length, "item") + " will be skipped because " + (plan.skipped.length === 1 ? "it is" : "they are") + " links or not plain files, for example " + plan.skipped[0].path + ".") : null]);
        return confirmDialog("Restore " + what + "?", body, "Restore " + plural(plan.files, "file")).then(function (yes) {
          if (!yes) return;
          setTimeout(watch.kick, 300);
          return api("POST", base + "/restore-batch", date ? { date: date, expect_files: plan.files } : { all: true, expect_files: plan.files })
            .then(function (r) { toast("Restored " + plural(r.restored, "file")); say(r.message[0].toUpperCase() + r.message.slice(1) + "."); });
        });
      }).catch(function (e) { fail(e); say(e.message, true); }).then(function () { btn.disabled = false; });
    }
    api("GET", base + "/deleted").then(draw).catch(function (e) {
      while (box.firstChild) box.removeChild(box.firstChild);
      box.appendChild(h("div", { class: "notice bad" }, e.message));
      box.appendChild(h("a", { class: "btn", href: "#/folders" }, "Back to folders"));
    });
  }

  // Activity: one line per entry, grouped by day. Routine lines are quiet and problems keep
  // their colour; a line opens to its full sentence (a button, so the keyboard can too). The
  // filter lives in the address (#/activity/problems/photos), written with replaceState so
  // choosing a filter does not redraw the page.
  var ACTIVITY_PAGE = 50, ACTIVITY_LOAD = 200, ACTIVITY_SYSTEM = "_system";
  function activityBad(e) { return !!e.level && e.level !== "info"; }
  function activityDay(iso) {
    var d = new Date(iso);
    return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
  }
  function activityDayTitle(day) {
    var now = new Date(), y = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    if (day === activityDay(now.toISOString())) return "Today";
    if (day === activityDay(y.toISOString())) return "Yesterday";
    return dayLabel(day);
  }
  function activityTime(iso) { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }); }
  function pageActivity() {
    var parts = location.hash.replace(/^#\/?/, "").split("/");
    var filter = parts[1] === "problems" ? "problems" : "all";
    var folder = parts[2] ? decodeURIComponent(parts[2]) : "";
    var entries = [], shown = ACTIVITY_PAGE, closedDays = {}, openRows = {};
    var list = h("div", { class: "alog", id: "alog" });
    var more = h("div", { class: "row" });
    var last = h("ul", { class: "log" });
    var seg = h("div", { class: "themeseg alog-seg", role: "group", "aria-label": "Which entries" });
    var pick = h("select", { id: "alog-folder", "aria-label": "Folder" });
    var count = h("p", { class: "hint alog-count", role: "status", "aria-live": "polite" });
    function remember() {
      var tail = (filter !== "all" || folder) ? "/" + filter + (folder ? "/" + encodeURIComponent(folder) : "") : "";
      history.replaceState(null, "", "#/activity" + tail);
    }
    function drawSeg() {
      while (seg.firstChild) seg.removeChild(seg.firstChild);
      [["all", "All"], ["problems", "Problems only"]].forEach(function (o) {
        seg.appendChild(h("button", { type: "button", "aria-pressed": filter === o[0] ? "true" : "false", onclick: function () {
          filter = o[0]; shown = ACTIVITY_PAGE; remember(); drawSeg(); render();
        } }, o[1]));
      });
    }
    function drawPick() {
      var seen = {}, names = [], system = false;
      entries.forEach(function (e) { if (e.job) { if (!seen[e.job]) { seen[e.job] = 1; names.push(e.job); } } else system = true; });
      names.sort();
      while (pick.firstChild) pick.removeChild(pick.firstChild);
      pick.appendChild(h("option", { value: "", selected: !folder }, "All folders"));
      names.forEach(function (n) { pick.appendChild(h("option", { value: n, selected: folder === n }, n)); });
      if (system) pick.appendChild(h("option", { value: ACTIVITY_SYSTEM, selected: folder === ACTIVITY_SYSTEM }, "Keepsake itself"));
    }
    function matches(e) {
      if (filter === "problems" && !activityBad(e)) return false;
      if (folder === ACTIVITY_SYSTEM) return !e.job;
      return !folder || e.job === folder;
    }
    function row(e) {
      var bad = activityBad(e), key = e.at + "|" + (e.job || "") + "|" + e.message;
      var li = h("li", { class: "alog-row " + (bad ? "bad" : "ok") + (openRows[key] ? " open" : "") });
      var btn = h("button", { type: "button", class: "alog-line", "aria-expanded": openRows[key] ? "true" : "false", onclick: function () {
        openRows[key] = !openRows[key];
        li.classList.toggle("open", !!openRows[key]);
        btn.setAttribute("aria-expanded", openRows[key] ? "true" : "false");
      } }, [
        h("time", { class: "alog-time", datetime: e.at }, activityTime(e.at)),
        h("span", { class: "alog-dot", "aria-hidden": "true" }),
        h("span", { class: "sr" }, bad ? "Problem. " : "Done. "),
        h("span", { class: "alog-tag" }, e.job || "Keepsake"),
        h("span", { class: "alog-msg" }, e.message)]);
      li.appendChild(btn);
      li.appendChild(h("div", { class: "alog-full" }, absTime(e.at)));
      return li;
    }
    function render() {
      var rows = entries.filter(matches), visible = rows.slice(0, shown);
      while (list.firstChild) list.removeChild(list.firstChild);
      while (more.firstChild) more.removeChild(more.firstChild);
      count.textContent = "";
      if (!entries.length) { list.appendChild(h("p", { class: "muted" }, "Nothing has happened yet.")); return; }
      if (!rows.length) {
        list.appendChild(h("p", { class: "muted" }, filter === "problems" ? "No problems in this log." : "Nothing for this folder."));
        return;
      }
      var days = [], byDay = {};
      visible.forEach(function (e) {
        var d = activityDay(e.at);
        if (!byDay[d]) { byDay[d] = []; days.push(d); }
        byDay[d].push(e);
      });
      days.forEach(function (d) {
        var es = byDay[d], quiet = es.filter(function (e) { return !activityBad(e); }).length, closed = !!closedDays[d];
        var ul = h("ul", { class: "alog-rows", id: "alog-" + d });
        es.forEach(function (e) { ul.appendChild(row(e)); });
        var sec = h("section", { class: "alog-day" + (closed ? " closed" : "") });
        var head = h("h3", { class: "alog-head" }, h("button", { type: "button", "aria-expanded": closed ? "false" : "true", "aria-controls": "alog-" + d, onclick: function () {
          closedDays[d] = !closedDays[d];
          sec.classList.toggle("closed", !!closedDays[d]);
          head.firstChild.setAttribute("aria-expanded", closedDays[d] ? "false" : "true");
        } }, [h("span", { class: "alog-day-name" }, activityDayTitle(d)), h("span", { class: "alog-day-n" }, (es.length === 1 ? "1 entry" : es.length + " entries") + (quiet < es.length ? ", " + (es.length - quiet) + " with problems" : ""))]));
        sec.appendChild(head); sec.appendChild(ul);
        list.appendChild(sec);
      });
      count.textContent = "Showing " + visible.length + " of " + rows.length + (entries.length >= ACTIVITY_LOAD ? " (the latest " + ACTIVITY_LOAD + " are loaded)" : "") + ".";
      if (rows.length > visible.length) {
        more.appendChild(h("button", { type: "button", class: "btn", id: "alog-more", onclick: function () { shown += ACTIVITY_PAGE; render(); } },
          "Show " + Math.min(ACTIVITY_PAGE, rows.length - visible.length) + " more (" + (rows.length - visible.length) + " left)"));
      }
    }
    pick.addEventListener("change", function () { folder = pick.value; shown = ACTIVITY_PAGE; remember(); render(); });
    drawSeg();
    setView("Activity", [h("div", { class: "row between" }, [h("div", null, [h("h1", null, "Activity"), h("p", { class: "lead" }, "What Keepsake did, newest first.")]), h("button", { type: "button", class: "btn", onclick: load }, "Refresh")]),
      h("h2", { class: "mt0" }, "What changed on the last run"), last,
      h("h2", null, "Log"), h("div", { class: "alog-bar" }, [seg, pick]), count, list, more]);
    function load() {
      loadStatus().then(function (s) {
        while (last.firstChild) last.removeChild(last.firstChild);
        var changed = s.jobs.filter(function (j) { return j.changes && j.changes.summary; });
        if (!changed.length) last.appendChild(h("li", { class: "muted" }, "No folder has been copied yet."));
        changed.forEach(function (j) {
          last.appendChild(h("li", null, [h("time", { datetime: j.last_run, title: j.last_run }, absTime(j.last_run)), h("strong", null, j.name), changesBlock(j)]));
        });
      }).catch(fail);
      api("GET", "/api/activity?limit=" + ACTIVITY_LOAD).then(function (d) {
        entries = d.entries || [];
        drawPick();
        render();
      }).catch(fail);
    }
    load();
  }

  // Settings card: change the page password. Self-contained (its own ids, no shared
  // state beyond `state.authRequired`) so it can move to any tab. Null when no password is set.
  function passwordCard() {
    if (!state.authRequired) return null;
    var cur = h("input", { type: "password", id: "pw-current", autocomplete: "current-password", required: true });
    var nw = h("input", { type: "password", id: "pw-new", autocomplete: "new-password", minlength: "10", required: true });
    var again = h("input", { type: "password", id: "pw-again", autocomplete: "new-password", minlength: "10", required: true });
    var out = h("div", { id: "pw-out", role: "status", "aria-live": "polite" });
    return h("form", { class: "stack", id: "pw-form", novalidate: true, onsubmit: function (e) {
      e.preventDefault();
      while (out.firstChild) out.removeChild(out.firstChild);
      var say = function (cls, t) { out.appendChild(h("div", { class: "notice " + cls }, t)); };
      if (nw.value.length < 10) { say("bad", "The new password needs at least 10 characters."); return; }
      if (nw.value !== again.value) { say("bad", "The two new passwords are not the same."); return; }
      api("POST", "/api/password", { current: cur.value, new: nw.value, confirm: again.value }).then(function (r) {
        cur.value = nw.value = again.value = "";
        say("info", r.message);
        toast("Password changed");
      }).catch(function (x) { say("bad", x.message); });
    } }, [h("h2", null, "Change password"),
      h("p", { class: "hint mt0" }, "Takes effect at once and signs out every other browser. If you forget it, run the reset command in the security notes."),
      field("Current password", cur, null, "pw-current"), field("New password", nw, "At least 10 characters.", "pw-new"),
      field("New password again", again, null, "pw-again"),
      h("div", { class: "row" }, h("button", { type: "submit", class: "btn", id: "pw-save" }, "Change password")), out]);
  }

  // ---- settings tabs -------------------------------------------------------
  // A tab bar with automatic activation (arrow keys, Home and End move and open). Every panel
  // stays in the page, hidden and not removed, so what was typed in one tab is still there when
  // the person comes back. The open tab lives in the address (#/settings/alerts); it is written
  // with replaceState, because setting location.hash would redraw the page and drop the edits.
  function tabbed(items, wanted) {
    var ids = items.map(function (it) { return it.id; });
    var list = h("div", { class: "tablist", role: "tablist", "aria-label": "Settings sections" });
    var tabs = {}, panels = {};
    var active = ids.indexOf(wanted) >= 0 ? wanted : ids[0];
    function select(id, focus) {
      active = id;
      ids.forEach(function (x) {
        var on = x === id;
        tabs[x].setAttribute("aria-selected", on ? "true" : "false");
        tabs[x].tabIndex = on ? 0 : -1;
        panels[x].hidden = !on;
      });
      history.replaceState(null, "", "#/settings/" + id);
      if (focus) tabs[id].focus();
      tabs[id].scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    items.forEach(function (it) {
      tabs[it.id] = h("button", { type: "button", role: "tab", id: "tab-" + it.id, class: "tab", "aria-controls": "panel-" + it.id,
        onclick: function () { select(it.id, false); } }, it.label);
      panels[it.id] = h("section", { role: "tabpanel", id: "panel-" + it.id, class: "tabpanel", tabindex: "0", "aria-labelledby": "tab-" + it.id }, it.nodes);
      list.appendChild(tabs[it.id]);
    });
    list.addEventListener("keydown", function (e) {
      var n = ids.length, i = ids.indexOf(active), to = -1;
      if (e.key === "ArrowRight") to = (i + 1) % n;
      else if (e.key === "ArrowLeft") to = (i + n - 1) % n;
      else if (e.key === "Home") to = 0;
      else if (e.key === "End") to = n - 1;
      if (to < 0) return;
      e.preventDefault();
      select(ids[to], true);
    });
    select(active, false);
    return { nodes: [list, ids.map(function (x) { return panels[x]; })], tabs: tabs, active: function () { return active; } };
  }

  var LIMIT_IDS = ["s-max", "s-free", "s-shrink", "s-pause", "s-mbps"];
  var ALERT_IDS = ["s-hook", "s-clear", "s-ev-failed", "s-ev-stale", "s-ev-recovered"];
  function readFields(ids) {
    var out = {};
    ids.forEach(function (id) { var el = document.getElementById(id); if (el) out[id] = el.type === "checkbox" ? el.checked : el.value; });
    return out;
  }
  function writeFields(values) {
    Object.keys(values).forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      if (el.type === "checkbox") el.checked = values[id]; else el.value = values[id];
    });
  }

  function pageSettings() {
    loadStatus().then(function () { return api("GET", "/api/settings"); }).then(function (s) {
      var num = function (id, label, v, min, max, hint) { return field(label, h("input", { type: "number", id: id, min: min, max: max, value: String(v) }), hint, id); };
      var hook = h("input", { type: "url", id: "s-hook", placeholder: s.webhook_set ? "Set (" + s.webhook_host + "). Leave blank to keep it." : "https://example.com/hook", autocomplete: "off" });
      var clear = h("input", { type: "checkbox", id: "s-clear" });
      var EVENTS = [["failed", "When a copy fails, is refused, or a check finds a difference"], ["stale", "When a folder has had no successful run for several days"], ["recovered", "When a failed or silent folder works again"]];
      var evs = EVENTS.map(function (e) { return check("s-ev-" + e[0], e[1], s.notify_on.indexOf(e[0]) >= 0); });
      var testOut = h("div", { id: "test-out", role: "status", "aria-live": "polite" });
      var testBtn = h("button", { type: "button", class: "btn", id: "test-alert", onclick: function () {
        testBtn.disabled = true;
        while (testOut.firstChild) testOut.removeChild(testOut.firstChild);
        api("POST", "/api/settings/test-alert", {}).catch(function (e) { return e.data || { ok: false, message: e.message, results: [] }; }).then(function (r) {
          testOut.appendChild(h("div", { class: "notice " + (r.ok ? "info" : "bad") }, r.message));
          if (r.results && r.results.length) testOut.appendChild(h("ul", { class: "plain", "aria-label": "Test alert results" }, r.results.map(function (x) {
            return h("li", null, [h("strong", null, x.channel), x.ok ? h("span", { class: "badge ok" }, "Delivered") : h("span", { class: "badge failed" }, "Failed: " + x.error)]);
          })));
        }).then(function () { testBtn.disabled = false; });
      } }, "Send test alert");
      var chan = h("table", { class: "settings-table", "aria-label": "Alert channels" }, [h("thead", null, h("tr", null, ["Channel", "State"].map(function (x) { return h("th", { scope: "col" }, x); }))),
        h("tbody", null, s.channels.map(function (c) {
          return h("tr", null, [h("td", null, c.name), h("td", null, c.configured ? [h("span", { class: "badge ok" }, "Configured"), c.host ? " " + c.host : ""] : h("span", { class: "muted" }, "Not set"))]);
        }))]);
      // One Save settings saves the Limits and the Alerts tab together, from whichever tab it is pressed in.
      var save = function (e) {
        if (e) e.preventDefault();
        var body = { max_dest_gb: +document.getElementById("s-max").value, min_free_gb: +document.getElementById("s-free").value,
          shrink_pct: +document.getElementById("s-shrink").value, pause_ms: +document.getElementById("s-pause").value, max_mbps: +document.getElementById("s-mbps").value,
          notify_on: EVENTS.map(function (x) { return x[0]; }).filter(function (x) { return document.getElementById("s-ev-" + x).checked; }) };
        if (clear.checked) body.webhook_url = ""; else if (hook.value.trim()) body.webhook_url = hook.value.trim();
        api("PUT", "/api/settings", body).then(function (r) { settingsDraft = null; toast(r.message); pageSettings(); }).catch(fail);
      };
      var unsaved = [h("div", { class: "notice unsaved", role: "status", "aria-live": "polite", hidden: true }, ""),
        h("div", { class: "notice unsaved", role: "status", "aria-live": "polite", hidden: true }, "")];
      var saveRow = function (i) { return h("div", { class: "row" }, [h("button", { type: "submit", class: "btn primary" }, "Save settings"), unsaved[i]]); };
      var limitsForm = h("form", { class: "stack", novalidate: true, onsubmit: save }, [
        h("fieldset", null, [h("legend", null, "Safety limits"), h("div", { class: "grid2" }, [
          num("s-max", "Largest folder in iCloud (GB)", s.max_dest_gb, 1, 100000, "A copy that would go over this is refused."),
          num("s-free", "Keep this much disk free (GB)", s.min_free_gb, 0, 100000, "A copy that would leave the Mac with less is refused."),
          num("s-shrink", "Shrink guard (%)", s.shrink_pct, 1, 99, "If the source loses more than this in files, size or remembered paths since the last good run, nothing is changed."),
          num("s-pause", "Pause between files (ms)", s.pause_ms, 0, 10000, "Be gentle with iCloud uploads."),
          num("s-mbps", "Speed limit (megabit/s, 0 = none)", s.max_mbps, 0, 10000)])]),
        saveRow(0)]);
      var alertsForm = h("form", { class: "stack", novalidate: true, onsubmit: save }, [
        h("fieldset", null, [h("legend", null, "Notifications (optional)"),
          field("Webhook address", hook, "Keepsake sends a small JSON message here. The address is stored on this machine and never shown again.", "s-hook"),
          h("div", { class: "field" }, [h("label", { class: "check", for: "s-clear" }, [clear, h("span", null, "Remove the saved webhook")])]), evs,
          h("p", { class: "hint" }, "ntfy, Pushover, e-mail and MQTT are set up with environment variables or secret files, never here. Their addresses, keys and passwords are not shown by this page or by the API; the table below says only whether each is configured.")]),
        saveRow(1)]);
      var pause = s.pause;
      var goPause = function (spec, label) {
        return api("PUT", "/api/settings", { pause: spec }).then(function (r) { toast(label); return pageSettings(); }).catch(fail);
      };
      var custom = h("input", { type: "datetime-local", id: "p-custom" });
      var qOn = h("input", { type: "checkbox", id: "q-on", checked: !!pause.quiet });
      var qStart = h("input", { type: "time", id: "q-start", value: pause.quiet ? pause.quiet.start : "22:00" });
      var qEnd = h("input", { type: "time", id: "q-end", value: pause.quiet ? pause.quiet.end : "07:00" });
      var pauseBox = h("section", { class: "card stack", "aria-labelledby": "pause-h" }, [
        h("h2", { id: "pause-h", class: "mt0" }, "Pause everything"),
        h("p", { id: "pause-state", role: "status", "aria-live": "polite", class: pause.paused ? "notice" : "muted" }, pause.paused ? pause.message : "Nothing is paused."),
        h("p", { class: "hint" }, "While paused, scheduled syncs and scheduled copy checks do not start, and nothing is called late or silent. A sync that is already running is not interrupted. Sync now and Preview still work when you ask."),
        h("div", { class: "row" }, [
          h("button", { type: "button", class: "btn", id: "p-1h", onclick: function () { goPause({ preset: "1h" }, "Paused for an hour"); } }, "1 hour"),
          h("button", { type: "button", class: "btn", id: "p-tomorrow", onclick: function () { goPause({ preset: "tomorrow" }, "Paused until tomorrow 08:00"); } }, "Until tomorrow 08:00"),
          h("button", { type: "button", class: "btn", id: "p-1w", onclick: function () { goPause({ preset: "1w" }, "Paused for a week"); } }, "1 week"),
          pause.pause_until ? h("button", { type: "button", class: "btn primary", id: "p-resume", onclick: function () { goPause(null, "Resumed"); } }, "Resume now") : null]),
        h("div", { class: "field" }, [h("label", { for: "p-custom" }, "Pause until a date and time"), custom,
          h("p", { class: "hint" }, "In the " + s.timezone + " time zone, like the schedules. Up to a year ahead.")]),
        h("button", { type: "button", class: "btn", id: "p-custom-go", onclick: function () {
          if (!custom.value) { toast("Pick a date and time first."); return; }
          goPause({ until: custom.value }, "Paused until " + custom.value.replace("T", " "));
        } }, "Pause until this time"),
        h("h2", { class: "mt24" }, "Quiet hours"),
        h("p", { class: "hint mt0" }, "A daily window in which scheduled syncs and checks do not start (for example overnight, so a big copy does not compete with your connection). A sync already running is never stopped, and the hours are not counted when Keepsake decides a folder is late. Times use the " + s.timezone + " time zone."),
        h("div", { class: "field" }, [h("label", { class: "check", for: "q-on" }, [qOn, h("span", null, "Use quiet hours")])]),
        h("div", { class: "grid2" }, [field("From", qStart, "A window may cross midnight, such as 22:00 to 07:00.", "q-start"), field("Until", qEnd, null, "q-end")]),
        h("button", { type: "button", class: "btn", id: "q-save", onclick: function () {
          api("PUT", "/api/settings", { quiet_hours: qOn.checked ? { start: qStart.value, end: qEnd.value } : null })
            .then(function () { toast(qOn.checked ? "Quiet hours saved" : "Quiet hours are off"); return pageSettings(); }).catch(fail);
        } }, "Save quiet hours")]);
      var confirmBlock = state.status && state.status.icloud_available && !state.status.anchor_ok ? h("div", { class: "card" }, [
          h("p", { class: "mt0" }, "First run: confirm that this is your real Keepsake folder in iCloud Drive, not an empty folder Docker made because iCloud Drive was not available. Keepsake then writes one small marker file in it and will copy nothing until that exists."),
          h("button", { type: "button", class: "btn primary", id: "confirm-icloud", onclick: function () {
            confirmDialog("Confirm the iCloud folder?", "Check in Finder that iCloud Drive shows a folder named Keepsake and that it is the folder you shared with Keepsake.", "Yes, this is the right folder").then(function (yes) {
              if (!yes) return;
              return api("POST", "/api/icloud/confirm", {}).then(function (r) { toast(r.message); return pageSettings(); });
            }).catch(fail);
          } }, "Confirm iCloud folder")]) : (state.status && state.status.anchor_ok ? h("p", { class: "muted" }, "✓ Confirmed: Keepsake wrote its marker file here.") : null);
      var items = [
        { id: "limits", label: "Limits", nodes: [limitsForm] },
        { id: "alerts", label: "Alerts", nodes: [alertsForm, h("h2", null, "Alert channels"), chan,
          h("div", { class: "row" }, [testBtn, h("span", { class: "hint" }, "Sends one test message through every configured channel and says which worked.")]), testOut] },
        { id: "pause", label: "Pause", nodes: [pauseBox] },
        { id: "locations", label: "Locations", nodes: [
          h("h2", { class: "mt0" }, "iCloud Drive folder"), h("p", null, [h("code", null, s.icloud_dir), "  ", state.status && state.status.icloud_available ? h("span", { class: "badge ok" }, "Available") : h("span", { class: "badge failed" }, "Not available")]),
          confirmBlock, locationsCard().el] },
        { id: "diagnostics", label: "Diagnostics", nodes: [
          h("h2", { class: "mt0" }, "Diagnostics"),
          h("p", { class: "hint mt0" }, "A plain text file for a bug report: versions, which alert channels are set, each folder's last run and check, and the last log lines. It leaves out passwords and tokens, cuts any web address down to its host name, leaves out file names from your folders, and shows your home folder as ~. Read it before you share it."),
          h("div", { class: "row" }, h("a", { class: "btn", id: "diagnostics", href: "/api/diagnostics", download: "keepsake-diagnostics.txt" }, "Download diagnostics")),
          h("p", { class: "muted" }, "Times use the " + s.timezone + " time zone (set TZ to change it).")] }];
      var pw = passwordCard();
      if (pw) items.push({ id: "security", label: "Security", nodes: [pw] });
      items.push({ id: "help", label: "Help", nodes: helpCard() });
      var tb = tabbed(items, location.hash.replace(/^#\/?/, "").split("/")[1]);
      setView("Settings", [h("h1", null, "Settings"), h("p", { class: "lead mt0" }, "Limits, alerts, pause, the places Keepsake may read, the page password and help."),
        tb.nodes]);
      // Unsaved edits: compared with what the server holds, kept across tab switches and redraws.
      var baseline = readFields(LIMIT_IDS.concat(ALERT_IDS));
      var differs = function (ids) { var now = readFields(ids); return ids.some(function (id) { return now[id] !== baseline[id]; }); };
      var refresh = function () {
        var panes = [["limits", "Limits", LIMIT_IDS], ["alerts", "Alerts", ALERT_IDS]], names = [];
        panes.forEach(function (p) {
          var dirty = differs(p[2]);
          tb.tabs[p[0]].classList.toggle("dirty", dirty);
          if (dirty) { names.push(p[1]); tb.tabs[p[0]].setAttribute("aria-label", p[1] + ", unsaved changes"); } else tb.tabs[p[0]].removeAttribute("aria-label");
        });
        settingsDraft = names.length ? readFields(LIMIT_IDS.concat(ALERT_IDS)) : null;
        unsaved.forEach(function (n) {
          n.hidden = !names.length;
          n.textContent = names.length ? "You have unsaved changes in " + names.join(" and ") + ". Save settings saves Limits and Alerts together." : "";
        });
      };
      if (settingsDraft) writeFields(settingsDraft);
      [limitsForm, alertsForm].forEach(function (f) { f.addEventListener("input", refresh); f.addEventListener("change", refresh); });
      refresh();
      window.requestAnimationFrame(function () { var t = document.getElementById("tab-" + tb.active()); if (t) t.scrollIntoView({ block: "nearest", inline: "nearest" }); });
    }).catch(fail);
  }

  // The help text lives in the Help tab of Settings. It is plain content with short headings,
  // so it needs no state; a person who has not finished the guided setup never reaches it,
  // because the wizard is then the only page.
  function helpCard() {
    var code = function (t) { return h("pre", null, h("code", null, t)); };
    return [h("h2", { class: "mt0" }, "Getting started"),
      h("p", null, "iCloud Drive exists as a normal folder only on a Mac, so Keepsake runs natively on the Mac or in Docker Desktop for Mac with the iCloud Drive folder shared into the container. A new install shows only the guided setup: confirm the iCloud folder, choose the folders to keep, preview the first copy, make it and check it. Until it is finished no other page opens; after that this Help lives here, in Settings."),
      h("div", { class: "notice info" }, "iCloud Drive is a sync service, not a backup. A deletion syncs everywhere. That is why the default mode keeps replaced and deleted files for 30 days under .deleted."),
      h("ol", { class: "steps" }, [
        h("li", null, ["Create the folder ", h("code", null, "~/Library/Mobile Documents/com~apple~CloudDocs/Keepsake"), " (Finder: iCloud Drive, new folder named Keepsake)."]),
        h("li", null, "Docker Desktop, Settings, Resources, File sharing: make sure that folder (or your home folder) is shared. On first use macOS may ask Docker for access to iCloud Drive; allow it."),
        h("li", null, "Mount that folder at /icloud (read-write) and the folders to back up read-only, for example under /sources.")]),
      code("-v \"$HOME/Library/Mobile Documents/com~apple~CloudDocs/Keepsake:/icloud\"\n-v \"$HOME/Documents:/sources/documents:ro\""),
      h("p", null, "Confirm the iCloud folder once, in the guided setup or in Settings, Locations tab, after checking in Finder that iCloud Drive shows the Keepsake folder. Keepsake writes one marker file there and refuses to copy anything until it exists, so an empty folder Docker made while iCloud Drive was unavailable is never mistaken for the real one."),
      h("h2", null, "Locations and folders"),
      h("p", null, "A location is a place Keepsake may read from, always read-only. The ones the container starts with are shown as built in; Settings, Locations tab adds and removes the others, as long as they sit inside a folder mounted into the container. A location a folder uses cannot be removed."),
      h("ul", null, [
        h("li", null, "Add folder lists the folders of a location with a tick box each. Tick several, open a folder to look inside without choosing it, and press Add. Your ticks stay while you move between folders and locations."),
        h("li", null, "Refresh re-reads the list, so a folder you made a moment ago appears. Your place and your ticks are kept."),
        h("li", null, "Keepsake never copies when the source looks empty, shrank a lot or could not be fully read: an unmounted share looks exactly like that.")]),
      h("h2", null, "Destination folder"),
      h("p", null, "Everything is copied into iCloud Drive, Keepsake, then the destination folder you name (letters, digits and hyphens; upper and lower case are kept). Several folders added together go into one destination, each in its own subfolder named after it; two with the same name become Holidays and Holidays-2."),
      h("ul", null, [
        h("li", null, "Pick a destination folder that already exists from the list, or type a new name."),
        h("li", null, "Keepsake refuses a folder in iCloud it did not create unless you tick the box that says to take it over."),
        h("li", null, "It writes only inside its own folder in iCloud and never follows a link out of it.")]),
      h("h2", null, "Copies and checks"),
      h("ul", null, [
        h("li", null, "Each folder has a schedule (every N hours, or daily at a time) and Sync now. A new folder is a safe first run: it copies and checks, and removes nothing that was already in iCloud."),
        h("li", null, "Preview next sync reads the source and the iCloud folder and tells you what a sync started now would add, update and move to .deleted, with up to 50 example files, and whether a guard would refuse it. It changes nothing anywhere, and its answer is a prediction: files can change before a real sync."),
        h("li", null, "Every long action shows a progress bar with what it is doing, how many files and how much data so far, and the folder it is in. After 30 seconds without news it says so plainly, which is not an error on a slow network share. After 5 minutes it offers Stop this run, which stops between files and never touches your originals."),
        h("li", null, "Check a copy reads up to 40 files back from the source and from the iCloud folder and compares them. Each check carries on after the last file the previous one read, so every file is looked at after about (files / 40) checks. Files that changed since the run, were evicted or are over 256 MB are skipped, not compared. A scheduled check can run daily, weekly or monthly."),
        h("li", null, "A file that changes while it is being read is not copied half-read; it is retried on the next run."),
        h("li", null, "Each bar in a folder's Last 14 nights opens that night: what it did and up to 50 example files."),
        h("li", null, "Settings, Pause tab: pause everything for 1 hour, until tomorrow 08:00, 1 week or a date and time, and set Quiet hours, a daily window such as 22:00 to 07:00. While paused, scheduled syncs and checks do not start and nothing is reported as late. A running sync is not stopped, and Sync now and Preview still work.")]),
      h("h2", null, "Restore and deleted files"),
      h("ul", null, [
        h("li", null, "A replaced or deleted file is moved to .deleted, dated, and removed after 30 days (you can change the number of days per folder). The Deleted files page lists them with days left."),
        h("li", null, "Restore copies a file into .restored inside the folder's iCloud copy. It never writes to your source, never replaces a file and skips links. Restore all from a date, and Restore everything kept, work the same way."),
        h("li", null, "The Plain mirror option deletes from iCloud when you delete at the source and keeps nothing. It is off by default."),
        h("li", null, "When macOS Optimize Storage frees space it replaces a file with a small hidden placeholder named like .photo.jpg.icloud. Keepsake counts those per folder and leaves them alone while the source is unchanged. They are not lost, but they are not on this Mac's disk: open one in Finder to download it, and a restore needs them downloaded first.")]),
      h("h2", null, "Alerts"),
      h("p", null, "Settings, Alerts tab sets a webhook and the other channels (ntfy, Pushover, e-mail, MQTT) and which events send a message: a copy that failed, a check that found a difference, and a folder with no successful run for a number of days. A channel that is not set up does nothing, so use the test button, which sends one message through every configured channel and says which worked."),
      h("h2", null, "Password"),
      h("p", null, "When the page has a password, Settings, Security tab changes it: your current password, then the new one twice, at least 10 characters. Every other browser is signed out. If you forgot it, run this on the Mac that runs Keepsake; it asks for a new one without showing it and signs everyone out:"),
      code("docker exec -it keepsake python3 -m keepsake reset-password"),
      h("h2", null, "Troubleshooting"),
      h("ul", null, [
        h("li", null, "Nothing copies and the page says the iCloud folder is not confirmed: confirm it once, as above."),
        h("li", null, "A copy is refused because the source looks empty or shrank: check that the share is mounted, then use Preview next sync before you allow a smaller source."),
        h("li", null, "A copy failed because files changed while being read: nothing was lost; the next run retries them. Exclude files that always change."),
        h("li", null, "Add location says no folder is mounted for choosing: the container sees only what is mounted into it, so mount the folder first."),
        h("li", null, "Diagnostics, in Settings, downloads a plain text file for a bug report. It leaves out passwords and tokens.")]),
      h("p", { class: "muted" }, "iCloud Drive is not end-to-end encrypted unless your Apple Account has Advanced Data Protection on. Times use the TZ time zone, like the schedules."),
      h("p", { class: "muted" }, "Keepsake for iCloud is not affiliated with or endorsed by Apple Inc. iCloud is a trademark of Apple Inc.")];
  }

  function pageLogin() {
    var pw = h("input", { type: "password", id: "pw", autocomplete: "current-password", required: true });
    var err = h("p", { class: "notice bad", role: "alert", hidden: true });
    setView("Log in", [h("form", { class: "card login stack", onsubmit: function (e) {
      e.preventDefault();
      api("POST", "/api/login", { password: pw.value }).then(function (r) { state.csrf = r.csrf; state.authed = true; boot(); }).catch(function (x) { err.textContent = x.message; err.hidden = false; pw.select(); });
    } }, [h("h1", null, "Log in"), h("p", { class: "lead" }, "Keepsake needs its password."), field("Password", pw, null, "pw"), err, h("button", { type: "submit", class: "btn primary" }, "Log in")])]);
    pw.focus();
  }

  // ---- the guided setup ----------------------------------------------------
  // Until the server says the setup is complete this wizard is the whole page:
  // no menu, no banner, no links out, no way to skip a step. The server answers
  // the same way (status.setup, and a refusal for every call outside the wizard),
  // so a typed address or a second browser lands here too. What step a person is
  // on is read from the server, so a reload or another browser resumes it.
  var WIZ_STEPS = [["confirm", "Confirm iCloud"], ["choose", "Choose folders"], ["preview", "Preview"], ["copy", "First copy"], ["done", "Done"]];
  var WIZ_SCREENS = ["confirm", "locations", "folders", "preview", "copy", "done"];
  var WIZ_STEP_OF = { confirm: 0, locations: 1, folders: 1, preview: 2, copy: 3, done: 4 };
  // The furthest screen the server's step allows, and the screen it puts a person on.
  var WIZ_MAX = { confirm: 0, folder: 2, preview: 3, "sync-check": 4, complete: 5 };
  var WIZ_START = { confirm: "confirm", folder: "locations", preview: "preview", "sync-check": "copy", complete: "done" };
  var wiz = { screen: null, seenOpen: false, message: "", focus: true, asked: {}, checked: {}, sig: "" };

  function setSetupMode(on) {
    document.body.classList.toggle("setup", on);
    var brand = document.querySelector(".brand");
    if (on) { brand.removeAttribute("href"); brand.removeAttribute("aria-label"); }
    else { brand.setAttribute("href", "#/folders"); brand.setAttribute("aria-label", "Keepsake, Folders"); }
    if (on && location.hash !== "#/setup") history.replaceState(null, "", "#/setup");
  }
  function wizIndex(screen) { return WIZ_SCREENS.indexOf(screen); }
  function wizGo(screen) { wiz.screen = screen; wiz.message = ""; wiz.focus = true; return drawWizard(); }
  function wizWhere(j) { return j.root + " / " + j.source + " → iCloud Drive / Keepsake / " + (j.group ? j.group + " / " : "") + j.dest; }
  function wizJobs(s) { return s.jobs.filter(function (j) { return j.enabled; }); }
  function wizBusy(s) { return s.jobs.some(hasWork); }

  // "Where can Keepsake read from": the Locations card used by Settings, so a
  // location can be added or removed here too. `again` runs after every change,
  // which reloads the status and with it the Continue button.
  function wizardLocations(s, again) {
    return [locationsCard({ onChange: again }).el,
      h("button", { type: "button", class: "btn", onclick: again }, "Check again")];
  }

  function wizScreen(s) {
    var su = s.setup, screen = wiz.screen;
    var jobs = wizJobs(s);
    if (screen === "confirm") {
      var confirmed = s.icloud_available && s.anchor_ok;
      return {
        title: "Confirm the iCloud folder",
        lead: confirmed ? "Confirmed: Keepsake wrote its marker file in the iCloud folder."
          : !s.icloud_available ? "Keepsake cannot see the iCloud Drive folder on this Mac, so nothing can be copied yet."
            : "Check in Finder that iCloud Drive shows a folder called Keepsake. Then confirm it once. Nothing is copied before you do.",
        body: [!s.icloud_available ? h("p", { class: "hint" }, "If macOS asked whether Docker may access that folder and you refused, open System Settings, Privacy & Security, Files and Folders (or Full Disk Access) and switch Docker on. Then press Check again.") : null,
          s.icloud_available && !s.anchor_ok ? h("p", { class: "hint" }, "Why once: if iCloud Drive was not available when Docker started, Docker may have made an empty folder in its place. Keepsake copies nothing until a person confirms that this is the real one.") : null],
        primary: confirmed ? { label: "Continue", run: function () { return wizGo("locations"); } }
          : !s.icloud_available ? { label: "Check again", run: function () { return wizRefresh(); } }
            : { label: "Confirm the iCloud folder", run: function () {
              return api("POST", "/api/icloud/confirm", {}).then(function (r) { toast(r.message); return loadStatus().then(function () { return wizGo("locations"); }); }); } },
        back: null };
    }
    if (screen === "locations") {
      var open = s.roots.some(function (r) { return r.available; });
      return {
        title: "Where can Keepsake read from?",
        lead: "Keepsake only reads from these locations, and never changes anything in them.",
        body: wizardLocations(s, function () { wizRefresh(); }),
        primary: { label: "Continue", disabled: !open, run: function () { return wizGo("folders"); } },
        reason: open ? "" : "At least one location has to be available before you can continue.",
        back: "confirm" };
    }
    if (screen === "folders") {
      var mount = h("div", { id: "wiz-add" });
      var have = s.jobs.length ? h("div", { class: "wiz-have" }, [h("h2", { class: "mt0" }, "Folders chosen so far"),
        h("ul", { class: "wiz-list", "aria-label": "Folders chosen so far" }, s.jobs.map(function (j) {
          return h("li", null, [h("strong", null, j.name), h("p", { class: "hint" }, wizWhere(j)),
            j.last_success ? null : h("button", { type: "button", class: "btn ghost", onclick: function () {
              api("POST", "/api/jobs/" + encodeURIComponent(j.name) + "/remove", { delete_copy: false }).then(function () { return wizRefresh(); }).catch(fail); } }, "Remove " + j.name)]);
        }))]) : null;
      pageAdd({ embedded: true, mount: function (nodes) { while (mount.firstChild) mount.removeChild(mount.firstChild); add(mount, nodes); }, after: function () { return wizRefresh(); } });
      return {
        title: "Choose folders and the destination folder",
        lead: "Pick what to keep. Your originals are only ever read. The destination folder is the folder inside iCloud Drive / Keepsake where the copy goes.",
        body: [have, h("h2", null, s.jobs.length ? "Add another folder" : "Add a folder"), mount],
        primary: { label: "Continue to the preview", disabled: !s.jobs.length, run: function () { return wizGo("preview"); } },
        reason: s.jobs.length ? "" : "Add at least one folder to continue.",
        back: "locations" };
    }
    if (screen === "preview") {
      var todo = jobs.filter(function (j) { return !j.last_success; });
      var undone = todo.filter(function (j) { return !j.preview; });
      var askPreview = function (list) {
        list.forEach(function (j) { wiz.asked[j.name] = Date.now(); });
        return Promise.all(list.map(function (j) { return api("POST", "/api/jobs/" + encodeURIComponent(j.name) + "/preview", { accept_shrink: false }); }))
          .then(function () { return wizRefresh(); }, function (e) { list.forEach(function (j) { delete wiz.asked[j.name]; }); wiz.message = e.message; return wizRefresh(); });
      };
      var rows = todo.map(function (j) {
        var p = j.preview, lost = wizUnstarted(j);
        // "Reading" is only ever what the SERVER says: previewing, queued or running.
        return h("li", null, [h("strong", null, j.name), " ", j.previewing ? h("span", { class: "pill running" }, "Reading") : p ? h("span", { class: "pill " + (p.would_refuse ? "bad" : "ok") }, p.would_refuse ? "Would be refused" : "Previewed") : lost ? h("span", { class: "pill bad" }, "Did not start") : h("span", { class: "pill" }, "Not previewed"),
          h("p", { class: "hint" }, wizWhere(j)),
          j.previewing ? progressWidget(j) : null,
          lost ? h("div", { class: "notice bad", role: "alert" }, [h("p", null, "The preview did not start. Try again."), h("button", { type: "button", class: "btn", onclick: function (e) { e.currentTarget.disabled = true; askPreview([j]).catch(fail); } }, "Retry")]) : null,
          p ? (p.would_refuse
            ? h("div", { class: "notice bad", role: "alert" }, p.message)
            : h("p", null, "A first copy would add " + pluralN(p.added, "file") + " (" + fmtBytes(p.bytes) + ") and change nothing that is already in iCloud. " + pluralN(p.files_total, "file") + " found in the source.")) : null]);
      });
      var allSeen = todo.length > 0 && !undone.length;
      return {
        title: "Preview the first copy",
        lead: "A preview reads your folder and the iCloud folder and tells you what a first copy would do. It changes nothing: not your originals, not iCloud.",
        body: [rows.length ? h("ul", { class: "wiz-list", "aria-label": "Folders to preview" }, rows) : h("p", { class: "muted" }, "Nothing left to preview."),
          wiz.message ? h("div", { class: "notice", role: "status" }, wiz.message) : null],
        primary: allSeen || !todo.length ? { label: "Continue", run: function () { return wizGo("copy"); } }
          : { label: wizBusy(s) ? "Reading" : "Preview the first copy", disabled: wizBusy(s), run: function () { return askPreview(undone); } },
        back: "folders" };
    }
    if (screen === "copy") {
      var waiting = jobs.filter(function (j) { return !j.last_success && j.status !== "running" && j.status !== "queued"; });
      var busy = wizBusy(s);
      var okJob = jobs.filter(function (j) { return j.last_success; })[0];
      var rowsC = jobs.map(function (j) {
        return h("li", null, [h("strong", null, j.name), " ", pill(j), h("p", { class: "hint" }, wizWhere(j)),
          (j.status === "failed" || j.status === "skipped") && j.error ? h("div", { class: "notice bad", role: "alert" }, j.error) : null,
          hasWork(j) ? progressWidget(j, { stage: { of: 2 } })
            : j.last_success ? h("p", { class: "hint" }, "Copied " + pluralN(j.files_copied, "file") + " (" + fmtBytes(j.bytes_copied) + ") in " + fmtClock(j.duration_s) + "." + (j.verify ? " " + j.verify.message + "." : "")) : null]);
      });
      var primary;
      if (busy) primary = { label: s.jobs.some(function (j) { return j.operation === "check"; }) ? "Checking" : "Copying", disabled: true, run: function () {} };
      else if (okJob) primary = { label: "Check the copy", run: function () {
        return api("POST", "/api/jobs/" + encodeURIComponent(okJob.name) + "/verify", {}).then(function (r) { toast(r.message); return wizRefresh(); },
          function (e) { wiz.message = e.message; return wizRefresh(); }); } };
      else primary = { label: waiting.some(function (j) { return j.result && j.result !== "never"; }) ? "Try the first copy again" : "Start the first copy", run: function () {
        return Promise.all(waiting.map(function (j) { return api("POST", "/api/jobs/" + encodeURIComponent(j.name) + "/sync", { accept_shrink: false }); }))
          .then(function () { return wizRefresh(); }); } };
      return {
        title: "Make the first copy and check it",
        lead: okJob ? "The copy is in the iCloud folder on this Mac. Now Keepsake reads some files back from both sides and compares them."
          : "Keepsake copies each folder into iCloud Drive and verifies it. This can take a while for a large folder; you can leave this page open.",
        body: [h("ul", { class: "wiz-list", "aria-label": "Folders being copied" }, rowsC),
          wiz.message ? h("div", { class: "notice bad", role: "alert" }, wiz.message) : null],
        primary: primary, back: busy ? null : "preview" };
    }
    // done
    return {
      title: "Setup is finished",
      lead: pluralN(s.jobs.length, "folder") + " set up. The first copy was checked: files were read back from iCloud and matched.",
      body: [h("p", { class: "hint" }, "From now on Keepsake copies on the schedule you chose. The Folders page shows each folder, its last nights and anything that needs you.")],
      primary: { label: "Open Keepsake", run: function () { wiz.seenOpen = false; setSetupMode(false); location.hash = "#/folders"; return Promise.resolve(); } },
      back: null, last: true };
  }

  function drawWizard() {
    var s = state.status;
    if (!s || !s.setup) return Promise.resolve();
    var su = s.setup;
    if (su.complete) {
      // Seen open in this page, now complete: the finished screen, once. Loaded
      // after the setup was complete: the normal page.
      if (!wiz.seenOpen) { setSetupMode(false); location.hash = "#/folders"; return Promise.resolve(); }
      wiz.screen = "done";
    } else {
      wiz.seenOpen = true;
      if (!wiz.screen || wizIndex(wiz.screen) > WIZ_MAX[su.step]) wiz.screen = WIZ_START[su.step];
    }
    setSetupMode(true);
    var cfg = wizScreen(s);
    var at = WIZ_STEP_OF[wiz.screen];
    var steps = h("ol", { class: "wiz-steps", "aria-label": "Setup progress" }, WIZ_STEPS.map(function (st, i) {
      var done = i < at || (wiz.screen === "done");
      return h("li", { class: done ? "done" : "", "aria-current": i === at ? "step" : null }, [
        h("span", { class: "tick", "aria-hidden": "true" }, done ? "✓" : String(i + 1)), h("span", null, st[1]),
        h("span", { class: "sr" }, done ? " (done)" : i === at ? " (current step)" : " (to do)")]);
    }));
    var nav = h("div", { class: "row wiz-nav" }, [
      cfg.back ? h("button", { type: "button", class: "btn", onclick: function () { wizGo(cfg.back); } }, "Back") : null,
      cfg.primary ? h("button", { type: "button", class: "btn primary", id: "wiz-primary", disabled: !!cfg.primary.disabled, "aria-describedby": cfg.reason ? "wiz-reason" : null, onclick: function (e) {
        var b = e.currentTarget; b.disabled = true;
        Promise.resolve(cfg.primary.run()).catch(fail).then(function () { if (document.body.contains(b)) b.disabled = !!cfg.primary.disabled; });
      } }, cfg.primary.label) : null,
      cfg.reason ? h("span", { class: "hint", id: "wiz-reason" }, cfg.reason) : null]);
    var heading = h("h1", { id: "wiz-h", tabindex: "-1" }, cfg.title);
    setView("Setup", [h("div", { class: "wiz" }, [steps,
      h("section", { class: "card wiz-card", "aria-labelledby": "wiz-h" }, [heading, h("p", { class: "lead" }, cfg.lead), h("div", { id: "wiz-body", class: "stack" }, cfg.body), nav])])]);
    if (wiz.focus) { wiz.focus = false; heading.focus({ preventScroll: true }); }
    clearInterval(state.timer);
    wiz.sig = wizSig(s);
    wizAutoCheck(s);
    var token = ++state.pollToken;
    if (wiz.screen === "preview" || wiz.screen === "copy") wizLoop(token);
    return Promise.resolve();
  }
  // Stage 2 of the last step starts by itself when stage 1 is done: the copy has
  // finished, nothing has been checked yet and nothing is running. Once per page, so a
  // refused check is not retried in a loop (the button is still there for a retry).
  function wizAutoCheck(s) {
    if (wiz.screen !== "copy" || s.setup.complete) return;
    s.jobs.forEach(function (j) {
      if (!j.last_success || j.verify || j.operation || hasWork(j) || wiz.checked[j.name]) return;
      wiz.checked[j.name] = true;
      api("POST", "/api/jobs/" + encodeURIComponent(j.name) + "/verify", {})
        .then(function (r) { toast(r.message); }, function (e) { wiz.message = e.message; })
        .then(function () { return wizRefresh(); });
    });
  }
  // Ask the server how it is going: every second while a preview or copy runs (the
  // numbers update in place), every five seconds otherwise, so a page that thinks
  // something is running always learns from the server that it is not.
  function wizUnstarted(j) { return !j.previewing && !j.preview && !j.last_success && wiz.asked[j.name] && Date.now() - wiz.asked[j.name] > 4000; }
  function wizSig(s) { return jobsSig(s) + "|" + s.setup.step + "|" + s.jobs.filter(wizUnstarted).map(function (j) { return j.name; }).join(","); }
  function wizLoop(token) {
    var busy = !!state.status && wizBusy(state.status);
    state.timer = setTimeout(function () {
      if (token !== state.pollToken) return;
      loadStatus().then(function (s) {
        if (token !== state.pollToken) return;
        if (wizSig(s) !== wiz.sig && !keyBusy()) return drawWizard();     // a redraw starts a fresh loop
        updateLiveProgress(s);
        wizLoop(token);
      }, function () { if (token === state.pollToken) wizLoop(token); });
    }, pollDelay(busy));
  }
  function wizRefresh() { return loadStatus().then(function () { return drawWizard(); }).catch(fail); }
  function pageSetup() { return wizRefresh(); }

  // ---- router --------------------------------------------------------------
  function route() {
    clearInterval(state.timer);
    state.pollToken += 1;
    state.stops.splice(0).forEach(function (stop) { stop(); });
    var token = route.n = (route.n || 0) + 1;
    while (view.firstChild) view.removeChild(view.firstChild);
    view.appendChild(h("p", { class: "muted" }, "Loading"));
    var hash = location.hash.replace(/^#\/?/, "") || "folders";
    var seg = hash.split("/");
    var page = seg[0];
    if (page !== "settings") settingsDraft = null;
    document.getElementById("logout").hidden = !(state.authRequired && state.authed);
    if (state.authRequired && !state.authed) { setSetupMode(false); document.getElementById("nav").hidden = true; return pageLogin(); }
    // The server decides first whether the guided setup is still open: while it
    // is, every address shows the wizard and nothing else. Once it was seen
    // complete it stays complete, so later routes need no extra round trip.
    function enter(s) {
      var su = s && s.setup;
      if (su && !su.complete) return pageSetup();
      if (su && page === "setup" && wiz.seenOpen) return pageSetup();     // the finished screen, once
      if (page === "setup") { location.hash = "#/folders"; return; }
      if (page === "help") { history.replaceState(null, "", "#/settings/help"); return route(); }
      wiz.seenOpen = false;
      setSetupMode(false);
      // A page under a section keeps that section lit: a folder's own page and Add folder belong to
      // Folders, the Locations page to Settings. Deleted files of one folder is already "deleted".
      var section = page === "job" || page === "add" ? "folders" : page === "locations" ? "settings" : page;
      document.querySelectorAll("#nav a").forEach(function (a) {
        if (a.dataset.page === section) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
      });
      document.getElementById("nav").hidden = false;
      view.setAttribute("data-page", section);
      var pages = { folders: pageFolders, add: pageAdd, locations: pageLocations, activity: pageActivity, settings: pageSettings };
      if (page === "job" && seg[1]) pageJob(decodeURIComponent(seg[1]));
      else if (page === "deleted" && seg[1]) pageDeleted(decodeURIComponent(seg[1]));
      else if (page === "deleted") pageDeletedIndex();
      else (pages[page] || pageFolders)();
    }
    var known = state.status && state.status.setup;
    if (known && known.complete && page !== "setup") return enter(state.status);
    return loadStatus().then(function (s) { return s; }, function () { return null; }).then(function (s) {
      if (token !== route.n) return;       // a newer route started while this one waited
      enter(s);
    });
  }

  function boot() {
    return fetch("/api/session", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (s) {
      state.csrf = s.csrf; state.authRequired = s.auth_required; state.authed = s.authenticated;
      route();
      if (state.authed) loadStatus().catch(function () {});
    }).catch(function () { setView("Offline", [h("div", { class: "notice bad" }, "Cannot reach Keepsake. Is it running?")]); });
  }
  window.addEventListener("hashchange", function () { route(); view.focus({ preventScroll: true }); });
  boot();
})();
