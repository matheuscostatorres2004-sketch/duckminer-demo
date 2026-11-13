let poolState = {power: 0, elapsed: 0, duration: POOL_SECONDS, reward: POOL_REWARD};
let rankingState = [];
let authenticated = false;
let requestBusy = false;
let settingsDirty = false;
let csrfToken = '';
let authMode = 'login';

async function api(path, data, key) {
  const options = {credentials: 'same-origin', headers: {}};
  if (data !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.headers['X-DuckMiner-Request'] = '1';
    if (csrfToken) options.headers['X-CSRF-Token'] = csrfToken;
    if (key) options.headers['Idempotency-Key'] = key;
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (result.csrf) csrfToken = result.csrf;
  if (!response.ok) {
    if (response.status === 401) showLogin();
    throw new Error(result.error || 'Não foi possível conectar.');
  }
  return result;
}

function showLogin() {
  authenticated = false;
  document.body.classList.add("auth-screen");
  $('accountGate').hidden = false;
  document.querySelector('main').hidden = true;
  document.querySelector('header').hidden = true;
}

function receive(result) {
  const metrics = game.metrics || [];
  game = result.game;
  for (const machine of [...game.inventory, ...game.racks.flat().filter(Boolean)]) {
    machine.name = machine.name.replace(/^Antminer\b/, 'Duck');
    machine.rarity = 'Basic';
  }
  game.history = game.history.map(entry => entry.replace(/\bAntminer\b/g, 'Duck'));
  game.metrics = metrics;
  poolState = result.pool;
  poolSplit = game.poolSplit || {DCK:100, BTC:0, DOGE:0};
  for (const currency of ["BTC", "DOGE"]) {
    $("walletMined" + currency).textContent = $("mined" + currency).textContent = ((game.miningWallet?.[currency] || 0) / 1000000)
      .toLocaleString("pt-BR", {maximumFractionDigits:6});
  }
  rankingState = result.ranking;
  $('adminMenuButton').hidden = !result.adminEligible;
  if (!result.adminEligible) $('tab-admin').hidden = true;
  const settings = game.settings || {displayName: result.user, zoom: 75};
  document.body.style.zoom = settings.zoom / 100;
  $('accountName').textContent = settings.displayName;
  $('settingsAccount').textContent = result.user;
  $('settingsEmail').textContent = result.email || 'Ainda não cadastrado';
  $('linkEmailForm').hidden = Boolean(result.email);
  if (!settingsDirty) {
    $('settingsName').value = settings.displayName;
    $('settingsZoom').value = String(settings.zoom);
  }
  $('accountGate').hidden = true;
  document.querySelector('main').hidden = false;
  document.querySelector('header').hidden = false;
  authenticated = true;
  document.body.classList.remove("auth-screen");
  render();
  renderExchangeHistory();
  renderDeposits(result.depositConfig, result.deposits || []);
  renderBitcoin(result.btcConfig, result.btcDeposits || []);
}

async function serverAction(action, fields = {}) {
  if (requestBusy || !authenticated) return;
  requestBusy = true;
  try {
    const result = await api('/api/action', {action, ...fields}, crypto.randomUUID());
    if (action === 'settings') settingsDirty = false;
    if (action === 'install' || action === 'remove') selectedSlot = selectedId = null;
    receive(result);
    if (result.message) result.message = result.message.replace(/\bAntminer\b/g, 'Duck');
    $('connectionStatus').textContent = result.message || 'Dados atualizados.';
    if (result.message) {
      $('purchaseNotice').textContent = result.message;
      clearTimeout(purchaseNoticeTimer);
      purchaseNoticeTimer = setTimeout(() => $('purchaseNotice').textContent = '', 4500);
    }
    return true;
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
clearBalance = () => { if (confirm('Zerar o saldo em DCK?')) serverAction('clear_balance'); };
expandRoom = (size = 4) => serverAction('expand', {size});
function installRack(position) { return serverAction('install_rack', {position}); }
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
  renderPoolEstimate(own, poolState.power, poolState.reward, poolState.powers);
  renderPoolCountdown(poolState.duration ?? POOL_SECONDS, poolState.elapsed);
  $('poolYourPower').textContent = fmt(own) + ' TH/s';
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
    const register = authMode === 'register';
    receive(await api(register ? '/api/register' : '/api/login', {
      email: $('loginName').value.trim(), password: $('loginPassword').value,
      ...(register ? {name:$('registerNickname').value.trim(), invite: $('loginInvite').value} : {})
    }));
    $('loginPassword').value = '';
    $('loginInvite').value = '';
    $('authMessage').textContent = '';
  } catch (error) {
    $('authMessage').textContent = error.message;
  } finally {
    requestBusy = false;
  }
});
function setAuthMode(mode) {
  authMode = mode;
  const register = mode === 'register';
  $('registerFields').hidden = !register;
  $('registerNickname').required = register;
  $('loginInvite').required = register;
  $('loginName').type = register ? 'email' : 'text';
  $('loginPassword').autocomplete = register ? 'new-password' : 'current-password';
  $('authSubmit').textContent = register ? 'Criar minha conta' : 'Entrar';
  $('authTitle').textContent = register ? 'Criar conta' : 'Acessar conta';
  $('legacyLoginHint').hidden = register;
  $('authMessage').textContent = '';
  $('authSwitchText').textContent = register ? 'Já tem conta?' : 'Ainda não tem conta?';
  $('authSwitch').textContent = register ? 'Entrar' : 'Criar conta';
}
$('authSwitch').addEventListener('click', () => setAuthMode(authMode === 'login' ? 'register' : 'login'));

