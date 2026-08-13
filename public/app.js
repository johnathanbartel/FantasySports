(function () {
  const state = { leagueId: null };

  const stepName = document.getElementById('step-name');
  const stepRoster = document.getElementById('step-roster');
  const stepTeam = document.getElementById('step-team');
  const stepDone = document.getElementById('step-done');
  const stepIndicator = document.getElementById('step-indicator');

  function showStep(step, label) {
    [stepName, stepRoster, stepTeam, stepDone].forEach((s) => s.classList.add('hidden'));
    step.classList.remove('hidden');
    stepIndicator.textContent = label;
  }

  function setError(elId, message) {
    document.getElementById(elId).textContent = message || '';
  }

  async function postJson(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || 'Request failed.');
    }
    return data;
  }

  document.getElementById('btn-create-league').addEventListener('click', async () => {
    setError('error-name', '');
    const name = document.getElementById('league-name').value;
    try {
      const league = await postJson('/api/leagues', { name });
      state.leagueId = league.id;
      showStep(stepRoster, 'Step 2 of 3');
    } catch (err) {
      setError('error-name', err.message);
    }
  });

  document.getElementById('btn-set-roster').addEventListener('click', async () => {
    setError('error-roster', '');
    const rosterSize = Number(document.getElementById('roster-size').value);
    try {
      await postJson(`/api/leagues/${state.leagueId}/roster-size`, { rosterSize });
      showStep(stepTeam, 'Step 3 of 3');
    } catch (err) {
      setError('error-roster', err.message);
    }
  });

  document.getElementById('btn-add-team').addEventListener('click', async () => {
    setError('error-team', '');
    try {
      const league = await postJson(`/api/leagues/${state.leagueId}/teams/placeholder`, {});
      document.getElementById('summary').textContent = JSON.stringify(league, null, 2);
      showStep(stepDone, 'Done');
    } catch (err) {
      setError('error-team', err.message);
    }
  });
})();
