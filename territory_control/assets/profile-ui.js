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
