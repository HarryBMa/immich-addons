// Small, dependency-free replacements for the three things htmx would have done here: poll a
// fragment, POST a run, POST a cancel. Vendoring htmx would mean fetching an asset at image build
// time; the hub has to work on a LAN with no internet, so this stays local. See design/README.md.

(function () {
  "use strict";

  function poll(container) {
    var url = container.dataset.poll;
    var interval = parseInt(container.dataset.interval || "2000", 10);
    var state = document.getElementById("poll-state");

    function tick() {
      if (document.hidden) return; // don't poll a background tab
      fetch(url, { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
        .then(function (html) {
          if (container.innerHTML !== html) container.innerHTML = html;
          if (state) { state.textContent = "live"; state.classList.remove("warn"); }
        })
        .catch(function () {
          if (state) { state.textContent = "offline"; state.classList.add("warn"); }
        });
    }

    tick();
    setInterval(tick, interval);
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  document.addEventListener("click", function (event) {
    var runTarget = event.target.closest("[data-run]");
    if (runTarget) {
      var form = document.querySelector("form.panel");
      var config = {};
      if (form) {
        new FormData(form).forEach(function (value, key) { config[key] = value; });
      }
      runTarget.disabled = true;
      post("/api/run/" + encodeURIComponent(runTarget.dataset.run), config)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          window.location.href = data.job_id ? "/jobs/" + data.job_id : "/jobs";
        })
        .catch(function () { runTarget.disabled = false; });
      return;
    }

    var cancelTarget = event.target.closest("[data-cancel]");
    if (cancelTarget) {
      cancelTarget.disabled = true;
      post("/api/jobs/" + encodeURIComponent(cancelTarget.dataset.cancel) + "/cancel");
    }
  });

  // --- selection handover (PLAN.md Phase 8a) --------------------------------------------
  //
  // Immich's "Send to Addons" POSTs the chosen asset IDs to /api/inbox and sends us here with
  // #sel=<token>. The fragment is the point: browsers never transmit it, so the IDs exist in no
  // URL, no access log and no Referer header. We exchange the token for them over a normal
  // authenticated request and fill the form's hidden field.
  function fillForm(input, ids) {
    input.value = ids.join(",");
    var label = document.querySelector("[data-selection-count]");
    if (label) label.textContent = ids.length + " photo" + (ids.length === 1 ? "" : "s") + " from Immich";

    // Point the source field at the selection, if the addon has one.
    var source = document.querySelector('select[name="source"]');
    if (source && Array.prototype.some.call(source.options, function (o) { return o.value === "selection"; })) {
      source.value = "selection";
    }

    // Drop the token from the address bar now that the selection lives in the form: a token in a
    // bookmarked URL is a loose end.
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }

  function offerAddons(token, count) {
    // The catalog has no form to fill, so it carries the token onward instead: every addon link
    // keeps the fragment, and the banner says what is waiting.
    var banner = document.querySelector("[data-selection-banner]");
    if (banner) {
      banner.hidden = false;
      banner.textContent = count + " photo" + (count === 1 ? "" : "s") + " from Immich — open an addon to use them.";
    }
    document.querySelectorAll('a[href^="/addons/"]').forEach(function (link) {
      link.href = link.href.split("#")[0] + "#sel=" + token;
    });
  }

  function applySelection() {
    var match = /(?:^|[#&])sel=([A-Za-z0-9_-]+)/.exec(window.location.hash || "");
    if (!match) return;
    var token = match[1];

    fetch("/api/selection/" + encodeURIComponent(token), { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (data) {
        var ids = data.asset_ids || [];
        var input = document.querySelector("[data-selection-input]");
        if (input) fillForm(input, ids);
        else offerAddons(token, ids.length);
      })
      .catch(function () {
        var label = document.querySelector("[data-selection-count]") || document.querySelector("[data-selection-banner]");
        if (label) {
          label.hidden = false;
          label.textContent = "that selection has expired — send it again from Immich";
        }
      });
  }

  var jobs = document.getElementById("jobs");
  if (jobs && jobs.dataset.poll) poll(jobs);
  applySelection();
})();
