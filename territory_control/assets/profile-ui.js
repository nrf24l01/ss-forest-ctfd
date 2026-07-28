(() => {
  const rewardsUrl = "/api/v1/territory-control/challenge-rewards";

  const showAttackPoints = async challenge => {
    const rewards = await (await fetch(rewardsUrl)).json();
    const attackPoints = rewards[challenge.id];
    if (attackPoints !== undefined) challenge.value = `${attackPoints} AP`;
    return challenge;
  };

  const decorateChallengePages = () => {
    const challengePages = window.CTFd && window.CTFd.pages;
    if (!challengePages || challengePages.territoryControlDecorated) return;
    challengePages.territoryControlDecorated = true;
    const getChallenges = challengePages.challenges.getChallenges;
    challengePages.challenges.getChallenges = async (...args) => {
      const challenges = await getChallenges(...args);
      return Promise.all(challenges.map(showAttackPoints));
    };

    const getChallenge = challengePages.challenge.getChallenge;
    challengePages.challenge.getChallenge = async (...args) => showAttackPoints(await getChallenge(...args));
  };

  document.addEventListener("DOMContentLoaded", decorateChallengePages);

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
