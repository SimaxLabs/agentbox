(function () {
  "use strict";

  function initializeHostPicker(root) {
    root.querySelectorAll("[data-host-picker]").forEach(function (form) {
      if (form.dataset.ready) return;
      form.dataset.ready = "true";
      form.querySelector("select").addEventListener("change", function () {
        form.submit();
      });
    });
  }

  function initializeRestoreForms(root) {
    root.querySelectorAll("[data-restore-form]").forEach(function (form) {
      if (form.dataset.ready) return;
      form.dataset.ready = "true";
      var mode = form.querySelector("[data-restore-mode]");
      var source = form.querySelector("[data-source-mode]");
      var target = form.querySelector('select[name="tool"]');
      function updateMode() {
        var exact = mode.value === "exact";
        if (exact) source.value = "matching";
        source.disabled = exact;
        source.title = exact ? "Exact restoration uses recorded locations from the matching catalog." : "";
        target.querySelectorAll("option[data-portable]").forEach(function (option) {
          option.disabled = !exact && option.dataset.portable !== "true";
        });
        if (target.selectedOptions[0] && target.selectedOptions[0].disabled) target.value = "all";
      }
      mode.addEventListener("change", updateMode);
      updateMode();
    });
  }

  function initializeInventory(root) {
    root.querySelectorAll("[data-inventory-search]").forEach(function (input) {
      if (input.dataset.ready) return;
      input.dataset.ready = "true";
      input.addEventListener("input", function () {
        var query = input.value.trim().toLowerCase();
        var visible = 0;
        root.querySelectorAll("[data-inventory-row]").forEach(function (row) {
          var matches = !query || row.dataset.searchValue.toLowerCase().indexOf(query) !== -1;
          row.hidden = !matches;
          if (matches) visible += 1;
        });
        var empty = root.querySelector("[data-no-results]");
        if (empty) empty.hidden = visible !== 0;
      });
    });
  }

  function refreshDashboard(host) {
    var url = "/dashboard?host=" + encodeURIComponent(host || "");
    if (window.htmx) {
      window.htmx.ajax("GET", url, {target: "#dashboard-shell", swap: "innerHTML"});
      return;
    }
    fetch(url).then(function (response) { return response.text(); }).then(function (html) {
      var shell = document.querySelector("#dashboard-shell");
      shell.innerHTML = html;
      initializeInventory(shell);
    });
  }

  function appendEvent(log, payload) {
    var line = document.createElement("div");
    line.className = "log-line tone-" + (payload.tone || "quiet");
    var kind = document.createElement("span");
    kind.textContent = payload.kind || "event";
    var message = document.createElement("code");
    message.textContent = payload.message || "";
    line.appendChild(kind);
    line.appendChild(message);
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  }

  function initializeJobs(root) {
    root.querySelectorAll("[data-operation-job]").forEach(function (panel) {
      if (panel.dataset.started) return;
      panel.dataset.started = "true";
      var log = panel.querySelector("[data-live-log]");
      var url = "/operations/" + encodeURIComponent(panel.dataset.jobId) + "/events?token=" + encodeURIComponent(panel.dataset.csrfToken);
      var stream = new EventSource(url);
      stream.onmessage = function (event) {
        var payload = JSON.parse(event.data);
        appendEvent(log, payload);
        if (payload.done) {
          stream.close();
          var state = panel.querySelector(".running-state");
          state.className = "running-state " + (payload.success ? "finished" : "failed");
          state.textContent = payload.success ? "Complete" : "Stopped";
          if (payload.success && panel.dataset.reloadPage === "true") {
            window.location.reload();
          } else if (payload.success) {
            refreshDashboard(panel.dataset.host);
          }
        }
      };
      stream.onerror = function () {
        if (stream.readyState === EventSource.CLOSED) return;
        appendEvent(log, {kind: "connection", tone: "warning", message: "Live log connection interrupted; reconnecting..."});
      };
    });
  }

  function initializeFallbackForms(root) {
    root.querySelectorAll("[data-async-form]").forEach(function (form) {
      if (form.dataset.fallbackReady) return;
      form.dataset.fallbackReady = "true";
      form.addEventListener("submit", function (event) {
        if (window.htmx) return;
        event.preventDefault();
        var target = document.querySelector(form.dataset.target || "#operation-panel");
        fetch(form.action, {method: "POST", body: new FormData(form), credentials: "same-origin"})
          .then(function (response) { return response.text(); })
          .then(function (html) {
            target.innerHTML = html;
            initialize(target);
          });
      });
    });
  }

  function initializeDismissButtons(root) {
    root.querySelectorAll("[data-dismiss-error]").forEach(function (button) {
      if (button.dataset.ready) return;
      button.dataset.ready = "true";
      button.addEventListener("click", function () {
        var card = button.closest(".error-card");
        if (card) card.remove();
      });
    });
  }

  function initialize(root) {
    initializeHostPicker(root);
    initializeRestoreForms(root);
    initializeInventory(root);
    initializeJobs(root);
    initializeFallbackForms(root);
    initializeDismissButtons(root);
  }

  document.addEventListener("DOMContentLoaded", function () { initialize(document); });
  document.addEventListener("htmx:beforeSwap", function (event) {
    var response = event.detail.xhr;
    var contentType = response.getResponseHeader("Content-Type") || "";
    if (response.status >= 400 && response.status < 500 && contentType.indexOf("text/html") === 0) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
  document.addEventListener("htmx:afterSwap", function (event) { initialize(event.target); });
})();
