// Shared Conference and Division Management Modal
// Allows the commissioner to create, rename, delete conferences and divisions,
// configure conference/division structures, and randomize or manually align teams.
const ConferenceModal = (() => {
  let modalEl = null;
  let currentOpts = null;
  let conferences = [];
  let teams = [];
  let isEditable = true;

  function ensureModal() {
    if (modalEl) return;
    modalEl = document.createElement('div');
    modalEl.id = 'conference-modal-container';
    modalEl.style.cssText = 'display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.65);z-index:99999;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(2px);box-sizing:border-box;';
    modalEl.innerHTML = `
      <div class="card elev-lg" style="max-width:740px;width:100%;padding:var(--space-5);background:var(--color-bg);border:1px solid var(--color-divider);border-radius:var(--radius-md);box-shadow:0 12px 32px rgba(0,0,0,0.25);position:relative;max-height:92vh;overflow-y:auto;box-sizing:border-box">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:var(--space-2)">
          <div>
            <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--color-accent);margin-bottom:2px">Commissioner Control Room</div>
            <h3 style="margin:0;font-size:20px" id="cm-modal-title">Manage Conferences &amp; Divisions</h3>
          </div>
          <button type="button" class="btn btn-ghost" id="cm-close-btn" style="padding:2px 8px;font-size:20px;line-height:1;cursor:pointer">&times;</button>
        </div>
        <p class="text-muted" style="margin:0 0 var(--space-4);font-size:13px" id="cm-modal-desc">
          Conferences contain divisions. Configure the number of conferences and divisions, customize names, and randomize or manually assign teams.
        </p>

        <div id="cm-banner-locked" style="display:none;background:var(--hi);color:var(--ink);border:3px solid var(--ink);padding:10px 14px;border-radius:var(--radius-md);margin-bottom:var(--space-4);font-size:13px">
          <strong>Season in progress:</strong> Conferences and divisions are locked while regular season games or series are active. Realignment unlocks before or after the season.
        </div>

        <div id="cm-error" style="display:none;background:var(--color-accent-2-100, #fee2e2);color:var(--color-accent-2-800, #991b1b);padding:8px 12px;border-radius:var(--radius-md);margin-bottom:var(--space-3);font-size:13px"></div>
        <div id="cm-success" style="display:none;background:var(--good);color:#fff;border:3px solid var(--ink);padding:8px 12px;border-radius:var(--radius-md);margin-bottom:var(--space-3);font-size:13px"></div>

        <!-- Section 1: Structure Setup (Conferences & Divisions) -->
        <div style="background:var(--color-surface, rgba(255,255,255,0.03));border:1px solid var(--color-divider);border-radius:var(--radius-md);padding:var(--space-3) var(--space-4);margin-bottom:var(--space-4)">
          <div style="font-weight:700;font-size:14px;margin-bottom:4px">Structure Setup</div>
          <p class="text-muted" style="font-size:12px;margin:0 0 var(--space-3)">
            Quickly generate standard conferences and divisions, with conferences containing equal divisions.
          </p>
          <div style="display:flex;gap:var(--space-3);align-items:flex-end;flex-wrap:wrap">
            <div style="flex:1;min-width:140px">
              <label for="cm-conf-count-sel" style="display:block;font-size:12px;font-weight:600;margin-bottom:4px">Conferences</label>
              <select id="cm-conf-count-sel" class="inp" style="width:100%;padding:6px 10px;font-size:13px">
                <option value="1">1 Conference</option>
                <option value="2" selected>2 Conferences</option>
                <option value="3">3 Conferences</option>
                <option value="4">4 Conferences</option>
              </select>
            </div>
            <div style="flex:1;min-width:160px">
              <label for="cm-div-count-sel" style="display:block;font-size:12px;font-weight:600;margin-bottom:4px">Divisions per Conference</label>
              <select id="cm-div-count-sel" class="inp" style="width:100%;padding:6px 10px;font-size:13px">
                <option value="0">0 (No Divisions)</option>
                <option value="1">1 Division</option>
                <option value="2" selected>2 Divisions</option>
                <option value="3">3 Divisions</option>
                <option value="4">4 Divisions</option>
              </select>
            </div>
            <div style="display:flex;align-items:center;gap:6px;padding-bottom:6px">
              <input type="checkbox" id="cm-setup-randomize" checked style="cursor:pointer">
              <label for="cm-setup-randomize" style="font-size:12px;cursor:pointer">Randomize teams evenly</label>
            </div>
            <button type="button" class="btn btn-secondary" id="cm-apply-structure-btn" style="font-size:13px;padding:7px 14px">Set Structure</button>
          </div>
        </div>

        <!-- Section 2: Conferences & Divisions Hierarchy List -->
        <div style="margin-bottom:var(--space-5)">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--space-2)">
            <h4 style="margin:0;font-size:15px">Conferences &amp; Divisions Hierarchy</h4>
            <span class="text-muted" style="font-size:12px" id="cm-conf-count"></span>
          </div>

          <div id="cm-confs-list" style="display:flex;flex-direction:column;gap:10px;margin-bottom:var(--space-3)"></div>

          <form id="cm-add-form" onsubmit="return false;" style="display:flex;gap:var(--space-2);align-items:center;margin-top:var(--space-2)">
            <input type="text" id="cm-new-conf-name" placeholder="New conference name (e.g. Eastern Conference)" maxlength="80" style="flex:1;padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--color-divider);background:var(--color-bg);color:var(--color-text);font-size:13px;box-sizing:border-box">
            <button type="submit" class="btn btn-secondary" id="cm-add-conf-btn" style="font-size:13px;padding:8px 14px">Add Conference</button>
          </form>
        </div>

        <!-- Section 3: Team Alignment & Randomize -->
        <div style="margin-bottom:var(--space-4);padding-top:var(--space-3);border-top:1px solid var(--color-divider)">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:var(--space-2);margin-bottom:var(--space-2)">
            <div>
              <h4 style="margin:0;font-size:15px">Team Alignment</h4>
              <span class="text-muted" style="font-size:12px">Assign each team to a division / conference, or randomize evenly.</span>
            </div>
            <button type="button" class="btn btn-secondary" id="cm-randomize-btn" style="font-size:13px;padding:6px 12px;background:var(--color-accent, #2563eb);color:#fff;font-weight:600;display:flex;align-items:center;gap:6px">
              🎲 Randomize Divisions &amp; Conferences
            </button>
          </div>

          <div style="max-height:260px;overflow-y:auto;border:1px solid var(--color-divider);border-radius:var(--radius-md);margin-bottom:var(--space-3)">
            <table class="table" style="width:100%;margin:0;font-size:13px">
              <thead><tr><th style="padding:8px 12px">Team</th><th style="padding:8px 12px">Owner / Status</th><th style="padding:8px 12px">Conference &amp; Division</th></tr></thead>
              <tbody id="cm-alignment-body"></tbody>
            </table>
          </div>

          <div style="display:flex;justify-content:flex-end;gap:var(--space-2)">
            <button type="button" class="btn btn-secondary" id="cm-cancel-btn">Close</button>
            <button type="button" class="btn btn-primary" id="cm-save-alignment-btn">Save Team Alignment</button>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(modalEl);

    document.getElementById('cm-close-btn').onclick = close;
    document.getElementById('cm-cancel-btn').onclick = close;
    modalEl.onclick = (e) => { if (e.target === modalEl) close(); };

    document.getElementById('cm-apply-structure-btn').onclick = doSetupStructure;
    document.getElementById('cm-add-conf-btn').onclick = doAddConference;
    document.getElementById('cm-randomize-btn').onclick = doRandomizeAlignment;
    document.getElementById('cm-save-alignment-btn').onclick = doSaveAlignment;
  }

  function showError(msg) {
    const err = document.getElementById('cm-error');
    err.textContent = msg;
    err.style.display = msg ? 'block' : 'none';
    if (msg) document.getElementById('cm-success').style.display = 'none';
  }

  function showSuccess(msg) {
    const s = document.getElementById('cm-success');
    s.textContent = msg;
    s.style.display = msg ? 'block' : 'none';
    if (msg) document.getElementById('cm-error').style.display = 'none';
  }

  function esc(t) {
    return String(t || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  }

  async function reload() {
    showError('');
    showSuccess('');
    try {
      const [cRes, tRes] = await Promise.all([
        FSC.api(`/leagues/${currentOpts.leagueId}/conferences`),
        FSC.api(`/leagues/${currentOpts.leagueId}/teams`),
      ]);
      conferences = cRes || [];
      teams = tRes || [];
      isEditable = conferences.length ? (conferences[0].editable !== false) : true;
      renderUI();
    } catch (e) {
      showError(e.message || 'Failed to load conferences and divisions');
    }
  }

  function renderUI() {
    document.getElementById('cm-banner-locked').style.display = isEditable ? 'none' : 'block';
    document.getElementById('cm-apply-structure-btn').disabled = !isEditable;
    document.getElementById('cm-conf-count-sel').disabled = !isEditable;
    document.getElementById('cm-div-count-sel').disabled = !isEditable;
    document.getElementById('cm-setup-randomize').disabled = !isEditable;
    document.getElementById('cm-new-conf-name').disabled = !isEditable;
    document.getElementById('cm-add-conf-btn').disabled = !isEditable;
    document.getElementById('cm-randomize-btn').disabled = !isEditable;
    document.getElementById('cm-save-alignment-btn').disabled = !isEditable;

    const totalDivs = conferences.reduce((acc, c) => acc + (c.divisions ? c.divisions.length : 0), 0);
    document.getElementById('cm-conf-count').textContent =
      `${conferences.length} conference${conferences.length === 1 ? '' : 's'} · ${totalDivs} division${totalDivs === 1 ? '' : 's'}`;

    // Render Conferences and Divisions Hierarchy
    const confsList = document.getElementById('cm-confs-list');
    confsList.innerHTML = conferences.map(c => {
      const divs = c.divisions || [];
      const divsHtml = divs.length ? divs.map(d => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:var(--color-bg);border:1px solid var(--color-divider);border-radius:var(--radius-sm);font-size:12px;margin-top:4px">
          <div style="display:flex;align-items:baseline;gap:6px">
            <span style="color:var(--color-accent);font-weight:700">↳</span>
            <strong>${esc(d.name)}</strong>
            <span class="text-muted" style="font-size:11px">(${d.team_count} team${d.team_count === 1 ? '' : 's'})</span>
          </div>
          ${isEditable ? `
            <div style="display:flex;gap:4px">
              <button type="button" class="btn btn-ghost" style="font-size:11px;padding:2px 6px" data-cm-rename-div="${d.id}" data-cm-div-name="${esc(d.name)}">Rename</button>
              <button type="button" class="btn btn-ghost" style="font-size:11px;padding:2px 6px;color:var(--color-accent-2, #ef4444)" data-cm-del-div="${d.id}" data-cm-div-name="${esc(d.name)}">Delete</button>
            </div>
          ` : ''}
        </div>
      `).join('') : `<div class="text-muted" style="font-size:11px;font-style:italic;padding:4px 0">No divisions (teams belong directly to conference)</div>`;

      return `
        <div style="background:var(--color-surface, rgba(255,255,255,0.03));border:1px solid var(--color-divider);border-radius:var(--radius-md);padding:10px 14px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="display:flex;align-items:baseline;gap:8px">
              <strong style="font-size:14px">${esc(c.name)}</strong>
              <span class="text-muted" style="font-size:12px">${c.team_count} total team${c.team_count === 1 ? '' : 's'}</span>
            </div>
            ${isEditable ? `
              <div style="display:flex;gap:4px">
                <button type="button" class="btn btn-ghost" style="font-size:11px;padding:3px 8px;color:var(--color-accent)" data-cm-add-div="${c.id}" data-cm-conf-name="${esc(c.name)}">+ Add Division</button>
                <button type="button" class="btn btn-ghost" style="font-size:11px;padding:3px 8px" data-cm-rename-conf="${c.id}" data-cm-conf-name="${esc(c.name)}">Rename</button>
                ${conferences.length > 1 ? `<button type="button" class="btn btn-ghost" style="font-size:11px;padding:3px 8px;color:var(--color-accent-2, #ef4444)" data-cm-del-conf="${c.id}" data-cm-conf-name="${esc(c.name)}">Delete</button>` : ''}
              </div>
            ` : ''}
          </div>
          <div style="margin-top:6px;padding-left:8px;border-left:2px solid var(--color-divider)">
            ${divsHtml}
          </div>
        </div>
      `;
    }).join('');

    // Wire conference & division actions
    confsList.querySelectorAll('[data-cm-add-div]').forEach(btn => {
      btn.onclick = () => {
        const cid = Number(btn.getAttribute('data-cm-add-div'));
        const cname = btn.getAttribute('data-cm-conf-name');
        const divName = prompt(`Enter new division name for "${cname}":`);
        if (divName && divName.trim()) {
          doAddDivision(cid, divName.trim());
        }
      };
    });

    confsList.querySelectorAll('[data-cm-rename-conf]').forEach(btn => {
      btn.onclick = () => {
        const cid = Number(btn.getAttribute('data-cm-rename-conf'));
        const curName = btn.getAttribute('data-cm-conf-name');
        const newName = prompt('Enter new name for this conference:', curName);
        if (newName && newName.trim() && newName.trim() !== curName) {
          doRenameConference(cid, newName.trim());
        }
      };
    });

    confsList.querySelectorAll('[data-cm-del-conf]').forEach(btn => {
      btn.onclick = () => {
        const cid = Number(btn.getAttribute('data-cm-del-conf'));
        const name = btn.getAttribute('data-cm-conf-name');
        if (confirm(`Delete conference "${name}" and its divisions? Teams will be reassigned to another conference.`)) {
          doDeleteConference(cid);
        }
      };
    });

    confsList.querySelectorAll('[data-cm-rename-div]').forEach(btn => {
      btn.onclick = () => {
        const did = Number(btn.getAttribute('data-cm-rename-div'));
        const curName = btn.getAttribute('data-cm-div-name');
        const newName = prompt('Enter new name for this division:', curName);
        if (newName && newName.trim() && newName.trim() !== curName) {
          doRenameDivision(did, newName.trim());
        }
      };
    });

    confsList.querySelectorAll('[data-cm-del-div]').forEach(btn => {
      btn.onclick = () => {
        const did = Number(btn.getAttribute('data-cm-del-div'));
        const name = btn.getAttribute('data-cm-div-name');
        if (confirm(`Delete division "${name}"? Teams assigned to it will remain in the conference without a division.`)) {
          doDeleteDivision(did);
        }
      };
    });

    // Render Team Alignment Table
    const hasAnyDivisions = conferences.some(c => c.divisions && c.divisions.length > 0);
    const alignBody = document.getElementById('cm-alignment-body');
    alignBody.innerHTML = teams.map(t => {
      const statusBadge = t.is_ai_owned
        ? '<span class="tag tag-neutral" style="font-size:11px">AI</span>'
        : (t.invite_status === 'pending_claim'
          ? `<span class="tag" style="font-size:11px;background:var(--hi);color:var(--ink)">Pending (${esc(t.owner_email || '')})</span>`
          : `<span class="tag" style="font-size:11px;background:var(--good);color:#fff">Claimed (${esc(t.owner_name || 'Manager')})</span>`);

      let optionsHtml = '<option value="">-- Unassigned --</option>';
      if (hasAnyDivisions) {
        optionsHtml += conferences.map(c => {
          const divs = c.divisions || [];
          if (divs.length) {
            return `
              <optgroup label="${esc(c.name)}">
                ${divs.map(d => `<option value="div_${d.id}" ${t.division_id === d.id ? 'selected' : ''}>${esc(c.name)} · ${esc(d.name)}</option>`).join('')}
              </optgroup>
            `;
          } else {
            return `<option value="conf_${c.id}" ${t.conference_id === c.id && !t.division_id ? 'selected' : ''}>${esc(c.name)} (No Division)</option>`;
          }
        }).join('');
      } else {
        optionsHtml += conferences.map(c =>
          `<option value="conf_${c.id}" ${t.conference_id === c.id ? 'selected' : ''}>${esc(c.name)}</option>`
        ).join('');
      }

      return `<tr>
        <td style="padding:8px 12px;font-weight:600">${esc(t.name)}</td>
        <td style="padding:8px 12px">${statusBadge}</td>
        <td style="padding:8px 12px">
          <select class="inp cm-team-align-select" data-team-id="${t.id}" ${!isEditable ? 'disabled' : ''} style="width:100%;padding:4px 8px;font-size:13px">
            ${optionsHtml}
          </select>
        </td>
      </tr>`;
    }).join('');
  }

  async function doSetupStructure() {
    showError('');
    showSuccess('');
    const confCount = Number(document.getElementById('cm-conf-count-sel').value);
    const divCount = Number(document.getElementById('cm-div-count-sel').value);
    const randomize = document.getElementById('cm-setup-randomize').checked;

    if (!confirm(`Configure league into ${confCount} conference${confCount > 1 ? 's' : ''} with ${divCount} division${divCount === 1 ? '' : 's'} each? Existing alignment will be updated.`)) {
      return;
    }

    const btn = document.getElementById('cm-apply-structure-btn');
    btn.disabled = true;
    try {
      await FSC.post(`/leagues/${currentOpts.leagueId}/conferences/setup-structure`, {
        conference_count: confCount,
        divisions_per_conference: divCount,
        randomize_teams: randomize,
      });
      showSuccess('Conference and division structure configured successfully!');
      await reload();
      if (currentOpts && typeof currentOpts.onUpdated === 'function') {
        currentOpts.onUpdated();
      }
    } catch (e) {
      showError(e.message || 'Failed to setup structure');
    } finally {
      btn.disabled = false;
    }
  }

  async function doRandomizeAlignment() {
    showError('');
    showSuccess('');
    if (!confirm('Randomly assign all teams evenly across all conferences and divisions?')) {
      return;
    }
    const btn = document.getElementById('cm-randomize-btn');
    btn.disabled = true;
    try {
      await FSC.post(`/leagues/${currentOpts.leagueId}/conferences/randomize`);
      showSuccess('Teams randomly and evenly assigned across all conferences and divisions!');
      await reload();
      if (currentOpts && typeof currentOpts.onUpdated === 'function') {
        currentOpts.onUpdated();
      }
    } catch (e) {
      showError(e.message || 'Failed to randomize teams');
    } finally {
      btn.disabled = false;
    }
  }

  async function doAddConference() {
    showError('');
    showSuccess('');
    const nameInput = document.getElementById('cm-new-conf-name');
    const name = nameInput.value.trim();
    if (!name) {
      showError('Please enter a conference name');
      return;
    }
    const btn = document.getElementById('cm-add-conf-btn');
    btn.disabled = true;
    try {
      await FSC.post(`/leagues/${currentOpts.leagueId}/conferences`, { name });
      nameInput.value = '';
      showSuccess(`Created conference "${name}"!`);
      await reload();
      if (currentOpts && typeof currentOpts.onUpdated === 'function') {
        currentOpts.onUpdated();
      }
    } catch (e) {
      showError(e.message || 'Failed to add conference');
    } finally {
      btn.disabled = false;
    }
  }

  async function doAddDivision(confId, name) {
    showError('');
    showSuccess('');
    try {
      await FSC.post(`/leagues/${currentOpts.leagueId}/conferences/${confId}/divisions`, { name });
      showSuccess(`Created division "${name}"!`);
      await reload();
      if (currentOpts && typeof currentOpts.onUpdated === 'function') {
        currentOpts.onUpdated();
      }
    } catch (e) {
      showError(e.message || 'Failed to add division');
    }
  }

  async function doRenameConference(confId, newName) {
    showError('');
    showSuccess('');
    try {
      await FSC.patch(`/leagues/${currentOpts.leagueId}/conferences/${confId}`, { name: newName });
      showSuccess(`Renamed to "${newName}"!`);
      await reload();
      if (currentOpts && typeof currentOpts.onUpdated === 'function') {
        currentOpts.onUpdated();
      }
    } catch (e) {
      showError(e.message || 'Failed to rename conference');
    }
  }

  async function doRenameDivision(divId, newName) {
    showError('');
    showSuccess('');
    try {
      await FSC.patch(`/leagues/${currentOpts.leagueId}/divisions/${divId}`, { name: newName });
      showSuccess(`Renamed division to "${newName}"!`);
      await reload();
      if (currentOpts && typeof currentOpts.onUpdated === 'function') {
        currentOpts.onUpdated();
      }
    } catch (e) {
      showError(e.message || 'Failed to rename division');
    }
  }

  async function doDeleteConference(confId) {
    showError('');
    showSuccess('');
    try {
      await FSC.del(`/leagues/${currentOpts.leagueId}/conferences/${confId}`);
      showSuccess('Conference deleted.');
      await reload();
      if (currentOpts && typeof currentOpts.onUpdated === 'function') {
        currentOpts.onUpdated();
      }
    } catch (e) {
      showError(e.message || 'Failed to delete conference');
    }
  }

  async function doDeleteDivision(divId) {
    showError('');
    showSuccess('');
    try {
      await FSC.del(`/leagues/${currentOpts.leagueId}/divisions/${divId}`);
      showSuccess('Division deleted.');
      await reload();
      if (currentOpts && typeof currentOpts.onUpdated === 'function') {
        currentOpts.onUpdated();
      }
    } catch (e) {
      showError(e.message || 'Failed to delete division');
    }
  }

  async function doSaveAlignment() {
    showError('');
    showSuccess('');
    const btn = document.getElementById('cm-save-alignment-btn');
    btn.disabled = true;
    try {
      const selects = document.querySelectorAll('.cm-team-align-select');
      const assignments = Array.from(selects).map(sel => {
        const val = sel.value;
        const tid = Number(sel.getAttribute('data-team-id'));
        if (!val) {
          return { team_id: tid, conference_id: null, division_id: null };
        }
        if (val.startsWith('div_')) {
          return { team_id: tid, division_id: Number(val.slice(4)) };
        }
        if (val.startsWith('conf_')) {
          return { team_id: tid, conference_id: Number(val.slice(5)), division_id: null };
        }
        return { team_id: tid, conference_id: null, division_id: null };
      });

      await FSC.post(`/leagues/${currentOpts.leagueId}/conferences/align-teams`, { assignments });
      showSuccess('Team alignment updated successfully!');
      await reload();
      if (currentOpts && typeof currentOpts.onUpdated === 'function') {
        currentOpts.onUpdated();
      }
    } catch (e) {
      showError(e.message || 'Failed to update alignment');
    } finally {
      btn.disabled = false;
    }
  }

  function open(opts) {
    ensureModal();
    currentOpts = opts;
    modalEl.style.display = 'flex';
    document.getElementById('cm-new-conf-name').value = '';
    reload();
  }

  function close() {
    if (modalEl) modalEl.style.display = 'none';
  }

  return { open, close };
})();
