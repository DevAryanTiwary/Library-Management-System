'use strict';
(() => {
  const UI = window.SmartLibUI;
  const {state, api, escape:esc, icon, money, pageHeader, toast, openModal,
    closeModal, modalActions, bindModalSubmit, registerRoute, renderRoute, showAuth, showApp} = UI;
  const $ = selector => document.querySelector(selector);
  const errorBox = '<div class="form-error" role="alert" hidden></div>';
  const limit = () => Number(state.backup_max_mb || 64);

  registerRoute('settings', async () => {
    state.settings = (await api('/settings')).settings;
    const policy = state.settings;
    return `${pageHeader('LIBRARY PREFERENCES','Settings & data','Set the lending policy and keep your library data protected.')}
      <div class="two-column">
        <section class="panel"><div class="panel-header"><div><h2>Lending policy</h2><p>A few simple rules for a well-run library.</p></div>${icon('settings')}</div>
          <div class="panel-body"><form id="policy-form" class="settings-form">
            <label>Loan period <input type="number" name="loan_days" required min="1" max="365" step="1" value="${Number(policy.loan_days)}"><span class="field-hint">Days a student can keep a newly issued book.</span></label>
            <label>Maximum active loans <input type="number" name="max_loans" required min="1" max="50" step="1" value="${Number(policy.max_loans)}"><span class="field-hint">Books each student may borrow at the same time.</span></label>
            <label>Fine per late day (₹) <input type="number" name="fine_per_day" required min="0" max="1000" step="0.01" value="${Number(policy.fine_per_day)}"><span class="field-hint">Current rate: ${money(policy.fine_per_day)} per calendar day after the due date.</span></label>
            <div class="info-note">${icon('info')}<span>Changes apply to new loans. Existing loans keep their original due dates and fine rates.</span></div>${errorBox}
            <button class="btn btn-primary" type="submit">${icon('check')} Save lending policy</button>
          </form></div></section>
        <section class="panel"><div class="panel-header"><div><h2>Backup & restore</h2><p>Your records, ready for a fresh start.</p></div>${icon('download')}</div>
          <div class="panel-body"><div class="backup-action"><span class="backup-icon">${icon('download')}</span><div><h3>Download a backup</h3><p>Save books, accounts, loans, fines, and policy in one SQLite file.</p></div><button class="btn btn-secondary" type="button" data-action="backup-download">Download</button></div>
            <div class="backup-action"><span class="backup-icon purple">${icon('upload')}</span><div><h3>Restore from a backup</h3><p>Replace the current records with a previous SmartLibPro backup.</p></div><button class="btn btn-secondary" type="button" data-action="backup-restore">Restore</button></div>
            <p class="backup-notes">Keep a copy somewhere separate from this computer. Backups include account and password records, so store them securely. The current file limit is ${limit()} MB.</p>
            <div class="info-note">${icon('info')}<span>Restore validates your backup and saves a safety copy in the backups folder beside the library database. Everyone will need to sign in again.</span></div>
          </div></section>
      </div>`;
  });

  registerRoute('profile', async () => {
    const user = state.user;
    const initials = user.name.trim().split(/\s+/).slice(0,2).map(word=>word[0]).join('').toUpperCase();
    return `${pageHeader('YOUR LIBRARY MEMBERSHIP','My account','Keep your details up to date and make your account your own.')}
      <section class="panel"><div class="panel-header"><div><h2>Profile & security</h2><p>Your details, all in one place.</p></div>${icon('profile')}</div><div class="panel-body">
        <div class="profile-summary"><span class="avatar">${esc(initials)}</span><div><h2>${esc(user.name)}</h2><p>${esc(user.email)}${user.student_id?' · '+esc(user.student_id):''}</p><span class="badge">${user.role==='admin'?'Administrator':'Student member'}</span></div></div>
        <form id="profile-form" class="profile-form"><div class="form-row"><label>Full name<input name="name" required minlength="2" maxlength="100" autocomplete="name" value="${esc(user.name)}"></label><label>Department<input name="department" maxlength="100" value="${esc(user.department||'')}"></label></div>
          <label>Email address<input class="read-only-field" type="email" value="${esc(user.email)}" readonly><span class="field-hint">Contact your library administrator to update your email or student ID.</span></label>
          <h3 class="subsection-title">Change password</h3><p>Leave these fields empty to keep your current password.</p>
          <label>Current password<input type="password" name="current_password" maxlength="128" autocomplete="current-password"></label>
          <div class="form-row"><label>New password<input type="password" name="new_password" minlength="8" maxlength="128" autocomplete="new-password"><span class="field-hint">Use 8–128 characters.</span></label><label>Confirm new password<input type="password" name="confirm_password" minlength="8" maxlength="128" autocomplete="new-password"></label></div>
          <div class="info-note">${icon('info')}<span>Changing your password signs out other sessions for your account.</span></div>${errorBox}
          <button class="btn btn-primary" type="submit">${icon('check')} Save account details</button>
        </form></div></section>`;
  });

  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!['policy-form','profile-form'].includes(form.id)) return;
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    const error = form.querySelector('.form-error');
    error.hidden = true;
    button.disabled = true;
    const values = Object.fromEntries(new FormData(form));
    try {
      if (form.id === 'policy-form') {
        const response = await api('/settings',{method:'PUT',body:{loan_days:Number(values.loan_days),max_loans:Number(values.max_loans),fine_per_day:values.fine_per_day}});
        state.settings = response.settings;
        toast('Lending policy saved. Existing loans keep their original terms.');
        await renderRoute();
      } else {
        if (values.new_password && values.new_password !== values.confirm_password) throw new Error('The new passwords do not match.');
        if (values.new_password && !values.current_password) throw new Error('Enter your current password to choose a new one.');
        const response = await api('/profile',{method:'PUT',body:{name:values.name,department:values.department,current_password:values.current_password,new_password:values.new_password}});
        state.user = response.user;
        showApp();
        toast(values.new_password?'Profile and password updated.':'Your account details have been saved.');
      }
    } catch (failure) {
      if (form.isConnected) { error.textContent = failure.message; error.hidden = false; }
      else toast(failure.message,'error');
    } finally { button.disabled = false; }
  });

  async function download(button) {
    button.disabled = true;
    try {
      const response = await fetch('/api/backup',{credentials:'same-origin'});
      if (!response.ok) {
        const payload = await response.json();
        if (response.status === 401) {
          const current = await api('/session');
          state.csrf = current.csrf_token;
          showAuth();
        }
        throw new Error(payload.error || 'The backup could not be downloaded.');
      }
      const blob = await response.blob();
      const link = document.createElement('a');
      const objectURL = URL.createObjectURL(blob);
      link.href = objectURL;
      const disposition = response.headers.get('Content-Disposition') || '';
      link.download = /filename="?([^";]+)/.exec(disposition)?.[1] || 'smartlib-backup.sqlite3';
      document.body.append(link); link.click(); link.remove();
      setTimeout(()=>URL.revokeObjectURL(objectURL),10000);
      toast('Backup downloaded. Keep a copy in a safe place.');
    } catch (error) { toast(error.message,'error'); }
    finally { button.disabled = false; }
  }

  function restore() {
    openModal('Restore your library','Replace the current data with a previous SmartLibPro backup.',`
      <form class="modal-form"><div class="modal-warning">This replaces all current books, accounts, loans, and settings. A safety backup is saved first. All signed-in accounts will be signed out.</div>
        <label>Backup file<input name="file" type="file" accept=".sqlite3,.db,.sqlite" required><span class="field-hint">Choose a SmartLibPro SQLite backup, up to ${limit()} MB.</span></label>
        <label>Your administrator password<input name="password" type="password" required maxlength="128" autocomplete="current-password"></label>
        <label>Type RESTORE to confirm<input name="confirmation" required pattern="RESTORE" placeholder="RESTORE" autocomplete="off"></label>
        ${modalActions('Restore backup','danger')}</form>`);
    bindModalSubmit(async (values,form) => {
      const file = form.querySelector('[name="file"]').files[0];
      if (file.size > limit()*1024*1024) throw new Error(`Choose a backup smaller than ${limit()} MB.`);
      const response = await api('/restore',{method:'POST',body:new FormData(form)});
      state.csrf = response.csrf_token;
      state.user = null;
      state.books = []; state.loans = []; state.users = [];
      closeModal(); showAuth();
      toast(response.message || 'Backup restored. Please sign in again.');
    });
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button || button.disabled) return;
    if (button.dataset.action === 'backup-download') download(button);
    if (button.dataset.action === 'backup-restore') restore();
  });
})();
