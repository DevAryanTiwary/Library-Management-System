/* Administrator member records and book maintenance. */
(() => {
  'use strict';
  const ui = window.SmartLibUI;
  const { state, api, escape: esc, icon, money, openModal, closeModal,
    toast, renderRoute, modalActions, bindModalSubmit } = ui;
  let searchTimer;
  let searchSequence = 0;

  function memberRows() {
    if (!state.users.length) return ui.empty('No members found',
      state.filters.users.q ? 'Try another name, email address, or student ID.' : 'Add a student to welcome your first reader.', 'users');
    return `<div class="table-wrap"><table class="data-table"><thead><tr><th>Member</th><th>Student ID / role</th><th>Department</th><th>On loan</th><th>Outstanding fines</th><th>Status</th><th>Actions</th></tr></thead><tbody>${state.users.map(user => {
      const initials = user.name.trim().split(/\s+/).slice(0, 2).map(part => part[0]).join('').toUpperCase();
      return `<tr><td><div class="user-cell"><span class="avatar row-avatar" aria-hidden="true">${esc(initials)}</span><div><span class="cell-main" title="${esc(user.name)}">${esc(user.name)}</span><span class="cell-sub">${esc(user.email)}</span></div></div></td><td><span class="cell-main">${esc(user.student_id || 'Administrator')}</span><span class="cell-sub">${user.role === 'admin' ? 'Administrator' : 'Student'}</span></td><td>${esc(user.department || '—')}</td><td>${Number(user.active_loans || 0)}</td><td><span class="fine-amount ${Number(user.unpaid_fines) > 0 ? 'unpaid' : ''}">${money(user.unpaid_fines)}</span></td><td><span class="badge ${user.active ? '' : 'inactive'}">${user.active ? 'Active' : 'Inactive'}</span></td><td><div class="cell-actions"><button class="icon-btn" type="button" data-action="user-edit" data-id="${user.id}" aria-label="Edit ${esc(user.name)}" title="Edit member">${icon('edit')}</button>${user.active && Number(user.id) !== Number(state.user.id) ? `<button class="icon-btn" type="button" data-action="user-deactivate" data-id="${user.id}" aria-label="Deactivate ${esc(user.name)}" title="Deactivate member">${icon('trash')}</button>` : ''}</div></td></tr>`;
    }).join('')}</tbody></table></div>`;
  }

  function memberResults() {
    const students = state.users.filter(user => user.role === 'student' && user.active).length;
    const inactive = state.users.filter(user => !user.active).length;
    return `<div class="summary-strip"><span><i></i><strong>${state.users.length}</strong> members in this view</span><span><i></i><strong>${students}</strong> active students</span><span><i></i><strong>${inactive}</strong> inactive accounts</span></div><section class="panel">${memberRows()}<div class="table-footer"><span>Inactive accounts retain their books, fines, and history.</span><span>Edit an inactive account to reactivate it.</span></div></section>`;
  }

  ui.registerRoute('users', async () => {
    const result = await api(`/users?${new URLSearchParams(state.filters.users)}`);
    state.users = result.users;
    return `${ui.pageHeader('YOUR LIBRARY COMMUNITY', 'Members', 'Manage student and administrator accounts.', `<button class="btn btn-primary" type="button" data-action="user-add">${icon('plus')} Add member</button>`)}<div class="toolbar"><label class="search-field">${icon('search')}<input type="search" data-member-search value="${esc(state.filters.users.q)}" aria-label="Search members by name, email, or student ID" placeholder="Search by name, email, or student ID…" autocomplete="off" maxlength="200"></label><span class="filter-note">Student and administrator records</span></div><div id="member-results">${memberResults()}</div>`;
  });

  function memberForm(user = null) {
    const editing = Boolean(user);
    const selected = user || { name: '', email: '', student_id: '', department: '', role: 'student', active: true };
    openModal(editing ? 'Edit member' : 'Add a library member', editing ? 'Keep membership details current.' : 'Welcome a new reader or administrator.',
      `<form class="modal-form"><label>Full name<input name="name" required minlength="2" maxlength="100" autocomplete="name" value="${esc(selected.name)}"></label><label>Email address<input name="email" type="email" required maxlength="254" autocomplete="email" value="${esc(selected.email)}"></label>${editing ? `<div class="info-note">${icon('info')}<span>Account role: ${selected.role === 'admin' ? 'Administrator' : 'Student'}. Roles are fixed for existing accounts.</span></div>` : '<label>Account role<select name="role"><option value="student">Student</option><option value="admin">Administrator</option></select></label>'}<div class="form-row"><label>Student ID <span class="optional">optional</span><input name="student_id" maxlength="40" value="${esc(selected.student_id || '')}" ${editing && selected.role === 'admin' ? 'disabled' : ''}><span class="field-hint">A unique ID is generated if left blank. IDs apply to students.</span></label><label>Department <span class="optional">optional</span><input name="department" maxlength="100" value="${esc(selected.department || '')}"></label></div><label>${editing ? 'New password' : 'Initial password'}${editing ? ' <span class="optional">optional</span>' : ''}<input name="password" type="password" minlength="8" maxlength="128" autocomplete="new-password" ${editing ? '' : 'required'} placeholder="${editing ? 'Leave blank to keep the current password' : '8–128 characters'}"><span class="field-hint">${editing ? 'Changing the password signs this member out on other sessions.' : 'Share this password with the member so they can sign in and change it.'}</span></label>${editing ? `<label class="checkbox-field"><input type="checkbox" name="active" ${selected.active ? 'checked' : ''} ${Number(selected.id) === Number(state.user.id) ? 'disabled' : ''}>Account is active</label><span class="field-hint">Inactive accounts cannot sign in. Their loan history and outstanding fines remain available.</span>` : ''}${modalActions(editing ? 'Save changes' : 'Create member')}</form>`);
    bindModalSubmit(async (values, form) => {
      const payload = { name: values.name, email: values.email, student_id: values.student_id || '', department: values.department };
      if (editing) payload.active = Number(selected.id) === Number(state.user.id) ? true : form.elements.active.checked;
      else payload.role = values.role;
      if (values.password) payload.password = values.password;
      const result = await api(editing ? `/users/${selected.id}` : '/users', { method: editing ? 'PUT' : 'POST', body: payload });
      if (editing && Number(selected.id) === Number(state.user.id)) {
        state.user = result.user;
        document.querySelector('#sidebar-name').textContent = result.user.name;
        document.querySelector('#sidebar-avatar').textContent = result.user.name.trim().split(/\s+/).slice(0, 2).map(part => part[0]).join('').toUpperCase();
      }
      closeModal();
      toast(editing ? 'Member details saved.' : 'Library member created.');
      renderRoute();
    });
  }

  function deactivateMember(user) {
    openModal('Deactivate member', 'Preserve their records while stopping account access.',
      `<form class="modal-form"><div class="confirm-detail"><strong>${esc(user.name)}</strong><br>${esc(user.email)}<br>${Number(user.active_loans || 0)} active loans · ${money(user.unpaid_fines)} outstanding</div><p class="confirm-copy">This member will be signed out and unable to sign in. Their records, loans, and fines will be kept. An administrator can still return their issued books and settle fines. You can reactivate the member by editing their account.</p>${modalActions('Deactivate account', 'danger')}</form>`);
    bindModalSubmit(async () => {
      await api(`/users/${user.id}`, { method: 'DELETE' });
      closeModal();
      toast('Member deactivated. Their history has been preserved.');
      renderRoute();
    });
  }

  function bookForm(book = null) {
    const editing = Boolean(book);
    const selected = book || { title: '', author: '', isbn: '', category: 'General', description: '', total_copies: 1, issued_copies: 0 };
    openModal(editing ? 'Edit book' : 'Add a book', editing ? 'Keep your collection accurate and ready for readers.' : 'Make room for another good story.',
      `<form class="modal-form"><label>Title<input name="title" required maxlength="200" value="${esc(selected.title)}"></label><label>Author<input name="author" required maxlength="150" value="${esc(selected.author)}"></label><div class="form-row"><label>ISBN <span class="optional">optional</span><input name="isbn" maxlength="32" value="${esc(selected.isbn || '')}"></label><label>Category<input name="category" maxlength="60" value="${esc(selected.category || 'General')}"></label></div><label>Total copies<input name="total_copies" type="number" min="${Math.max(1, Number(selected.issued_copies || 0))}" max="10000" step="1" required value="${Number(selected.total_copies)}"><span class="field-hint">${editing ? `${Number(selected.issued_copies || 0)} copies are currently issued. Total copies must include those copies.` : 'Enter the total copies owned by the library.'}</span></label><label>Description <span class="optional">optional</span><textarea name="description" maxlength="2000" rows="4">${esc(selected.description || '')}</textarea></label>${modalActions(editing ? 'Save book' : 'Add book')}</form>`);
    bindModalSubmit(async values => {
      const payload = { ...values, total_copies: Number(values.total_copies) };
      await api(editing ? `/books/${selected.id}` : '/books', { method: editing ? 'PUT' : 'POST', body: payload });
      closeModal();
      toast(editing ? 'Book details saved.' : 'Book added to the collection.');
      renderRoute();
    });
  }

  function archiveBook(book) {
    if (Number(book.issued_copies) > 0) {
      toast(`Return all ${Number(book.issued_copies)} issued copies before archiving this book.`, 'error');
      return;
    }
    openModal('Archive book', 'Remove this title from the catalog and preserve borrowing history.',
      `<form class="modal-form"><div class="confirm-detail"><strong>${esc(book.title)}</strong><br>by ${esc(book.author)}<br>${Number(book.total_copies)} total copies</div><p class="confirm-copy">This book will no longer appear in the catalog or be available for new loans. Existing borrowing history will be kept.</p>${modalActions('Archive book', 'danger')}</form>`);
    bindModalSubmit(async () => {
      await api(`/books/${book.id}`, { method: 'DELETE' });
      closeModal();
      toast('Book archived. Borrowing history has been preserved.');
      renderRoute();
    });
  }

  document.querySelector('#main-content').addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button || state.user?.role !== 'admin') return;
    const action = button.dataset.action;
    if (action === 'user-add') return memberForm();
    if (action === 'book-add') return bookForm();
    if (action === 'user-edit' || action === 'user-deactivate') {
      const user = state.users.find(item => Number(item.id) === Number(button.dataset.id));
      if (!user) return toast('Refresh the members page before editing this account.', 'error');
      return action === 'user-edit' ? memberForm(user) : deactivateMember(user);
    }
    if (action === 'book-edit' || action === 'book-archive') {
      const book = state.books.find(item => Number(item.id) === Number(button.dataset.id));
      if (!book) return toast('Refresh the catalog before editing this book.', 'error');
      return action === 'book-edit' ? bookForm(book) : archiveBook(book);
    }
  });

  document.querySelector('#main-content').addEventListener('input', event => {
    if (!event.target.matches('[data-member-search]')) return;
    state.filters.users.q = event.target.value;
    clearTimeout(searchTimer);
    const sequence = ++searchSequence;
    searchTimer = setTimeout(async () => {
      const target = document.querySelector('#member-results');
      if (!target || state.page !== 'users') return;
      target.setAttribute('aria-busy', 'true');
      try {
        const result = await api(`/users?${new URLSearchParams(state.filters.users)}`);
        if (state.page === 'users' && sequence === searchSequence && target.isConnected) {
          state.users = result.users;
          target.innerHTML = memberResults();
        }
      } catch (error) {
        if (state.page === 'users' && sequence === searchSequence) toast(error.message, 'error');
      } finally {
        if (sequence === searchSequence) target.removeAttribute('aria-busy');
      }
    }, 260);
  });
})();
