// Shared Informational Scoring Modal
// Explains fantasy scoring rules simply and clearly across Hockey (NHL) and Football (NFL/CFB).
const ScoringModal = (() => {
  let modalEl = null;
  let activeTab = 'nhl';

  function ensureModal() {
    if (modalEl) return;
    modalEl = document.createElement('div');
    modalEl.id = 'scoring-modal-container';
    modalEl.style.cssText = 'display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.68);z-index:99999;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(3px);box-sizing:border-box;';
    modalEl.innerHTML = `
      <div class="card elev-lg" style="max-width:560px;width:100%;padding:var(--space-5);background:var(--color-bg);border:1px solid var(--color-divider);border-radius:var(--radius-md);box-shadow:0 16px 40px rgba(0,0,0,0.35);position:relative;max-height:90vh;overflow-y:auto;box-sizing:border-box;display:flex;flex-direction:column;gap:var(--space-4)">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div>
            <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--color-accent);margin-bottom:2px">League Guide</div>
            <h3 style="margin:0;font-size:20px;display:flex;align-items:center;gap:8px">
              <span>ℹ️</span>
              <span>How Scoring Works</span>
            </h3>
          </div>
          <button type="button" class="btn btn-ghost" id="sm-close-btn" aria-label="Close" style="padding:2px 8px;font-size:22px;line-height:1;cursor:pointer">&times;</button>
        </div>

        <div style="display:flex;gap:4px;border-bottom:1px solid var(--color-divider);padding-bottom:2px">
          <button type="button" class="btn btn-ghost sm-tab-btn" data-sport="nhl" style="font-weight:700;font-size:13px;padding:6px 14px;border-radius:var(--radius-md) var(--radius-md) 0 0;cursor:pointer">🏒 Hockey (NHL)</button>
          <button type="button" class="btn btn-ghost sm-tab-btn" data-sport="football" style="font-weight:700;font-size:13px;padding:6px 14px;border-radius:var(--radius-md) var(--radius-md) 0 0;cursor:pointer">🏈 Football (NFL / CFB)</button>
        </div>

        <div id="sm-tab-nhl" style="display:flex;flex-direction:column;gap:var(--space-3);font-size:13px;line-height:1.5">
          <div style="background:color-mix(in srgb, var(--color-primary, #0284c7) 8%, var(--color-surface));border-left:3px solid var(--color-primary, #0284c7);padding:10px 12px;border-radius:var(--radius-sm)">
            <strong style="color:var(--color-primary, #0284c7);font-size:14px">Goal-Based Head-to-Head Series</strong>
            <p style="margin:4px 0 0;color:var(--color-text)">Each week, you play a 3-game series against your matchup opponent. The team with the most total goals scored in each game wins!</p>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <div class="card elev-sm" style="padding:10px 12px;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:var(--radius-sm)">
              <div style="font-weight:700;margin-bottom:4px;color:var(--color-accent)">🥅 Scoring Chances</div>
              <ul style="margin:0;padding-left:16px;display:flex;flex-direction:column;gap:4px;color:color-mix(in srgb, var(--color-text) 85%, transparent)">
                <li><strong>Goals &amp; Assists:</strong> Real-world goals and primary/secondary assists trigger prime scoring chances.</li>
                <li><strong>Shots on Goal (SOG):</strong> High-danger shots close to the crease yield highest conversion rates.</li>
              </ul>
            </div>
            <div class="card elev-sm" style="padding:10px 12px;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:var(--radius-sm)">
              <div style="font-weight:700;margin-bottom:4px;color:var(--color-accent)">🛡️ Goaltending &amp; Defense</div>
              <ul style="margin:0;padding-left:16px;display:flex;flex-direction:column;gap:4px;color:color-mix(in srgb, var(--color-text) 85%, transparent)">
                <li><strong>Starting Goalies:</strong> Stop opponent shots based on real saves &amp; Goals Saved Above Expected (GSAx).</li>
                <li><strong>Defensemen:</strong> Blocked shots, takeaways, and hits suppress opponent scoring chances.</li>
              </ul>
            </div>
          </div>

          <div class="card elev-sm" style="padding:10px 12px;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:var(--radius-sm)">
            <div style="font-weight:700;margin-bottom:4px;color:var(--color-text)">⏱️ Live Cadence &amp; Transparency</div>
            <p style="margin:0 0 6px;color:color-mix(in srgb, var(--color-text) 85%, transparent)">
              Games simulate live across 3 periods of 20:00 as real NHL games play out.
            </p>
            <div style="display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:color-mix(in srgb, var(--color-text) 70%, transparent)">
              <span>✓ <strong>Period Clock:</strong> Displays exact time elapsed and remaining</span>
              <span>✓ <strong>Shot Counter (SOG):</strong> Shows running shot totals per side</span>
              <span>✓ <strong>Highlights:</strong> Watch every scoring play unfold in real time</span>
            </div>
          </div>
        </div>

        <div id="sm-tab-football" style="display:none;flex-direction:column;gap:var(--space-3);font-size:13px;line-height:1.5">
          <div style="background:color-mix(in srgb, var(--color-primary, #0284c7) 8%, var(--color-surface));border-left:3px solid var(--color-primary, #0284c7);padding:10px 12px;border-radius:var(--radius-sm)">
            <strong style="color:var(--color-primary, #0284c7);font-size:14px">Weekly Head-to-Head Points</strong>
            <p style="margin:4px 0 0;color:var(--color-text)">Your starting lineup accumulates fantasy points from official play-by-play actions each week. Highest total score wins the matchup.</p>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <div class="card elev-sm" style="padding:10px 12px;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:var(--radius-sm)">
              <div style="font-weight:700;margin-bottom:4px;color:var(--color-accent)">🏈 Offense</div>
              <ul style="margin:0;padding-left:16px;display:flex;flex-direction:column;gap:4px;color:color-mix(in srgb, var(--color-text) 85%, transparent)">
                <li><strong>Passing:</strong> 1 pt per 25 pass yards, 4 pts per TD (-2 per INT).</li>
                <li><strong>Rushing/Receiving:</strong> 1 pt per 10 yards, 6 pts per TD.</li>
              </ul>
            </div>
            <div class="card elev-sm" style="padding:10px 12px;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:var(--radius-sm)">
              <div style="font-weight:700;margin-bottom:4px;color:var(--color-accent)">🛡️ Kicking &amp; Defense</div>
              <ul style="margin:0;padding-left:16px;display:flex;flex-direction:column;gap:4px;color:color-mix(in srgb, var(--color-text) 85%, transparent)">
                <li><strong>Kicking:</strong> Field Goals (3 pts + yardage bonus), PATs (1 pt).</li>
                <li><strong>Defense:</strong> Sacks, turnovers, safeties, defensive TDs.</li>
              </ul>
            </div>
          </div>

          <div class="card elev-sm" style="padding:10px 12px;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:var(--radius-sm)">
            <div style="font-weight:700;margin-bottom:4px;color:var(--color-text)">📊 Calibrated Attribution</div>
            <p style="margin:0;color:color-mix(in srgb, var(--color-text) 85%, transparent)">
              Points are generated directly from official real-world play credits, giving an authentic reflection of on-field player impact.
            </p>
          </div>
        </div>

        <div style="display:flex;justify-content:flex-end;align-items:center;padding-top:var(--space-2);border-top:1px solid var(--color-divider)">
          <button type="button" class="btn btn-primary" id="sm-got-it-btn" style="padding:6px 18px;font-size:13px;cursor:pointer">Got it</button>
        </div>
      </div>
    `;

    document.body.appendChild(modalEl);

    const close = () => {
      modalEl.style.display = 'none';
    };

    modalEl.querySelector('#sm-close-btn').onclick = close;
    modalEl.querySelector('#sm-got-it-btn').onclick = close;
    modalEl.onclick = (e) => {
      if (e.target === modalEl) close();
    };

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && modalEl && modalEl.style.display !== 'none') {
        close();
      }
    });

    modalEl.querySelectorAll('.sm-tab-btn').forEach(btn => {
      btn.onclick = () => {
        setTab(btn.getAttribute('data-sport'));
      };
    });
  }

  function setTab(sport) {
    activeTab = (sport === 'football' || sport === 'nfl' || sport === 'cfb') ? 'football' : 'nhl';
    if (!modalEl) return;

    modalEl.querySelectorAll('.sm-tab-btn').forEach(b => {
      const isTarget = b.getAttribute('data-sport') === activeTab;
      b.style.borderBottom = isTarget ? '2px solid var(--color-accent)' : '2px solid transparent';
      b.style.color = isTarget ? 'var(--color-accent)' : 'inherit';
      b.style.opacity = isTarget ? '1' : '0.7';
    });

    const nhlContent = modalEl.querySelector('#sm-tab-nhl');
    const fbContent = modalEl.querySelector('#sm-tab-football');
    if (nhlContent) nhlContent.style.display = activeTab === 'nhl' ? 'flex' : 'none';
    if (fbContent) fbContent.style.display = activeTab === 'football' ? 'flex' : 'none';
  }

  return {
    open(opts = {}) {
      ensureModal();
      const sport = opts.sport || 'nhl';
      setTab(sport);
      modalEl.style.display = 'flex';
    },
    close() {
      if (modalEl) modalEl.style.display = 'none';
    }
  };
})();