$('linkEmailForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (requestBusy) {
    $('linkEmailMessage').textContent = 'Aguarde a atualização e tente novamente.';
    return;
  }
  requestBusy = true;
  try {
    const result = await api('/api/account/email', {email:$('linkEmail').value, password:$('linkPassword').value});
    receive(result);
    $('connectionStatus').textContent = result.message;
  } catch (error) { $('linkEmailMessage').textContent = error.message; }
  finally {
    $('linkPassword').value = '';
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
let adminOffset = 0;

async function loadAdmin(offset = adminOffset) {
  try {
    const result = await api('/api/admin?offset=' + offset);
    adminOffset = result.offset;
    $('adminSummary').textContent = result.total + ' contas cadastradas · '
      + result.recent + ' com atividade no último minuto';
    $('adminPage').textContent = 'Página ' + (Math.floor(adminOffset / result.limit) + 1);
    $('adminPrevious').disabled = adminOffset === 0;
    $('adminNext').disabled = adminOffset + result.limit >= result.total;
    const rows = result.players.map(player => {
      const row = document.createElement('div');
      row.className = 'card admin-player';
      const title = document.createElement('strong');
      title.textContent = player.displayName + ' (@' + player.name + ')'
        + (player.admin ? ' · Administrador' : player.blocked ? ' · Bloqueado' : ' · Liberado');
      const info = document.createElement('p');
      info.textContent = 'Saldo: ' + lmt(player.balance) + ' · Total minerado: ' + lmt(player.totalMined)
        + ' · Máquinas instaladas: ' + player.installed + ' · No estoque: ' + player.inventory;
      const rackInfo = document.createElement('p');
      rackInfo.textContent = 'Poder: ' + fmt(player.power) + ' TH/s · Racks instalados: '
        + player.racks + ' · Racks no estoque: ' + player.rackStock;
      const activity = document.createElement('p');
      activity.textContent = 'Sessões válidas: ' + player.activeSessions
        + ' · Última atividade: ' + (player.lastSeen ? new Date(player.lastSeen * 1000).toLocaleString('pt-BR') : 'Sem registro');
      row.append(title, info, rackInfo, activity);
      const viewAccount = document.createElement('button');
      viewAccount.type = 'button';
      viewAccount.textContent = 'Ver conta';
      viewAccount.onclick = () => {
        $('adminPlayerTitle').textContent = player.displayName + ' (@' + player.name + ')';
        const balances = player.miningWallet || {};
        const formatBalance = value => (value ?? 0).toLocaleString('pt-BR', {maximumFractionDigits: 6});
        const dialog = $('adminPlayerDialog');
        const tabs = [...dialog.querySelectorAll('[data-player-view]')];
        const machineLabel = machine => machine
          ? machine.name + ' · ' + fmt(machine.power) + ' TH/s'
          : 'Espaço vazio';
        const views = {
          room: [
            ['Poder instalado', fmt(player.power) + ' TH/s'],
            ...(player.roomMachines || []).map((rack, index) => [
              'Rack ' + ((player.rackPositions?.[index] ?? index) + 1),
              rack.map((machine, slot) => (slot + 1) + '. ' + machineLabel(machine))
            ])
          ],
          wallet: [
            ['Saldo DCK', formatBalance(player.balance) + ' DCK'],
            ['Mineração simulada BTC', formatBalance(balances.BTC) + ' BTC'],
            ['Mineração simulada DOGE', formatBalance(balances.DOGE) + ' DOGE']
          ],
          stock: [
            ['Racks disponíveis', player.rackStock],
            ['Máquinas disponíveis', (player.stockMachines || []).map(machineLabel)]
          ]
        };
        function showView(view) {
          for (const tab of tabs) tab.setAttribute('aria-pressed', String(tab.dataset.playerView === view));
          $('adminPlayerDetails').replaceChildren(...views[view].flatMap(([label, value]) => {
            const term = document.createElement('dt');
            term.textContent = label;
            const values = Array.isArray(value) ? value : [value];
            return [term, ...(values.length ? values : ['Nenhum']).map(item => {
              const detail = document.createElement('dd');
              detail.textContent = item;
              return detail;
            })];
          }));
        }
        for (const tab of tabs) tab.onclick = () => showView(tab.dataset.playerView);
        showView('room');
        $('adminPlayerDialog').showModal();
      };
      row.append(viewAccount);
      if (!player.admin) {
        for (const [action, label] of [[player.blocked ? 'unblock' : 'block', player.blocked ? 'Desbloquear' : 'Bloquear'], ['revoke', 'Encerrar sessões']]) {
          const button = document.createElement('button');
          button.textContent = label;
          button.addEventListener('click', async () => {
            if (!confirm(label + ' da conta @' + player.name + '?')) return;
            button.disabled = true;
            try {
              const result = await api('/api/admin/action', {action, userId: player.id});
              $('adminMessage').textContent = result.message;
              await loadAdmin();
            } catch (error) { $('adminMessage').textContent = error.message; }
            finally { button.disabled = false; }
          });
          row.append(button);
        }
      }
      return row;
    });
    $('adminUsers').replaceChildren(...rows);
  } catch (error) {
    $('adminUsers').replaceChildren();
    $('adminSummary').textContent = '';
    $('adminMessage').textContent = error.message;
  }
}
$('adminUnlock').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const result = await api('/api/admin/unlock', {key: $('adminKey').value});
    $('adminKey').value = '';
    $('adminMessage').textContent = result.message;
    await loadAdmin(0);
  } catch (error) { $('adminMessage').textContent = error.message; }
  finally { $('adminKey').value = ''; }
});
$('adminRefresh').addEventListener('click', () => loadAdmin());
$('adminPrevious').addEventListener('click', () => loadAdmin(Math.max(0, adminOffset - 50)));
$('adminNext').addEventListener('click', () => loadAdmin(adminOffset + 50));
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
function startOnline(){
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

}
window.addEventListener('DOMContentLoaded',startOnline,{once:true});
$('passwordVisibility').addEventListener('click', () => {
  const visible = $('loginPassword').type === 'password';
  $('loginPassword').type = visible ? 'text' : 'password';
  $('passwordVisibility').setAttribute('aria-label', visible ? 'Ocultar senha' : 'Mostrar senha');
  $('passwordVisibility').setAttribute('aria-pressed', String(visible));
});

function removeRack(position) {
  if (confirm('Remover este rack? O rack e as máquinas instaladas voltarão ao estoque.')) {
    return serverAction('remove_rack', {position});
  }
}
