// Shared Conference and Division Management Modal
// Allows the commissioner to create, rename, delete conferences/divisions and align teams before/after the season.
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
      <div class="card elev-lg" style="max-width:680px;width:100%;padding:var(--space-5);background:var(--color-bg);border:1px solid var(--color-divider);border-radius:var(--radius-md);box-shadow:0 12px 32px rgba(0,0,0,0.25);position:relative;max-height:90vh;overflow-y:auto;box-sizing:border-box">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:var(--space-2)">
          <div>
            <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--color-accent);margin-bottom:2px">Commissioner Control Room</div>
            <h3 style="margin:0;font-size:20px" id="cm-modal-title">Manage Conferences &amp; Divisions</h3>
          </div>
          <button type="button" class="btn btn-ghost" id="cm-close-btn" style="padding:2px 8px;font-size:20px;line-height:1;cursor:pointer">&times;</button>
        </div>
        <p class="text-muted" style="margin:0 0 var(--space-4);font-size:13px" id="cm-modal-desc">
          Organize your league into custom conferences and divisions. Add, rename, or remove groups, and assign teams below.
        </p>

        <div id="cm-banner-locked" style="display:none;background:#451a03;color:#fbbf24;border:1px solid #78350f;padding:10px 14px;border-radius:var(--radius-md);margin-bottom:var(--space-4);font-size:13px">
          <strong>Season in progress:</strong> Conferences and divisions are locked while regular season games or series are active. Realignment unlocks before or after the season.
        </div>

        <div id="cm-error" style="display:none;background:var(--color-accent-2-100, #fee2e2);color:var(--color-accent-2-800, #991b1b);padding:8px 12px;border-radius:var(--radius-md);margin-bottom:var(--space-3);font-size:13px"></div>
        <div id="cm-success" style="display:none;background:#052e16;color:#4ade80;border:1px solid #166534;padding:8px 12px;border-radius:var(--radius-md);margin-bottom:var(--space-3);font-size:13px"></div>

        <!-- Section 1: Conferences & Divisions List -->
        <div style="margin-bottom:var(--space-5)">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--space-2)">
            <h4 style="margin:0;font-size:15px">Conferences &amp; Divisions</h4>
            <span class="text-muted" style="font-size:12px" id="cm-conf-count"></span>
          </div>

          <div id="cm-confs-list" style="display:flex;flex-direction:column;gap:6px;margin-bottom:var(--space-3)"></div>

          <form id="cm-add-form" onsubmit="return false;" style="display:flex;gap:var(--space-2);align-items:center">
            <input type="text" id="cm-new-conf-name" placeholder="New conference or division name (e.g. Atlantic Division)" maxlength="80" style="flex:1;padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--color-divider);background:var(--color-bg);color:var(--color-text);font-size:13px;box-sizing:border-box">
            <button type="submit" class="btn btn-secondary" id="cm-add-conf-btn" style="font-size:13px;padding:8px 14px">Add Group</button>
          </form>
        </div>

        <!-- Section 2: Team Alignment -->
        <div style="margin-bottom:var(--space-4);padding-top:var(--space-3);border-top:1px solid var(--color-divider)">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--space-2)">
            <h4 style="margin:0;font-size:15px">Team Alignment</h4>
            <span class="text-muted" style="font-size:12px">Assign each team to a conference or division</span>
          </div>

          <div style="max-height:260px;overflow-y:auto;border:1px solid var(--color-divider);border-radius:var(--radius-md);margin-bottom:var(--space-3)">
            <table class="table" style="width:100%;margin:0;font-size:13px">
              <thead><tr><th style="padding:8px 12px">Team</th><th style="padding:8px 12px">Owner / Status</th><th style="padding:8px 12px">Conference / Division</th></tr></thead>
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

    document.getElementById('cm-add-conf-btn').onclick = doAddConference;
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
      showError(e.message || 'Failed to load conferences');
    }
  }

  function renderUI() {
    document.getElementById('cm-banner-locked').style.display = isEditable ? 'none' : 'block';
    document.getElementById('cm-new-conf-name').disabled = !isEditable;
    document.getElementById('cm-add-conf-btn').disabled = !isEditable;
    document.getElementById('cm-save-alignment-btn').disabled = !isEditable;

    document.getElementById('cm-conf-count').textContent = `${conferences.length} total`;

    // Render Conferences List
    const confsList = document.getElementById('cm-confs-list');
    confsList.innerHTML = conferences.map(c => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:var(--color-surface, rgba(255,255,255,0.03));border:1px solid var(--color-divider);border-radius:var(--radius-md);gap:8px">
        <div style="display:flex;align-items:baseline;gap:8px;flex:1">
          <strong style="font-size:14px">${esc(c.name)}</strong>
          <span class="text-muted" style="font-size:12px">${c.team_count} team${c.team_count === 1 ? '' : 's'}</span>
        </div>
        ${isEditable ? `
          <div style="display:flex;gap:4px">
            <button type="button" class="btn btn-ghost" style="font-size:11px;padding:3px 8px" data-cm-rename="${c.id}" data-cm-name="${esc(c.name)}">Rename</button>
            ${conferences.length > 1 ? `<button type="button" class="btn btn-ghost" style="font-size:11px;padding:3px 8px;color:var(--color-accent-2, #ef4444)" data-cm-delete="${c.id}" data-cm-name="${esc(c.name)}">Delete</button>` : ''}
          </div>
        ` : ''}
      </div>
    `).join('');

    // Wire rename & delete
    confsList.querySelectorAll('[data-cm-rename]').forEach(btn => {
      btn.onclick = () => {
        const cid = Number(btn.getAttribute('data-cm-rename'));
        const curName = btn.getAttribute('data-cm-name');
        const newName = prompt('Enter new name for this conference / division:', curName);
        if (newName && newName.trim() && newName.trim() !== curName) {
          doRenameConference(cid, newName.trim());
        }
      };
    });

    confsList.querySelectorAll('[data-cm-delete]').forEach(btn => {
      btn.onclick = () => {
        const cid = Number(btn.getAttribute('data-cm-delete'));
        const name = btn.getAttribute('data-cm-name');
        if (confirm(`Delete "${name}"? Teams assigned to it will be moved to another conference.`)) {
          doDeleteConference(cid);
        }
      };
    });

    // Render Team Alignment Table
    const alignBody = document.getElementById('cm-alignment-body');
    alignBody.innerHTML = teams.map(t => {
      const statusBadge = t.is_ai_owned
        ? '<span class="tag tag-neutral" style="font-size:11px">AI</span>'
        : (t.invite_status === 'pending_claim'
          ? `<span class="tag" style="font-size:11px;background:#451a03;color:#fbbf24">Pending (${esc(t.owner_email || '')})</span>`
          : `<span class="tag" style="font-size:11px;background:#052e16;color:#4ade80">Claimed (${esc(t.owner_name || 'Manager')})</span>`);

      return `<tr>
        <td style="padding:8px 12px;font-weight:600">${esc(t.name)}</td>
        <td style="padding:8px 12px">${statusBadge}</td>
        <td style="padding:8px 12px">
          <select class="inp cm-team-conf-select" data-team-id="${t.id}" ${!isEditable ? 'disabled' : ''} style="width:100%;padding:4px 8px;font-size:13px">
            <option value="">-- No Conference --</option>
            ${conferences.map(c => `<option value="${c.id}" ${t.conference_id === c.id ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
          </select>
        </td>
      </tr>`;
    }).join('');
  }

  async function doAddConference() {
    showError('');
    showSuccess('');
    const nameInput = document.getElementById('cm-new-conf-name');
    const name = nameInput.value.trim();
    if (!name) {
      showError('Please enter a conference or division name');
      return;
    }
    const btn = document.getElementById('cm-add-conf-btn');
    btn.disabled = true;
    try {
      await FSC.post(`/leagues/${currentOpts.leagueId}/conferences`, { name });
      nameInput.value = '';
      showSuccess(`Created "${name}"!`);
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

  async function doSaveAlignment() {
    showError('');
    showSuccess('');
    const btn = document.getElementById('cm-save-alignment-btn');
    btn.disabled = true;
    try {
      const selects = document.querySelectorAll('.cm-team-conf-select');
      const assignments = Array.from(selects).map(sel => ({
        team_id: Number(sel.getAttribute('data-team-id')),
        conference_id: sel.value ? Number(sel.value) : null,
      }));

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
