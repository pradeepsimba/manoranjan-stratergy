'use strict';

var _authMode = 'login';   // 'login' | 'register'

function setAuthTab(mode) {
  _authMode = mode;
  document.getElementById('tab-login').classList.toggle('active', mode === 'login');
  document.getElementById('tab-register').classList.toggle('active', mode === 'register');
  document.getElementById('auth-submit').textContent = mode === 'login' ? 'Log in' : 'Create account';
  document.getElementById('auth-hint').style.display = mode === 'login' ? 'block' : 'none';
  document.getElementById('auth-error').textContent = '';
}

async function submitAuth(evt) {
  evt.preventDefault();
  var errEl = document.getElementById('auth-error');
  var btn = document.getElementById('auth-submit');
  errEl.textContent = '';

  var username = document.getElementById('auth-username').value.trim();
  var password = document.getElementById('auth-password').value;

  btn.disabled = true;
  try {
    var endpoint = _authMode === 'login' ? '/api/auth/login' : '/api/auth/register';
    await apiPost(endpoint, { username: username, password: password });
    location.href = '/';
  } catch (e) {
    errEl.textContent = e.message || 'Something went wrong';
  } finally {
    btn.disabled = false;
  }
  return false;
}

// Already logged in? Skip the form.
apiGet('/api/auth/me').then(function () { location.href = '/'; }).catch(function () {});
