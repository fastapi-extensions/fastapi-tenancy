/* fastapi-tenancy docs extra JS */

document.addEventListener("DOMContentLoaded", function () {

  /* ── Smooth anchor scroll on hash links ──────────────────────────────── */
  document.querySelectorAll('a[href^="#"]').forEach(function (anchor) {
    anchor.addEventListener("click", function (e) {
      var target = document.querySelector(this.getAttribute("href"));
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        history.pushState(null, null, this.getAttribute("href"));
      }
    });
  });

  /* ── Copy-code button label reset after 2 s ─────────────────────────── */
  document.querySelectorAll(".md-clipboard").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var original = btn.getAttribute("title");
      btn.setAttribute("title", "Copied!");
      setTimeout(function () {
        btn.setAttribute("title", original);
      }, 2000);
    });
  });

});
