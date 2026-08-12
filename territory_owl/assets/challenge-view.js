function territoryOwlRequest(method) {
  const id = CTFd._internal.challenge.data.id;
  return CTFd.fetch(`/plugins/territory_owl/instances/${id}`, { method, credentials: "same-origin" }).then(async response => {
    const data = await response.json();
    if (!response.ok || data.success === false) throw new Error(data.message || "Не удалось обработать инстанс");
    return data;
  });
}

function loadTerritoryOwlInstance() {
  territoryOwlRequest("GET").then(data => {
    const panel = document.getElementById("territory-owl-panel");
    panel.innerHTML = data.active
      ? `<p>Инстанс: <code>${data.host}:${data.port}</code></p><button class="btn btn-outline-danger" id="territory-owl-stop">Остановить</button>`
      : '<button class="btn btn-primary" id="territory-owl-launch">Запустить инстанс</button>';
    const launch = document.getElementById("territory-owl-launch");
    const stop = document.getElementById("territory-owl-stop");
    if (launch) launch.onclick = async () => {
      launch.disabled = true;
      launch.textContent = "Запуск...";
      try {
        await territoryOwlRequest("POST");
        loadTerritoryOwlInstance();
      } catch (error) {
        panel.insertAdjacentHTML("beforeend", `<p class="text-danger mt-2">${error.message}</p>`);
        launch.disabled = false;
        launch.textContent = "Запустить инстанс";
      }
    };
    if (stop) stop.onclick = () => territoryOwlRequest("DELETE").then(loadTerritoryOwlInstance).catch(error => {
      panel.insertAdjacentHTML("beforeend", `<p class="text-danger mt-2">${error.message}</p>`);
    });
  }).catch(error => {
    document.getElementById("territory-owl-panel").innerHTML = `<p class="text-danger">${error.message}</p>`;
  });
}

CTFd._internal.challenge.preRender = function() {};
CTFd._internal.challenge.postRender = loadTerritoryOwlInstance;
CTFd._internal.challenge.submit = function(preview) {
  const input = document.getElementById("challenge-input");
  const params = { challenge_id: CTFd._internal.challenge.data.id, submission: input ? input.value : "" };
  return CTFd.api.post_challenge_attempt(preview ? { preview: true } : {}, params);
};
