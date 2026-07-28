(() => {
  fetch("/api/v1/territory-control/me", { credentials: "same-origin" })
    .then(response => response.ok ? response.json() : null)
    .then(data => {
      if (!data) return;
      const score = document.querySelector("#team-score");
      const userScore = document.querySelector("#score-graph");
      const target = score || userScore;
      if (!target || document.querySelector("#territory-attack-points")) return;

      const card = document.createElement("section");
      card.id = "territory-attack-points";
      card.className = "text-center mt-3";
      card.innerHTML = `<h2>${data.attack_points} <small>Attack Points</small></h2><a href="/territory-control">Territory Control</a>`;
      target.parentElement.insertBefore(card, target.nextSibling);
    })
    .catch(() => {});
})();
