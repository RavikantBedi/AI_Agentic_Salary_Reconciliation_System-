/* Progressive enhancement only.
   Every page works with JavaScript disabled; this adds convenience,
   which matters on the low-end devices and weak connections this
   application targets (spec s59). */

(function () {
  "use strict";

  /* ------------------------------------------------------- mobile menu */
  function initMenu() {
    var btn = document.querySelector(".menu-btn");
    var sidebar = document.querySelector(".sidebar");
    var backdrop = document.querySelector(".backdrop");
    if (!btn || !sidebar) return;

    function toggle(open) {
      sidebar.classList.toggle("open", open);
      if (backdrop) backdrop.classList.toggle("show", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    }
    btn.addEventListener("click", function () {
      toggle(!sidebar.classList.contains("open"));
    });
    if (backdrop) backdrop.addEventListener("click", function () { toggle(false); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") toggle(false);
    });
  }

  /* --------------------------------------------- confirm risky actions */
  function initConfirms() {
    document.querySelectorAll("[data-confirm]").forEach(function (el) {
      el.addEventListener("click", function (e) {
        if (!window.confirm(el.getAttribute("data-confirm"))) e.preventDefault();
      });
    });
  }

  /* ------------------------------------------ assistant question chips */
  function initSuggestions() {
    var input = document.querySelector("#assistant-input");
    if (!input) return;
    document.querySelectorAll("[data-question]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        input.value = btn.getAttribute("data-question");
        input.focus();
      });
    });
  }

  /* ------------------------------------------------- job progress poll */
  function initJobPolling() {
    var el = document.querySelector("[data-job-uid]");
    if (!el) return;
    var uid = el.getAttribute("data-job-uid");
    var bar = document.querySelector("#job-progress .fill");
    var label = document.querySelector("#job-message");
    var pct = document.querySelector("#job-percent");
    var tries = 0;

    function poll() {
      tries += 1;
      fetch("/api/jobs/" + uid, { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          if (bar) bar.style.width = data.percent + "%";
          if (pct) pct.textContent = data.percent + "%";
          if (label && data.message) label.textContent = data.message;
          var done = ["COMPLETED", "FAILED", "REQUIRES_REVIEW", "CANCELLED"];
          if (done.indexOf(data.status) !== -1) {
            window.location.reload();
            return;
          }
          // Back off gradually so a long job does not hammer the server.
          if (tries < 200) setTimeout(poll, Math.min(4000, 900 + tries * 120));
        })
        .catch(function () { if (tries < 20) setTimeout(poll, 3000); });
    }
    setTimeout(poll, 900);
  }

  /* --------------------------------- keep a draft of long text fields */
  /* Weak connectivity means a lost form is a real risk (spec s59). */
  function initDrafts() {
    document.querySelectorAll("textarea[data-draft]").forEach(function (area) {
      var key = "draft:" + location.pathname + ":" + area.getAttribute("data-draft");
      try {
        var saved = localStorage.getItem(key);
        if (saved && !area.value) area.value = saved;
      } catch (e) { /* private mode: drafts simply unavailable */ }

      var timer;
      area.addEventListener("input", function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
          try { localStorage.setItem(key, area.value); } catch (e) {}
        }, 500);
      });
      var form = area.closest("form");
      if (form) {
        form.addEventListener("submit", function () {
          try { localStorage.removeItem(key); } catch (e) {}
        });
      }
    });
  }

  /* ------------------------------------- stop double form submissions */
  function initSubmitGuard() {
    document.querySelectorAll("form[data-guard]").forEach(function (form) {
      form.addEventListener("submit", function () {
        var btn = form.querySelector("button[type=submit], input[type=submit]");
        if (btn) {
          btn.disabled = true;
          btn.textContent = btn.getAttribute("data-busy") || "Working...";
        }
      });
    });
  }

  /* ------------------------------------------ dynamic rule-builder rows */
  function initRepeater() {
    document.querySelectorAll("[data-repeat-add]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var container = document.querySelector(btn.getAttribute("data-repeat-add"));
        if (!container) return;
        var rows = container.querySelectorAll("[data-repeat-row]");
        var last = rows[rows.length - 1];
        if (!last) return;
        var clone = last.cloneNode(true);
        clone.querySelectorAll("input").forEach(function (i) {
          if (i.type === "checkbox") { i.checked = false; i.value = String(rows.length); }
          else i.value = "";
        });
        container.appendChild(clone);
      });
    });
  }

  /* ----------------------------------------------- filter auto-submit */
  function initAutoFilters() {
    document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
      sel.addEventListener("change", function () {
        var form = sel.closest("form");
        if (form) form.submit();
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initMenu();
    initConfirms();
    initSuggestions();
    initJobPolling();
    initDrafts();
    initSubmitGuard();
    initRepeater();
    initAutoFilters();
  });
})();
