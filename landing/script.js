(function () {
  "use strict";

  var root = document.documentElement;
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- theme switcher ------------------------------------------------- */

  var switcher = document.querySelector("[data-theme-switcher]");
  var swatches = switcher ? Array.prototype.slice.call(switcher.querySelectorAll(".swatch")) : [];

  function applyTheme(value, persist) {
    if (value === "default") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", value);
    }
    swatches.forEach(function (btn) {
      var active = btn.getAttribute("data-theme-value") === value;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
    if (persist) {
      try {
        localStorage.setItem("veloci-landing-theme", value);
      } catch (e) {
        /* private browsing / storage disabled: theme just won't persist */
      }
    }
  }

  swatches.forEach(function (btn) {
    btn.addEventListener("click", function () {
      applyTheme(btn.getAttribute("data-theme-value"), true);
    });
  });

  (function restoreTheme() {
    var saved = null;
    try {
      saved = localStorage.getItem("veloci-landing-theme");
    } catch (e) {
      saved = null;
    }
    if (saved) applyTheme(saved, false);
  })();

  /* ---- pipeline pulse on scroll into view ------------------------------ */

  var pipelineLine = document.querySelector("[data-pipeline-line]");
  if (pipelineLine && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          pipelineLine.classList.toggle("is-live", entry.isIntersecting);
        });
      },
      { threshold: 0.4 }
    );
    io.observe(pipelineLine);
  } else if (pipelineLine) {
    pipelineLine.classList.add("is-live");
  }

  /* ---- screenshot tilt --------------------------------------------------- */

  var tiltHost = document.querySelector("[data-tilt]");
  var tiltFrame = tiltHost ? tiltHost.querySelector(".showcase-frame") : null;
  if (tiltHost && tiltFrame && !reduceMotion) {
    tiltHost.addEventListener("mousemove", function (e) {
      var rect = tiltHost.getBoundingClientRect();
      var px = (e.clientX - rect.left) / rect.width - 0.5;
      var py = (e.clientY - rect.top) / rect.height - 0.5;
      tiltFrame.style.transform =
        "rotateX(" + (py * -6).toFixed(2) + "deg) rotateY(" + (px * 8).toFixed(2) + "deg)";
    });
    tiltHost.addEventListener("mouseleave", function () {
      tiltFrame.style.transform = "rotateX(0) rotateY(0)";
    });
  }

  /* ---- hero console demo -------------------------------------------------
     Recreates the real app's scan-to-download loop: type a URL, reveal rows
     with skeleton thumbnails, fill each progress bar, then roll counts and
     throughput. Loops indefinitely; freezes on a finished state under
     reduced-motion instead of animating. */

  var consoleEl = document.getElementById("console");
  if (!consoleEl) return;

  var typedEl = consoleEl.querySelector("[data-typed]");
  var scanBtn = consoleEl.querySelector("[data-scan-btn]");
  var rows = Array.prototype.slice.call(consoleEl.querySelectorAll(".console-row"));
  var counts = {
    all: consoleEl.querySelector('[data-count="all"]'),
    queued: consoleEl.querySelector('[data-count="queued"]'),
    active: consoleEl.querySelector('[data-count="active"]'),
    completed: consoleEl.querySelector('[data-count="completed"]')
  };
  var throughputEl = consoleEl.querySelector("[data-throughput]");
  var totalEl = consoleEl.querySelector("[data-total]");
  var demoUrl = "gallery.example.com/listing?pages=1-4";

  function setCount(key, n) {
    if (counts[key]) counts[key].textContent = String(n);
  }

  function sizeToMb(str) {
    var n = parseFloat(str);
    return isFinite(n) ? n : 0;
  }

  function formatTotal(mb) {
    if (mb >= 1000) return (mb / 1000).toFixed(2) + " GB";
    return Math.round(mb) + " MB";
  }

  function wait(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  function resetConsole() {
    typedEl.textContent = "";
    scanBtn.classList.remove("is-live");
    scanBtn.textContent = "Scan";
    rows.forEach(function (row) {
      row.className = "console-row is-loading";
      row.querySelector(".title").textContent = "";
      row.querySelector(".size").textContent = "";
      row.querySelector(".len").textContent = "";
      row.querySelector(".bar i").style.width = "0%";
    });
    setCount("all", 0);
    setCount("queued", 0);
    setCount("active", 0);
    setCount("completed", 0);
    throughputEl.textContent = "0 B/s";
    totalEl.textContent = "0 MB";
  }

  function showFinishedState() {
    typedEl.textContent = demoUrl;
    scanBtn.textContent = "Scan";
    var totalMb = 0;
    rows.forEach(function (row) {
      row.className = "console-row is-in is-done";
      row.querySelector(".title").textContent = row.getAttribute("data-title");
      row.querySelector(".size").textContent = row.getAttribute("data-size");
      row.querySelector(".len").textContent = row.getAttribute("data-len");
      row.querySelector(".bar i").style.width = "100%";
      totalMb += sizeToMb(row.getAttribute("data-size"));
    });
    setCount("all", rows.length);
    setCount("queued", 0);
    setCount("active", 0);
    setCount("completed", rows.length);
    throughputEl.textContent = "0 B/s";
    totalEl.textContent = formatTotal(totalMb);
  }

  function typeText(el, text, speed) {
    return new Promise(function (resolve) {
      var i = 0;
      (function tick() {
        el.textContent = text.slice(0, i);
        i++;
        if (i <= text.length) {
          setTimeout(tick, speed);
        } else {
          resolve();
        }
      })();
    });
  }

  async function runLoop() {
    resetConsole();
    await wait(500);
    await typeText(typedEl, demoUrl, 28);
    await wait(200);

    scanBtn.classList.add("is-live");
    scanBtn.textContent = "Scanning…";
    await wait(500);

    var queued = 0;
    var active = 0;
    var completed = 0;
    var totalMb = 0;

    for (var idx = 0; idx < rows.length; idx++) {
      var row = rows[idx];
      row.classList.remove("is-loading");
      row.classList.add("is-in");
      row.querySelector(".title").textContent = row.getAttribute("data-title");
      queued++;
      setCount("all", queued);
      setCount("queued", queued);
      await wait(360);
    }

    scanBtn.textContent = "Scan";
    scanBtn.classList.remove("is-live");
    await wait(300);

    for (var j = 0; j < rows.length; j++) {
      (function (row) {
        row.classList.remove("is-loading");
        row.classList.add("is-active");
        active++;
        queued--;
        setCount("queued", queued);
        setCount("active", active);
        throughputEl.textContent = (12 + j * 3.4).toFixed(1) + " MB/s";
        row.querySelector(".bar i").style.width = "100%";
      })(rows[j]);
      await wait(700);
    }

    await wait(1100);

    for (var k = 0; k < rows.length; k++) {
      var r = rows[k];
      r.classList.remove("is-active");
      r.classList.add("is-done");
      active--;
      completed++;
      totalMb += sizeToMb(r.getAttribute("data-size"));
      r.querySelector(".size").textContent = r.getAttribute("data-size");
      r.querySelector(".len").textContent = r.getAttribute("data-len");
      setCount("active", Math.max(active, 0));
      setCount("completed", completed);
      totalEl.textContent = formatTotal(totalMb);
      await wait(180);
    }

    throughputEl.textContent = "0 B/s";
    await wait(3200);
    runLoop();
  }

  if (reduceMotion) {
    showFinishedState();
  } else {
    runLoop();
  }
})();
