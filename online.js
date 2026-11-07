let poolState = {power: 0, elapsed: 0, reward: 10};
let rankingState = [];
let authenticated = false;
let requestBusy = false;
let settingsDirty = false;

async function api(path, data, key) {
  const options = {credentials: 'same-origin', headers: {}};
  if (data !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    if (key) options.headers['Idempotency-Key'] = key;
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) showLogin();
    throw new Error(result.error || 'Não foi possível conectar.');
  }
  return result;
}

function showLogin() {
  authenticated = false;
  $('accountGate').hidden = false;
  document.querySelector('main').hidden = true;
  document.querySelector('header').hidden = true;
}

function receive(result) {
  const metrics = game.metrics || [];
  game = result.game;
  game.metrics = metrics;
  poolState = result.pool;
  rankingState = result.ranking;
  const settings = game.settings || {displayName: result.user, zoom: 75};
  document.body.style.zoom = settings.zoom / 100;
  $('accountName').textContent = settings.displayName;
  $('settingsAccount').textContent = result.user;
  if (!settingsDirty) {
    $('settingsName').value = settings.displayName;
    $('settingsZoom').value = String(settings.zoom);
  }
  $('accountGate').hidden = true;
  document.querySelector('main').hidden = false;
  document.querySelector('header').hidden = false;
  authenticated = true;
  render();
  renderExchangeHistory();
}

async function serverAction(action, fields = {}) {
  if (requestBusy || !authenticated) return;
  requestBusy = true;
  try {
    const result = await api('/api/action', {action, ...fields}, crypto.randomUUID());
    if (action === 'settings') settingsDirty = false;
    if (action === 'install' || action === 'remove') selectedSlot = selectedId = null;
    receive(result);
    $('connectionStatus').textContent = result.message || 'Dados atualizados.';
    if (result.message) {
      $('purchaseNotice').textContent = '✓ ' + result.message;
      clearTimeout(purchaseNoticeTimer);
      purchaseNoticeTimer = setTimeout(() => $('purchaseNotice').textContent = '', 4500);
    }
  } catch (error) {
    $('connectionStatus').textContent = error.message;
  } finally {
    requestBusy = false;
  }
}

// All economic mutations are commands validated by the server.
syncMining = () => {};
tick = () => {};
save = show => { if (show) $('connectionStatus').textContent = 'O servidor salva cada operação automaticamente.'; };
load = () => {};
collectOfflineMining = () => {};
buy = index => serverAction('buy', {index});
buyBattery = () => serverAction('battery');
rechargeBattery = () => serverAction('recharge');
clearBattery = () => { if (confirm('Zerar a carga atual?')) serverAction('clear_battery'); };
clearBalance = () => { if (confirm('Zerar o saldo em DCK?')) serverAction('clear_balance'); };
expandRoom = () => serverAction('expand');
buyCooling = () => serverAction('cooling');
upgrade = id => serverAction('upgrade', {id});
placeInventory = (id, r, s) => serverAction('install', {id, r, s});
removeSelectedMachine = () => { if (selectedSlot) serverAction('remove', selectedSlot); };
moveMachine = (r, s, tor, tos) => serverAction('move', {r, s, tor, tos});
claim = index => serverAction('claim', {index});
claimDaily = () => serverAction('daily');
claimTimedMission = (period, index) => serverAction('mission', {period, index});
openCrate = premium => serverAction('crate', {premium});
addMoney = () => {};
addTestXmr = () => {};
resetGame = () => {};
renderExchangeHistory = () => {};

renderPool = () => {
  const own = game.running ? power() : 0;
  $('poolNetworkPower').textContent = fmt(poolState.power) + ' TH/s';
  $('poolReward').textContent = lmt(poolState.reward);
  $('poolYourPower').textContent = fmt(own) + ' TH/s';
  $('poolRivalPower').textContent = fmt(poolState.power - own) + ' TH/s';
  $('poolShare').textContent = fmt(poolState.power ? 100 * own / poolState.power : 0) + '%';
  $('poolRound').max = 60;
  $('poolRound').value = poolState.elapsed;
  $('poolElapsed').textContent = poolState.elapsed + ' / 60 s';
  $('poolLast').textContent = lmt(game.poolLastReward);
};

renderRanking = () => {
  const entries = rankingState.map((player, index) => {
    const element = document.createElement('div');
    element.className = 'inventory-item';
    element.textContent = '#' + (index + 1) + ' ' + player.name
      + ' · ' + fmt(player.power) + ' TH/s' + (player.mine ? ' · VOCÊ' : '');
    return element;
  });
  $('rankingList').replaceChildren(...entries);
};

$('accountForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (requestBusy) return;
  requestBusy = true;
  $('authMessage').textContent = 'Conectando…';
  try {
    const register = event.submitter?.value === 'register';
    receive(await api(register ? '/api/register' : '/api/login', {
      name: $('loginName').value, password: $('loginPassword').value
    }));
    $('loginPassword').value = '';
    $('authMessage').textContent = '';
  } catch (error) {
    $('authMessage').textContent = error.message;
  } finally {
    requestBusy = false;
  }
});
$('playerSettings').addEventListener('input', () => { settingsDirty = true; });
$('playerSettings').addEventListener('submit', event => {
  event.preventDefault();
  serverAction('settings', {
    displayName: $('settingsName').value.trim(),
    zoom: Number($('settingsZoom').value)
  });
});
$('logoutButton').addEventListener('click', async () => {
  if (requestBusy) return;
  try {
    await api('/api/logout', {});
    location.reload();
  } catch (error) { $('connectionStatus').textContent = error.message; }
});
window.addEventListener('resize', renderCharts);
setInterval(async () => {
  if (!authenticated || requestBusy) return;
  requestBusy = true;
  try {
    receive(await api('/api/state'));
    $('connectionStatus').textContent = 'Conectado · pool compartilhada';
  } catch (error) { $('connectionStatus').textContent = error.message; }
  finally { requestBusy = false; }
}, 2000);
setInterval(() => { if (authenticated) { recordMetrics(); renderCharts(); } }, 15000);
showLogin();
if (location.protocol === 'file:') {
  $('authMessage').textContent = 'Para abrir esta versão, extraia a pasta inteira e execute iniciar.bat. O jogo abrirá pelo servidor no navegador.';
  $('accountForm').hidden = true;
} else {
  api('/api/state').then(receive).catch(error => {
    $('authMessage').textContent = error instanceof TypeError
      ? 'Sem conexão com o servidor. Execute iniciar.bat e mantenha a janela aberta.'
      : error.message;
  });
}
