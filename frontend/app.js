(() => {
  'use strict';

  /* ========================================================================
     Setup & state
     ======================================================================== */

  const app = document.getElementById('app');
  const SESSION_KEY = 'kanso.session';

  const ORG_KEY = 'kanso.org';
  const ROLES = ['admin', 'member', 'reviewer'];

  // Mirrors the backend for showing or hiding buttons only.
  // The API enforces the real rules.
  const PERMISSIONS = {
    admin: ['read', 'write', 'delete', 'manage'],
    member: ['read', 'write'],
    reviewer: ['read'],
  };

  const STATUSES = [
    ['todo', 'To do'],
    ['in_progress', 'In progress'],
    ['done', 'Completed'],
  ];
  const STATUS_LABEL = Object.fromEntries(STATUSES);

  const APPROVAL_LABEL = {
    draft: 'Draft',
    submitted: 'Pending review',
    approved: 'Approved',
    changes_requested: 'Changes requested',
  };
  const ACTION_LABEL = {
    submit: 'Submit for review',
    withdraw: 'Withdraw',
    approve: 'Approve',
    request_changes: 'Request changes',
    reopen: 'Reopen',
  };
  const ACTION_PATH = {
    submit: 'submit',
    withdraw: 'withdraw',
    approve: 'approve',
    request_changes: 'request-changes',
    reopen: 'reopen',
  };

  let config;
  let session;
  let route = 'overview';
  let data = { orgs: [], org: null, members: [], projects: [], tasks: [], pending: [] };
  let filters = { search: '', project: '' };


  /* ========================================================================
     Helpers
     ======================================================================== */

  const ESCAPES = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  };

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ESCAPES[ch]);
  }

  function initials(text) {
    const parts = String(text || '?')
      .split(/[@\s.]+/)
      .filter(Boolean)
      .slice(0, 2);
    return (parts.map(part => part[0]).join('') || '?').toUpperCase();
  }

  function decodeToken(token) {
    const payload = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(payload));
  }

  function myRole() {
    return data.org ? data.org.my_role : null;
  }

  function can(action) {
    return (PERMISSIONS[myRole()] || []).includes(action);
  }

  function mySub() {
    return decodeToken(session.token).sub;
  }

  function orgPath() {
    return `/orgs/${data.org.id}`;
  }

  function savedOrgId() {
    try {
      return sessionStorage.getItem(ORG_KEY);
    } catch (err) {
      return null;
    }
  }

  function saveOrgId(id) {
    try {
      sessionStorage.setItem(ORG_KEY, id);
    } catch (err) {
      // Ignore storage errors
    }
  }

  function projectName(projectId) {
    const project = data.projects.find(p => p.id === projectId);
    return project ? project.name : 'Unknown';
  }

  function projectProgress(projectId) {
    const tasks = data.tasks.filter(t => t.project_id === projectId);
    const done = tasks.filter(t => t.status === 'done').length;
    return {
      total: tasks.length,
      done,
      percent: tasks.length ? Math.round((done / tasks.length) * 100) : 0,
    };
  }

  function progressBar(percent) {
    return `<progress value="${percent}" max="100"></progress>`;
  }


  /* ========================================================================
     Session & API
     ======================================================================== */

  function loadSession() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(SESSION_KEY));
      if (saved && saved.exp * 1000 > Date.now()) {
        return saved;
      }
    } catch (err) {
      // No usable session
    }
    return null;
  }

  function logout() {
    try {
      sessionStorage.removeItem(SESSION_KEY);
      sessionStorage.removeItem(ORG_KEY);
    } catch (err) {
      // Ignore storage errors
    }
    session = null;
    renderLogin();
  }

  async function signIn(email, password) {
    const region = config.region || 'eu-north-1';

    const response = await fetch(`https://cognito-idp.${region}.amazonaws.com/`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-amz-json-1.1',
        'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth',
      },
      body: JSON.stringify({
        AuthFlow: 'USER_PASSWORD_AUTH',
        ClientId: config.userPoolClientId || config.clientId,
        AuthParameters: { USERNAME: email, PASSWORD: password },
      }),
    });

    const result = await response.json();
    const token = result.AuthenticationResult && result.AuthenticationResult.IdToken;

    if (!response.ok || !token) {
      throw new Error(
        result.ChallengeName
          ? 'Password change required'
          : 'Incorrect email or password'
      );
    }

    const claims = decodeToken(token);
    session = {
      token,
      exp: claims.exp,
      email: claims.email || email,
      groups: claims['cognito:groups'] || [],
    };

    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
    } catch (err) {
      // Ignore storage errors
    }
  }

  async function api(method, path, body) {
    const headers = { Authorization: 'Bearer ' + session.token };
    if (body) {
      headers['Content-Type'] = 'application/json';
    }

    const response = await fetch(config.apiUrl.replace(/\/$/, '') + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });

    if (response.status === 401) {
      logout();
      throw new Error('Session expired');
    }
    if (response.status === 204) {
      return null;
    }

    const json = await response.json().catch(() => ({}));

    if (!response.ok) {
      if (json.errors) {
        throw new Error(json.errors.map(e => `${e.field}: ${e.message}`).join('; '));
      }
      throw new Error(json.detail || 'Request failed');
    }
    return json;
  }

  async function fetchAll(path) {
    let items = [];
    let cursor = null;

    do {
      const suffix = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
      const page = await api('GET', path + suffix);
      items = items.concat(page.items);
      cursor = page.next_cursor;
    } while (cursor);

    return items;
  }

  async function loadData() {
    const result = await api('GET', '/orgs');
    data.orgs = result.items;
    data.org = data.orgs.find(o => o.id === savedOrgId()) || data.orgs[0] || null;
    data.members = [];
    data.projects = [];
    data.tasks = [];
    data.pending = [];

    if (!data.org) {
      return;
    }
    saveOrgId(data.org.id);

    const base = orgPath();
    data.members = await fetchAll(`${base}/members`);
    data.projects = await fetchAll(`${base}/projects`);

    const perProject = await Promise.all(
      data.projects.map(project => fetchAll(`${base}/projects/${project.id}/tasks`))
    );
    data.tasks = perProject.flat();
    data.pending = await fetchAll(`${base}/approvals`);
  }

  async function refresh() {
    await loadData();
    app.innerHTML = '';
    render();
  }


  /* ========================================================================
     Login & shell
     ======================================================================== */

  function renderLogin(error = '') {
    app.innerHTML = `
      <div class="login">
        <form class="card login-card" data-form="login">
          <div class="brand dark">
            <span class="logo">侘</span>
            <span><b>KANSO</b><small>WORKSPACE</small></span>
          </div>
          <h1>Sign in</h1>
          <p class="mute">Use your workspace account.</p>
          <label>Email
            <input name="email" type="email" autocomplete="username" required>
          </label>
          <label>Password
            <input name="password" type="password" autocomplete="current-password" required>
          </label>
          <p class="err" role="alert">${esc(error)}</p>
          <button class="btn" type="submit">Sign in</button>
        </form>
      </div>`;
  }

  function navButton(key, label, icon) {
    const active = route === key ? ' on' : '';
    const count = key === 'approvals' ? data.pending.length : 0;
    const badge = count ? `<span class="badge">${count}</span>` : '';
    return `<button class="nav${active}" data-route="${key}"><i>${icon}</i>${label}${badge}</button>`;
  }

  function disabledNav(label, icon) {
    return `<button class="nav" disabled title="Coming soon"><i>${icon}</i>${label}</button>`;
  }

  function renderShell() {
    const role = myRole() || 'no organization';

    app.innerHTML = `
      <div class="shell">
        <aside class="side">
          <div class="brand">
            <span class="logo">侘</span>
            <span><b>KANSO</b><small>WORKSPACE</small></span>
          </div>

          <div class="label">WORKSPACE</div>
          ${navButton('overview', 'Overview', '▯')}
          ${navButton('organizations', 'Organizations', '▦')}
          ${navButton('projects', 'Projects', '▤')}
          ${navButton('tasks', 'Tasks', '✓')}
          ${navButton('approvals', 'Approvals', '◷')}

          <div class="label">SECURITY &amp; GOVERNANCE</div>
          ${disabledNav('Security incidents', '~')}
          ${disabledNav('Audit trail', '≡')}

          <div class="me">
            <div class="user">
              <span class="av">${esc(initials(session.email))}</span>
              <span><b>${esc(session.email)}</b><small>${esc(role)}</small></span>
            </div>
            <button class="nav out" data-act="logout">Sign out</button>
            <span class="ver">Kanso Workspace</span>
          </div>
        </aside>

        <main class="main" id="main"></main>
      </div>
      <dialog id="dialog"></dialog>`;
  }

  function pageHead(crumb, title, subtitle, action = '') {
    return `
      <div class="crumb">WORKSPACE / ${crumb}</div>
      <div class="head">
        <div>
          <h1>${title}</h1>
          <p class="mute">${subtitle}</p>
        </div>
        ${action}
      </div>`;
  }


  /* ========================================================================
     Views
     ======================================================================== */

  function statCard(label, value, note) {
    return `
      <div class="card stat">
        <small>${label}</small>
        <b>${String(value).padStart(2, '0')}</b>
        <span>${note}</span>
      </div>`;
  }

  function viewOverview() {
    const hour = new Date().getHours();
    let greeting = 'Good evening';
    if (hour < 12) {
      greeting = 'Good morning';
    } else if (hour < 18) {
      greeting = 'Good afternoon';
    }

    const name = session.email.split('@')[0];
    const tasks = data.tasks;
    const activeProjects = data.projects.filter(p => p.status === 'active').length;
    const inProgress = tasks.filter(t => t.status === 'in_progress').length;
    const completed = tasks.filter(t => t.status === 'done').length;

    const recentTasks = [...tasks]
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .slice(0, 5);

    const newTaskButton = can('write')
      ? '<button class="btn" data-act="new-task">+ New task</button>'
      : '';

    const progressRows = data.projects.slice(0, 5).map(project => {
      const progress = projectProgress(project.id);
      return `
        <li>
          <div class="grow">
            <div class="split">
              <span>${esc(project.name)}</span>
              <span>${progress.percent}%</span>
            </div>
            ${progressBar(progress.percent)}
          </div>
        </li>`;
    }).join('');

    const recentRows = recentTasks.map(task => {
      const pillClass = task.status === 'done' ? 'pill ok' : 'pill';
      return `
        <li>
          <span>${esc(task.title)}<small>${esc(projectName(task.project_id))}</small></span>
          <span class="${pillClass}">${STATUS_LABEL[task.status]}</span>
        </li>`;
    }).join('');

    return `
      ${pageHead(
        'OVERVIEW',
        `${greeting}, ${esc(name)}.`,
        'A clear mind creates meaningful work. Here’s your workspace today.',
        newTaskButton
      )}

      <div class="grid4">
        ${statCard('Total projects', data.projects.length, `${activeProjects} active`)}
        ${statCard('Total tasks', tasks.length, 'Across all projects')}
        ${statCard('In progress', inProgress, 'Being worked on')}
        ${statCard('Completed', completed, 'Finished tasks')}
      </div>

      <div class="grid2">
        <div class="card">
          <h2>Project progress</h2>
          <p class="mute">Your active work at a glance</p>
          ${progressRows
            ? `<ul class="rows">${progressRows}</ul>`
            : '<p class="empty">No projects yet.</p>'}
        </div>

        <div class="card">
          <h2>Recent tasks</h2>
          <p class="mute">Latest additions</p>
          ${recentRows
            ? `<ul class="rows">${recentRows}</ul>`
            : '<p class="empty">No tasks yet.</p>'}
        </div>
      </div>`;
  }

  function projectCard(project) {
    const progress = projectProgress(project.id);
    const pillClass = project.status === 'active' ? 'pill ok' : 'pill';
    const taskWord = progress.total === 1 ? 'task' : 'tasks';

    return `
      <div class="card project-card">
        <span class="${pillClass}">${esc(project.status)}</span>
        <h2>${esc(project.name)}</h2>
        <p class="mute">${esc(project.description || 'No description')}</p>
        <div class="split">
          <span>Progress</span>
          <span>${progress.percent}%</span>
        </div>
        ${progressBar(progress.percent)}
        <small class="mute">${progress.total} ${taskWord} · ${esc(project.created_by.slice(0, 8))}</small>
      </div>`;
  }

  function viewProjects() {
    const newProjectButton = can('write')
      ? '<button class="btn" data-act="new-project">+ New project</button>'
      : '';

    const cards = data.projects.map(projectCard).join('');

    return `
      ${pageHead('PROJECTS', 'Projects', 'Projects belonging to your organization.', newProjectButton)}
      ${cards
        ? `<div class="project-grid">${cards}</div>`
        : '<div class="card"><p class="empty">No projects yet.</p></div>'}`;
  }

  function statusControl(task) {
    if (!task.can_edit) {
      return `<span class="pill">${STATUS_LABEL[task.status]}</span>`;
    }

    const options = STATUSES.map(([value, label]) => {
      const selected = value === task.status ? ' selected' : '';
      return `<option value="${value}"${selected}>${label}</option>`;
    }).join('');

    return `<select data-status="${task.project_id}|${task.id}">${options}</select>`;
  }

  function approvalButtons(task) {
    return (task.allowed_actions || []).map(action => `
      <button class="btn ghost small" data-approval="${task.project_id}|${task.id}|${action}">${ACTION_LABEL[action]}</button>`
    ).join('');
  }

  function taskCard(task) {
    const state = task.approval_state || 'draft';
    const stateClass = state === 'approved' ? 'pill ok' : 'pill';
    const deleteButton = task.can_delete
      ? `<button class="btn ghost small" data-delete="${task.project_id}|${task.id}">Delete</button>`
      : '';
    const decision = task.decision_comment
      ? `<p class="mute">“${esc(task.decision_comment)}”</p>`
      : '';

    return `
      <div class="card task-card">
        <b>${esc(task.title)}</b>
        <p class="mute">${esc(task.description || '')}</p>
        ${decision}
        <div class="split">
          <span class="pill">${esc(projectName(task.project_id))}</span>
          <span class="av small" title="${esc(task.owner || '')}">${esc(initials(task.owner))}</span>
        </div>
        <div class="split">
          <span class="${stateClass}">${APPROVAL_LABEL[state]}</span>
          ${statusControl(task)}
        </div>
        <small class="mute">${esc(task.due_date || 'No due date')}</small>
        <div class="task-actions">${approvalButtons(task)}${deleteButton}</div>
      </div>`;
  }

  function viewTasks() {
    const search = filters.search.toLowerCase();

    const visible = data.tasks.filter(task => {
      const matchesProject = !filters.project || task.project_id === filters.project;
      const text = `${task.title} ${task.description || ''}`.toLowerCase();
      return matchesProject && (!search || text.includes(search));
    });

    const createButton = can('write')
      ? '<button class="btn" data-act="new-task">+ Create task</button>'
      : '';

    const projectOptions = data.projects.map(project => {
      const selected = project.id === filters.project ? ' selected' : '';
      return `<option value="${project.id}"${selected}>${esc(project.name)}</option>`;
    }).join('');

    const columns = STATUSES.map(([value, label]) => {
      const tasks = visible.filter(task => task.status === value);
      return `
        <section class="column">
          <div class="split">
            <h2>${label}</h2>
            <span class="mute">${tasks.length}</span>
          </div>
          ${tasks.map(taskCard).join('') || '<p class="empty">Nothing here</p>'}
        </section>`;
    }).join('');

    return `
      ${pageHead('TASKS', 'Tasks', 'Organize work and track progress.', createButton)}

      <div class="filters">
        <input id="search" placeholder="Search tasks..." value="${esc(filters.search)}">
        <select id="project-filter">
          <option value="">All projects</option>
          ${projectOptions}
        </select>
      </div>

      <div class="board">${columns}</div>`;
  }

  function viewApprovals() {
    const rows = data.pending.map(item => {
      const task = data.tasks.find(t => t.id === item.task_id);
      const actions = task ? approvalButtons(task) : '';
      const note = item.note ? ` · ${esc(item.note)}` : '';
      return `
        <li>
          <span>${esc(item.title)}
            <small>${esc(projectName(item.project_id))} · submitted by ${esc(item.submitted_by_email)} · ${esc(item.submitted_at.slice(0, 10))}${note}</small>
          </span>
          <span class="task-actions">${actions}</span>
        </li>`;
    }).join('');

    return `
      ${pageHead('APPROVAL WORKFLOWS', 'Approval workflows', 'Review and manage submitted work.')}
      <div class="card">
        ${rows
          ? `<ul class="rows">${rows}</ul>`
          : '<p class="empty">No pending approvals. All caught up.</p>'}
      </div>`;
  }

  function viewNoOrganization() {
    return `
      ${pageHead('WORKSPACE', 'Welcome', 'Create an organization to start adding projects and tasks.')}
      <div class="card">
        <p class="empty">You are not a member of any organization yet.</p>
        <button class="btn" data-act="new-org">+ Create organization</button>
      </div>`;
  }

  function roleSelect(attribute, value) {
    const options = ROLES.map(role => {
      const selected = role === value ? ' selected' : '';
      return `<option value="${role}"${selected}>${role}</option>`;
    }).join('');
    return `<select ${attribute}>${options}</select>`;
  }

  function memberRow(member) {
    const own = member.user_id === mySub();
    let controls = `<span class="pill">${esc(member.role)}</span>`;

    if (can('manage')) {
      controls = `
        ${roleSelect(`data-role="${member.user_id}"`, member.role)}
        <button class="btn ghost" data-remove-member="${member.user_id}">Remove</button>`;
    } else if (own) {
      controls += `<button class="btn ghost" data-remove-member="${member.user_id}">Leave</button>`;
    }

    return `
      <li>
        <span class="split">
          <span class="av">${esc(initials(member.email))}</span>
          <span>${esc(member.email)}<small>Joined ${esc(member.joined_at.slice(0, 10))}</small></span>
        </span>
        <span class="split">${controls}</span>
      </li>`;
  }

  function viewOrganizations() {
    const org = data.org;

    const addButton = can('manage')
      ? '<button class="btn" data-act="add-member">+ Add member</button>'
      : '';

    const switcher = data.orgs.length > 1
      ? `<select id="org-switch">${data.orgs.map(o => {
          const selected = o.id === org.id ? ' selected' : '';
          return `<option value="${o.id}"${selected}>${esc(o.name)}</option>`;
        }).join('')}</select>`
      : '';

    return `
      ${pageHead(
        'ORGANIZATIONS',
        'Organizations',
        'Manage your organization and its members.',
        '<button class="btn" data-act="new-org">+ New organization</button>'
      )}

      <div class="stack">
        <div class="card">
          <div class="split">
            <div>
              <small class="mute">Current organization</small>
              <h2>${esc(org.name)}</h2>
              <p class="mute">${org.member_count} members · your role: ${esc(org.my_role)}</p>
            </div>
            ${switcher}
          </div>
        </div>

        <div class="card">
          <div class="split">
            <h2>Members</h2>
            ${addButton}
          </div>
          <ul class="rows">${data.members.map(memberRow).join('')}</ul>
        </div>
      </div>`;
  }

  function render() {
    if (!document.getElementById('main')) {
      renderShell();
    }

    document.querySelectorAll('.nav[data-route]').forEach(button => {
      button.classList.toggle('on', button.dataset.route === route);
    });

    const searchHadFocus =
      document.activeElement && document.activeElement.id === 'search';

    const views = {
      overview: viewOverview,
      projects: viewProjects,
      tasks: viewTasks,
      organizations: viewOrganizations,
      approvals: viewApprovals,
    };
    const view = data.org ? views[route] : viewNoOrganization;
    document.getElementById('main').innerHTML = view();

    if (searchHadFocus) {
      const search = document.getElementById('search');
      search.focus();
      search.setSelectionRange(search.value.length, search.value.length);
    }
  }


  /* ========================================================================
     Modals
     ======================================================================== */

  function openModal(title, fields) {
    const dialog = document.getElementById('dialog');

    dialog.innerHTML = `
      <form data-form="${title}">
        <div class="split">
          <h2>${title}</h2>
          <button type="button" class="btn ghost" data-act="close">✕</button>
        </div>
        ${fields}
        <p class="err" role="alert"></p>
        <button class="btn" type="submit">${title}</button>
      </form>`;

    dialog.showModal();
  }

  function openOrgModal() {
    openModal('Create organization', `
      <label>Name
        <input name="name" maxlength="100" required>
      </label>`);
  }

  function openMemberModal() {
    openModal('Add member', `
      <label>Email (must already have an account)
        <input name="email" type="email" maxlength="320" required>
      </label>
      <label>Role
        <select name="role">
          ${ROLES.map(role => `<option value="${role}"${role === 'member' ? ' selected' : ''}>${role}</option>`).join('')}
        </select>
      </label>`);
  }

  function openProjectModal() {
    openModal('Create project', `
      <label>Name
        <input name="name" maxlength="100" required>
      </label>
      <label>Description
        <textarea name="description" maxlength="1000"></textarea>
      </label>`);
  }

  function openTaskModal() {
    const projectOptions = data.projects
      .map(p => `<option value="${p.id}">${esc(p.name)}</option>`)
      .join('');

    const statusOptions = STATUSES
      .map(([value, label]) => `<option value="${value}">${label}</option>`)
      .join('');

    openModal('Create task', `
      <label>Project
        <select name="project">${projectOptions}</select>
      </label>
      <label>Task title
        <input name="title" maxlength="200" required>
      </label>
      <label>Description
        <textarea name="description" maxlength="2000"></textarea>
      </label>
      <label>Status
        <select name="status">${statusOptions}</select>
      </label>
      <label>Due date
        <input name="due_date" type="date">
      </label>`);
  }

  function closeModal() {
    document.getElementById('dialog').close();
  }


  /* ========================================================================
     Actions
     ======================================================================== */

  async function run(action, errorElement) {
    try {
      await action();
    } catch (err) {
      if (errorElement) {
        errorElement.textContent = err.message;
      } else {
        alert(err.message);
      }
    }
  }

  async function submitProject(values) {
    const body = { name: values.name };
    if (values.description.trim()) {
      body.description = values.description;
    }
    await api('POST', `${orgPath()}/projects`, body);
  }

  async function submitTask(values) {
    const body = { title: values.title, status: values.status };
    if (values.description.trim()) {
      body.description = values.description;
    }
    if (values.due_date) {
      body.due_date = values.due_date;
    }
    await api('POST', `${orgPath()}/projects/${values.project}/tasks`, body);
  }

  async function submitOrg(values) {
    const created = await api('POST', '/orgs', { name: values.name });
    saveOrgId(created.id);
  }

  async function submitMember(values) {
    await api('POST', `${orgPath()}/members`, { email: values.email, role: values.role });
  }

  async function changeRole(userId, role) {
    try {
      await api('PATCH', `${orgPath()}/members/${userId}`, { role });
    } finally {
      await refresh();
    }
  }

  async function removeMember(userId) {
    await api('DELETE', `${orgPath()}/members/${userId}`);
    await refresh();
  }

  async function runApproval(key) {
    const [projectId, taskId, action] = key.split('|');
    let body;

    if (action === 'request_changes') {
      const comment = prompt('What should the author change?');
      if (!comment || !comment.trim()) {
        return;
      }
      body = { comment: comment.trim() };
    }

    const path = `${orgPath()}/projects/${projectId}/tasks/${taskId}/${ACTION_PATH[action]}`;
    await api('POST', path, body);
    await refresh();
  }

  async function deleteTask(key) {
    const [projectId, taskId] = key.split('|');
    await api('DELETE', `${orgPath()}/projects/${projectId}/tasks/${taskId}`);
    await refresh();
  }

  async function changeStatus(key, status) {
    const [projectId, taskId] = key.split('|');
    await api('PATCH', `${orgPath()}/projects/${projectId}/tasks/${taskId}`, { status });
    await refresh();
  }

  const ACTIONS = {
    logout,
    close: closeModal,
    'new-org': openOrgModal,
    'add-member': openMemberModal,
    'new-project': openProjectModal,
    'new-task': () => {
      if (data.projects.length) {
        openTaskModal();
      } else {
        alert('Create a project first.');
      }
    },
  };


  /* ========================================================================
     Events
     ======================================================================== */

  app.addEventListener('click', event => {
    const routeButton = event.target.closest('[data-route]');
    if (routeButton) {
      route = routeButton.dataset.route;
      location.hash = route;
      render();
      return;
    }

    const deleteButton = event.target.closest('[data-delete]');
    if (deleteButton) {
      if (confirm('Delete this task?')) {
        run(() => deleteTask(deleteButton.dataset.delete));
      }
      return;
    }

    const approvalButton = event.target.closest('[data-approval]');
    if (approvalButton) {
      run(() => runApproval(approvalButton.dataset.approval));
      return;
    }

    const removeButton = event.target.closest('[data-remove-member]');
    if (removeButton) {
      if (confirm('Remove this member?')) {
        run(() => removeMember(removeButton.dataset.removeMember));
      }
      return;
    }

    const actionButton = event.target.closest('[data-act]');
    if (actionButton) {
      ACTIONS[actionButton.dataset.act]();
    }
  });

  app.addEventListener('change', event => {
    if (event.target.id === 'project-filter') {
      filters.project = event.target.value;
      render();
      return;
    }

    if (event.target.id === 'org-switch') {
      saveOrgId(event.target.value);
      filters.project = '';
      run(refresh);
      return;
    }

    if (event.target.dataset.role) {
      run(() => changeRole(event.target.dataset.role, event.target.value));
      return;
    }

    if (event.target.dataset.status) {
      run(() => changeStatus(event.target.dataset.status, event.target.value));
    }
  });

  app.addEventListener('input', event => {
    if (event.target.id === 'search') {
      filters.search = event.target.value;
      render();
    }
  });

  app.addEventListener('submit', event => {
    event.preventDefault();

    const form = event.target;
    const kind = form.dataset.form;
    const values = Object.fromEntries(new FormData(form));
    const errorElement = form.querySelector('.err');
    const submitButton = form.querySelector('button[type=submit]');

    submitButton.disabled = true;

    run(async () => {
      if (kind === 'login') {
        await signIn(values.email.trim(), values.password);
        await start();
        return;
      }

      const submit = {
        'Create project': submitProject,
        'Create task': submitTask,
        'Create organization': submitOrg,
        'Add member': submitMember,
      }[kind];
      await submit(values);

      closeModal();
      await refresh();
    }, errorElement).finally(() => {
      submitButton.disabled = false;
    });
  });


  /* ========================================================================
     Start
     ======================================================================== */

  async function start() {
    const hash = location.hash.slice(1);
    if (['overview', 'organizations', 'approvals', 'projects', 'tasks'].includes(hash)) {
      route = hash;
    }

    app.innerHTML = '<div class="login"><p class="mute">Loading…</p></div>';

    try {
      await loadData();
    } catch (err) {
      if (session) {
        renderShell();
        document.getElementById('main').innerHTML =
          `<p class="err">${esc(err.message)}</p>`;
      }
      return;
    }

    app.innerHTML = '';
    render();
  }

  async function init() {
    try {
      const response = await fetch('config.json', { cache: 'no-store' });
      config = await response.json();
    } catch (err) {
      app.textContent = 'Missing config.json. Run scripts/deploy-frontend.sh.';
      return;
    }

    config.apiUrl = config.apiUrl || config.ApiUrl;
    session = loadSession();

    if (session) {
      start();
    } else {
      renderLogin();
    }
  }

  init();
})();