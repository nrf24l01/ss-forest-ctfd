(() => {
  const rewardsUrl = "/api/v1/territory-control/challenge-rewards";

  fetch(rewardsUrl)
    .then(response => response.ok ? response.json() : {})
    .then(rewards => {
      const renderChallengeRewards = () => {
        for (const [id, attackPoints] of Object.entries(rewards)) {
          const value = document.querySelector(`button.challenge-button[value="${id}"] .challenge-inner span`);
          const label = `${attackPoints} AP`;
          if (value && value.textContent !== label) value.textContent = label;
        }
      };

      renderChallengeRewards();
      // The CTFd challenge board is rendered asynchronously after this plugin script.
      new MutationObserver(renderChallengeRewards).observe(document.body, { childList: true, subtree: true });
    })
    .catch(() => {});

  const teamId = document.querySelector("#team-id")?.dataset.ctfdTeamId;
  const pointsUrl = teamId
    ? `/api/v1/territory-control/teams/${teamId}/points`
    : "/api/v1/territory-control/me";

  fetch(pointsUrl, { credentials: "same-origin" })
    .then(data => {
      if (!data) return;
      const score = document.querySelector("#team-score");
      const userScore = document.querySelector("#score-graph");
      const target = score || userScore;
      if (!target || document.querySelector("#territory-team-points")) return;

      const card = document.createElement("section");
      card.id = "territory-team-points";
      card.className = "text-center mt-3";
      const scoreLabel = Number.isFinite(data.score) ? `${data.score} <small>CTFd Points</small>` : "";
      card.innerHTML = `<h2>${scoreLabel}${scoreLabel ? " · " : ""}${data.attack_points} <small>Attack Points</small></h2><a href="/territory-control">Territory Control</a>`;
      target.parentElement.insertBefore(card, target.nextSibling);
    })
    .catch(() => {});
})();
