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

  var jobs = document.getElementById("jobs");
  if (jobs && jobs.dataset.poll) poll(jobs);
})();
