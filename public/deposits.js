let depositSignature = '';
let depositAdminOffset = 0;
const depositNames = {address:'Aguardando endereço', payment:'Aguardando pagamento', review:'Em conferência manual', approved:'DCK creditado'};

function paragraph(text) {
  const item = document.createElement('p');
  item.textContent = text;
  return item;
}

function depositHeader(deposit) {
  const card = document.createElement('div');
  card.className = 'card';
  card.append(paragraph('Solicitação ' + deposit.id + ' · ' + depositNames[deposit.status]),
    paragraph((deposit.amount / 100000000).toFixed(8) + ' DOGE → ' + lmt(deposit.credit / 1000000)),
    paragraph('Taxa desta solicitação: 1 DOGE = ' + deposit.rate + ' DCK'));
  if (deposit.address) card.append(paragraph('Endereço Dogecoin exclusivo: ' + deposit.address));
  if (deposit.txid) card.append(paragraph('TXID: ' + deposit.txid));
  card.style.overflowWrap = 'anywhere';
  return card;
}

async function submitDeposit(data) {
  if (requestBusy) {
    $('depositMessage').textContent = 'Atualização em andamento. Aguarde e tente novamente.';
    return;
  }
  requestBusy = true;
  try {
    const result = await api('/api/deposit', data);
    $('depositMessage').textContent = result.message;
    receive(await api('/api/state'));
  } catch (error) { $('depositMessage').textContent = error.message; }
  finally { requestBusy = false; }
}

function renderDeposits(config, deposits) {
  if (!$("depositForm")) return;
  const signature = JSON.stringify([config, deposits]);
  if (signature === depositSignature) return;
  depositSignature = signature;
  const enabled = config?.enabled === true;
  $('depositAvailability').textContent = enabled
    ? 'Rede Dogecoin · 1 DOGE = ' + config.rate + ' DCK · aprovação manual'
    : 'Recebimento de DOGE desativado. Aguarde a configuração do administrador.';
  $('depositCreate').disabled = !enabled || deposits.some(d => d.status !== 'approved');
  const rows = deposits.map(deposit => {
    const card = depositHeader(deposit);
    if (deposit.status === 'payment' && enabled) {
      card.append(paragraph('Envie exatamente o valor indicado, pela rede Dogecoin. As taxas de envio são pagas à parte.'));
      const form = document.createElement('form');
      const label = document.createElement('label');
      label.textContent = 'Identificador da transação (TXID)';
      const input = document.createElement('input');
      input.required = true;
      input.pattern = '[a-fA-F0-9]{64}';
      input.maxLength = 64;
      const button = document.createElement('button');
      button.textContent = 'Informar transação enviada';
      label.append(input);
      form.append(label, button);
      form.addEventListener('submit', event => {
        event.preventDefault();
        submitDeposit({action:'report', id:deposit.id, txid:input.value.trim()});
      });
      card.append(form);
    }
    return card;
  });
  $('depositList').replaceChildren(...rows);
}
$('depositForm')?.addEventListener('submit', event => {
  event.preventDefault();
  submitDeposit({action:'create', amount:$('depositAmount').value});
});

function adminInput(form, title, type, minimum) {
  const label = document.createElement('label');
  label.textContent = title + ' ';
  const input = document.createElement('input');
  input.type = type;
  input.required = true;
  if (minimum !== undefined) input.min = minimum;
  if (type === 'number') input.step = '1';
  label.append(input);
  form.append(label, document.createElement('br'));
  return input;
}

async function loadAdminDeposits(offset = depositAdminOffset) {
  try {
    const result = await api('/api/admin/deposits?offset=' + offset);
    depositAdminOffset = result.offset;
    $('depositPrevious').disabled = result.offset === 0;
    $('depositNext').disabled = result.offset + 50 >= result.total;
    const rows = result.deposits.map(deposit => {
      const card = depositHeader(deposit);
      card.append(paragraph('Jogador: @' + deposit.name));
      const form = document.createElement('form');
      let fields;
      if (deposit.status === 'address') {
        const address = adminInput(form, 'Novo endereço público exclusivo da sua carteira DOGE', 'text');
        address.maxLength = 35;
        fields = () => ({action:'assign', address:address.value.trim()});
      } else if (deposit.status === 'review') {
        const received = adminInput(form, 'DOGE recebido no endereço exclusivo', 'text');
        const vout = adminInput(form, 'Índice da saída (vout)', 'number', '0');
        const confirmations = adminInput(form, 'Confirmações conferidas (mínimo 6)', 'number', '6');
        const verified = adminInput(form, 'Conferi na carteira a rede Dogecoin, o TXID, o endereço, a saída, o valor e as confirmações', 'checkbox');
        fields = () => ({action:'approve', received:received.value.trim(), vout:Number(vout.value), confirmations:Number(confirmations.value), verified:verified.checked});
      } else return card;
      const button = document.createElement('button');
      button.textContent = deposit.status === 'address' ? 'Atribuir endereço' : 'Aprovar crédito DCK';
      form.append(button);
      form.addEventListener('submit', async event => {
        event.preventDefault();
        if (!confirm('Confirmar esta operação para @' + deposit.name + '?')) return;
        button.disabled = true;
        try {
          const result = await api('/api/admin/deposit', {id:deposit.id, ...fields()});
          $('adminDepositMessage').textContent = result.message;
          await loadAdminDeposits();
        } catch (error) { $('adminDepositMessage').textContent = error.message; }
        finally { button.disabled = false; }
      });
      card.append(form);
      return card;
    });
    $('adminDeposits').replaceChildren(...rows);
  } catch (error) {
    $('adminDeposits').replaceChildren();
    $('adminDepositMessage').textContent = error.message;
  }
}
$('adminDepositsRefresh').addEventListener('click', () => loadAdminDeposits(0));
$('depositPrevious').addEventListener('click', () => loadAdminDeposits(Math.max(0,depositAdminOffset-50)));
$('depositNext').addEventListener('click', () => loadAdminDeposits(depositAdminOffset+50));
