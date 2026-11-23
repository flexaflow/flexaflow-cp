/* Client for live contest pages. Uses Socket.IO for pushes and a countdown
 * based on the server clock, so a participant's laptop clock does not matter. */
(function () {
  const body = document.body;
  const page = body.dataset.page || "";
  let C = { status: "not_configured" };
  try { C = JSON.parse(body.dataset.contest || "{}"); } catch (e) { /* keep default */ }
  if (!C.status) C.status = "not_configured";

  // Confirm prompts (inline onsubmit handlers are blocked by the CSP).
  document.querySelectorAll("form[data-confirm]").forEach((f) => {
    f.addEventListener("submit", (ev) => {
      if (!window.confirm(f.dataset.confirm)) ev.preventDefault();
    });
  });
  const notice = document.getElementById("notice");

  // Offset between server clock and this browser's clock.
  const offset = C.now ? Date.parse(C.now) - Date.now() : 0;
  const nowMs = () => Date.now() + offset;

  function fmt(ms) {
    if (ms < 0) ms = 0;
    const s = Math.floor(ms / 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
  }

  function tickCountdown() {
    const el = document.getElementById("countdown");
    if (!el) return;
    const target = C.status === "before" ? Date.parse(C.start)
                 : C.status === "running" ? Date.parse(C.end) : null;
    if (target === null) return;
    const left = target - nowMs();
    el.textContent = fmt(left);
    if (left <= -1500) {
      // Let the server state settle, then reload to show the new phase.
      location.reload();
    }
  }
  if (document.getElementById("countdown")) {
    tickCountdown();
    setInterval(tickCountdown, 500);
  }

  function showNotice(text) {
    if (notice) { notice.textContent = text; notice.hidden = false; }
  }

  // Disable submit on the problem page as soon as the contest is not running.
  function applyStatusToSubmitForm(status) {
    const btn = document.getElementById("submit-btn");
    if (btn && status !== "running") {
      btn.disabled = true;
      btn.textContent = status === "ended" ? "Contest ended" : "Not started";
    }
  }
  applyStatusToSubmitForm(C.status);

  if (!body.dataset.user || typeof io === "undefined") return;

  const sock = io({ transports: ["websocket", "polling"] });

  sock.on("contest_status", (s) => {
    const changed = s.status !== C.status || s.start !== C.start || s.end !== C.end;
    if (!changed) return;
    if (body.dataset.reload === "1") {
      location.reload();
    } else {
      showNotice("The contest schedule changed.");
      applyStatusToSubmitForm(s.status);
      Object.assign(C, s);
    }
  });

  sock.on("submission_update", (r) => {
    if (page === "submissions" || page === "problems") {
      location.reload();
      return;
    }
    const verdictText = `Submission #${r.id}: ${r.verdict}` +
      (r.total ? ` (${r.passed}/${r.total})` : "");
    showNotice(verdictText);
  });

  sock.on("leaderboard", (rows) => {
    const tbody = document.getElementById("board-body");
    if (!tbody) return;
    tbody.replaceChildren(...rows.map((r) => {
      const tr = document.createElement("tr");
      const cells = [r.rank, r.full_name ? `${r.username} (${r.full_name})` : r.username,
                     r.solved, r.penalty];
      for (const c of cells) {
        const td = document.createElement("td");
        td.textContent = String(c);
        tr.appendChild(td);
      }
      return tr;
    }));
    const stamp = document.getElementById("board-updated");
    if (stamp) stamp.textContent = "Updated " + new Date().toLocaleTimeString();
  });
})();
