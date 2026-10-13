// Shared Commissioner Team Assignment Modal
// Allows the commissioner at any point to assign an AI team to a user or convert a team back to AI.
const AssignTeamModal = (() => {
  let modalEl = null;
  let cachedUsers = null;
  let currentOpts = null;

  function ensureModal() {
    if (modalEl) return;
    modalEl = document.createElement('div');
    modalEl.id = 'assign-team-modal-container';
    modalEl.style.cssText = 'display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.65);z-index:99999;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(2px);box-sizing:border-box;';
    modalEl.innerHTML = `
      <div class="card elev-lg" style="max-width:480px;width:100%;padding:var(--space-5);background:var(--color-bg);border:1px solid var(--color-divider);border-radius:var(--radius-md);box-shadow:0 12px 32px rgba(0,0,0,0.25);position:relative;max-height:90vh;overflow-y:auto;box-sizing:border-box">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:var(--space-2)">
          <div>
            <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--color-accent);margin-bottom:2px">Commissioner Action</div>
            <h3 style="margin:0;font-size:20px" id="atm-title">Assign Team to User</h3>
          </div>
          <button type="button" class="btn btn-ghost" id="atm-close-btn" style="padding:2px 8px;font-size:20px;line-height:1;cursor:pointer">&times;</button>
        </div>
        <p class="text-muted" style="margin:0 0 var(--space-4);font-size:13px" id="atm-desc">
          Assign this AI team to a human manager. They will immediately take control of the team, its roster, and lineup decisions.
        </p>

        <div id="atm-error" style="display:none;background:var(--color-accent-2-100);color:var(--color-accent-2-800);padding:8px 12px;border-radius:var(--radius-md);margin-bottom:var(--space-3);font-size:13px"></div>

        <form id="atm-form" onsubmit="return false;">
          <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:var(--space-3)">
            <label for="atm-team-name" style="font-size:13px;font-weight:600">Team Name</label>
            <input type="text" id="atm-team-name" maxlength="80" style="padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--color-divider);background:var(--color-bg);color:var(--color-text);font-size:14px;box-sizing:border-box;width:100%">
          </div>

          <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:var(--space-3)">
            <label for="atm-email" style="font-size:13px;font-weight:600">Assign by Manager Email</label>
            <input type="email" id="atm-email" placeholder="manager@example.com" style="padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--color-divider);background:var(--color-bg);color:var(--color-text);font-size:14px;box-sizing:border-box;width:100%">
            <span class="hint" style="font-size:12px;color:color-mix(in srgb, var(--color-text) 55%, transparent)">
              Enter their email address. If they already have an account, the team transfers immediately. If not, an invite link is generated for them to join and claim it.
            </span>
          </div>

          <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:var(--space-4)">
            <label for="atm-user-select" style="font-size:13px;font-weight:600">Or Select Existing Member</label>
            <select id="atm-user-select" style="padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--color-divider);background:var(--color-bg);color:var(--color-text);font-size:14px;box-sizing:border-box;width:100%">
              <option value="">-- Choose registered account --</option>
            </select>
          </div>

          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:var(--space-2);padding-top:var(--space-2);border-top:1px solid var(--color-divider)">
            <button type="button" class="btn btn-ghost" id="atm-convert-ai-btn" style="color:var(--color-accent-2);font-size:12px;padding:4px 8px;display:none">Convert to AI Team</button>
            <div style="display:flex;gap:var(--space-2);margin-left:auto">
              <button type="button" class="btn btn-secondary" id="atm-cancel-btn">Cancel</button>
              <button type="submit" class="btn btn-primary" id="atm-submit-btn">Assign Team</button>
            </div>
          </div>
        </form>

        <div id="atm-success" style="display:none;margin-top:var(--space-4);padding:var(--space-3);border:1px solid var(--color-accent);border-radius:var(--radius-md);background:color-mix(in srgb, var(--color-accent) 6%, transparent)">
          <div style="font-weight:600;font-size:14px;color:var(--color-accent);margin-bottom:4px" id="atm-success-msg">Team assigned successfully!</div>
          <div id="atm-invite-box" style="margin-top:var(--space-2)">
            <div style="font-size:12px;margin-bottom:4px;color:var(--color-text)">Share invitation link with manager:</div>
            <div style="display:flex;gap:var(--space-2);align-items:center;margin-bottom:var(--space-3)">
              <input type="text" id="atm-invite-url" readonly style="flex:1;padding:6px 10px;font-size:12px;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:var(--radius-md);color:var(--color-text);box-sizing:border-box">
              <button type="button" class="btn btn-secondary" id="atm-copy-btn" style="font-size:12px;padding:6px 10px">Copy</button>
            </div>
            <div style="display:flex;gap:var(--space-2);justify-content:flex-end">
              <a id="atm-mailto-btn" href="#" class="btn btn-secondary" style="font-size:12px;padding:6px 10px">Send via Email App</a>
              <button type="button" class="btn btn-primary" id="atm-done-btn" style="font-size:12px;padding:6px 14px">Done</button>
            </div>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(modalEl);

    // Event wiring
    document.getElementById('atm-close-btn').onclick = close;
    document.getElementById('atm-cancel-btn').onclick = close;
    modalEl.onclick = (e) => { if (e.target === modalEl) close(); };

    document.getElementById('atm-user-select').onchange = (e) => {
      const selectedId = e.target.value;
      if (selectedId && cachedUsers) {
        const u = cachedUsers.find(x => String(x.id) === String(selectedId));
        if (u && u.email && !u.email.endsWith('@users.invalid')) {
          document.getElementById('atm-email').value = u.email;
        }
      }
    };

    document.getElementById('atm-submit-btn').onclick = doSubmit;
    document.getElementById('atm-convert-ai-btn').onclick = doConvertToAi;

    document.getElementById('atm-copy-btn').onclick = () => {
      const urlInput = document.getElementById('atm-invite-url');
      navigator.clipboard.writeText(urlInput.value).then(() => {
        const btn = document.getElementById('atm-copy-btn');
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = orig, 2000);
      }).catch(() => prompt('Copy invite link:', urlInput.value));
    };

    document.getElementById('atm-done-btn').onclick = () => {
      close();
      if (currentOpts && typeof currentOpts.onAssigned === 'function') {
        currentOpts.onAssigned();
      }
    };
  }

  function showError(msg) {
    const err = document.getElementById('atm-error');
    err.textContent = msg;
    err.style.display = msg ? 'block' : 'none';
  }

  async function loadUsers() {
    if (cachedUsers) return cachedUsers;
    try {
      const users = await fetch('/users').then(r => r.json());
      // Filter out auto-generated guest addresses for member dropdown
      cachedUsers = users.filter(u => u.email && !u.email.endsWith('@users.invalid'));
      return cachedUsers;
    } catch (e) {
      cachedUsers = [];
      return cachedUsers;
    }
  }

  async function open(opts) {
    ensureModal();
    currentOpts = opts;
    showError('');
    document.getElementById('atm-form').style.display = 'block';
    document.getElementById('atm-success').style.display = 'none';

    const isAi = !!opts.isAi;
    document.getElementById('atm-title').textContent = isAi ? `Assign AI Team: "${opts.teamName}"` : `Reassign Team: "${opts.teamName}"`;
    document.getElementById('atm-desc').textContent = isAi
      ? `Assign this AI team to a human manager. They will immediately take control of "${opts.teamName}", its roster, and lineup decisions.`
      : `Change the manager for "${opts.teamName}" or convert it to an AI team.`;

    document.getElementById('atm-team-name').value = opts.teamName || '';
    document.getElementById('atm-email').value = opts.currentEmail || '';

    const convertBtn = document.getElementById('atm-convert-ai-btn');
    convertBtn.style.display = isAi ? 'none' : 'inline-block';

    // Populate user picker
    const userSelect = document.getElementById('atm-user-select');
    userSelect.innerHTML = '<option value="">-- Choose registered account --</option>';
    const users = await loadUsers();
    users.forEach(u => {
      const opt = document.createElement('option');
      opt.value = u.id;
      opt.textContent = `${u.display_name} (${u.email})`;
      userSelect.appendChild(opt);
    });

    modalEl.style.display = 'flex';
    document.getElementById('atm-email').focus();
  }

  function close() {
    if (modalEl) modalEl.style.display = 'none';
    currentOpts = null;
  }

  async function doSubmit() {
    showError('');
    if (!currentOpts) return;

    const teamName = document.getElementById('atm-team-name').value.trim();
    if (!teamName) return showError('Team name cannot be empty.');

    const email = document.getElementById('atm-email').value.trim().toLowerCase();
    const selectedUserId = document.getElementById('atm-user-select').value;

    if (!email && !selectedUserId) {
      return showError('Please enter a manager email or select an existing member.');
    }

    if (email && (!email.includes('@') || !email.includes('.'))) {
      return showError('Please enter a valid email address.');
    }

    const submitBtn = document.getElementById('atm-submit-btn');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Assigning...';

    try {
      const payload = {
        team_name: teamName,
        email: email || null,
        user_id: selectedUserId ? Number(selectedUserId) : null,
        is_ai: false,
      };

      const res = await FSC.api(`/leagues/${currentOpts.leagueId}/teams/${currentOpts.teamId}/assign`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });

      document.getElementById('atm-form').style.display = 'none';
      const successBox = document.getElementById('atm-success');
      successBox.style.display = 'block';

      document.getElementById('atm-success-msg').textContent =
        `"${res.team_name}" is now assigned to ${res.owner_name} (${res.owner_email || 'Member'})!`;

      const fullInviteUrl = `${location.origin}${res.invite_url}`;
      document.getElementById('atm-invite-url').value = fullInviteUrl;

      const mailSubject = encodeURIComponent(`You've been assigned team "${res.team_name}"`);
      const mailBody = encodeURIComponent(`You've been assigned as manager of "${res.team_name}" on FantasyMotion!\n\nClaim and manage your team here:\n${fullInviteUrl}`);
      document.getElementById('atm-mailto-btn').href = `mailto:${encodeURIComponent(res.owner_email || '')}?subject=${mailSubject}&body=${mailBody}`;

    } catch (err) {
      showError(err.message || 'Failed to assign team');
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Assign Team';
    }
  }

  async function doConvertToAi() {
    if (!currentOpts) return;
    const confirmed = confirm(`Are you sure you want to convert "${currentOpts.teamName}" to an AI team? The current manager will no longer control this team.`);
    if (!confirmed) return;

    showError('');
    try {
      const res = await FSC.api(`/leagues/${currentOpts.leagueId}/teams/${currentOpts.teamId}/assign`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ is_ai: true }),
      });

      close();
      if (currentOpts && typeof currentOpts.onAssigned === 'function') {
        currentOpts.onAssigned();
      }
    } catch (err) {
      showError(err.message || 'Failed to convert team to AI');
    }
  }

  return { open, close };
})();
