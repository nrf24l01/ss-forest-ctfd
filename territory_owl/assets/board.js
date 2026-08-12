(() => {
  fetch("/plugins/territory_owl/challenge-rewards")
    .then(response => response.ok ? response.json() : {})
    .then(rewards => {
      const render = () => {
        for (const [id, points] of Object.entries(rewards)) {
          const value = document.querySelector(`button.challenge-button[value="${id}"] .challenge-inner span`);
          const label = `${points} AP`;
          if (value && value.textContent !== label) value.textContent = label;
        }
      };

      render();
      new MutationObserver(render).observe(document.body, { childList: true, subtree: true });
    })
    .catch(() => {});
})();
