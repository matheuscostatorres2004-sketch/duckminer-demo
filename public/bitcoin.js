let btcSignature = '';
let btcHistoryOffset = 0;
let btcAdminOffset = 0;

function renderBitcoin(config, invoices) {
  const rate = config?.rate ? Number(config.rate) : 0;
  $('btcRate').textContent = rate ? '1 BTC = US$ ' + fmt(rate) + ' = ' + fmt(rate) + ' DCK' : 'Cotação BTC/USD ainda não configurada pelo administrador.';
  $('btcConversionAmount').dataset.rate = String(rate);
  previewBtcConversion();
  const enabled = config?.enabled === true;
  $('btcConversionSubmit').disabled = !enabled || invoices.some(i => i.status !== 'approved');
  $('btcConversionAvailability').textContent = enabled ? 'Cotação definida pelo administrador. O valor fica fixado ao solicitar; o crédito depende de aprovação manual.' : 'Conversão real desativada até configurar cotação e armazenamento persistente.';
  const signature = JSON.stringify([enabled,invoices]);
  if (signature === btcSignature) return;
  btcSignature = signature;
  const cards = invoices.map(invoice => {
    const card = btcInvoiceCard(invoice);
    if (invoice.status === 'payment' && enabled) {
      const form = document.createElement('form');
      const input = adminInput(form,'TXID do pagamento BTC','text');
      input.pattern = '[a-fA-F0-9]{64}';
      input.maxLength = 64;
      const submit = document.createElement('button');
      submit.textContent = 'Informar pagamento';
      form.append(submit);
      form.addEventListener('submit', event => {
        event.preventDefault();
        btcRequest({action:'report',id:invoice.id,txid:input.value.trim()});
      });
      card.append(form);
    }
    return card;
  });
  $('btcInvoices').replaceChildren(...cards);
}

function previewBtcConversion() {
  const amount = Number($('btcConversionAmount').value);
  const rate = Number($('btcConversionAmount').dataset.rate);
  $('btcConversionPreview').textContent = amount > 0 && rate > 0
    ? 'Estimativa: ' + (amount * rate).toLocaleString('pt-BR',{maximumFractionDigits:6}) + ' DCK'
    : 'Informe a quantidade de BTC para calcular os DCK.';
}

function btcInvoiceCard(invoice) {
  const card = document.createElement('div');
  card.className = 'card';
  card.style.overflowWrap = 'anywhere';
  card.append(paragraph(new Date(invoice.created*1000).toLocaleString('pt-BR') + ' · ' + depositNames[invoice.status]),
    paragraph((invoice.amount/100000000).toFixed(8) + ' BTC → ' + (invoice.credit/1000000).toLocaleString('pt-BR',{maximumFractionDigits:6}) + ' DCK'),
    paragraph('Cotação fixada: 1 BTC = US$ ' + invoice.rate));
  if (invoice.address) card.append(paragraph('Endereço Bitcoin exclusivo deste pedido: '+invoice.address));
  if (invoice.txid) card.append(paragraph('TXID: '+invoice.txid));
  if (invoice.approved_at) card.append(paragraph('Crédito aprovado em '+new Date(invoice.approved_at*1000).toLocaleString('pt-BR')));
  return card;
}

async function btcRequest(data) {
  if (requestBusy) {
    $('btcConversionMessage').textContent = 'Atualização em andamento. Aguarde e tente novamente.';
    return;
  }
  requestBusy = true;
  try {
    const result = await api('/api/btc',data);
    $('btcConversionMessage').textContent = result.message;
    receive(await api('/api/state'));
    await loadDepositHistory(0);
  } catch (error) { $('btcConversionMessage').textContent = error.message; }
  finally { requestBusy = false; }
}
$('btcConversionAmount').addEventListener('input',previewBtcConversion);
$('btcConversionForm').addEventListener('submit',event => {
  event.preventDefault();
  btcRequest({action:'create',amount:$('btcConversionAmount').value});
});

async function loadDepositHistory(offset = btcHistoryOffset) {
  try {
    const result = await api('/api/deposits/history?offset='+offset);
    btcHistoryOffset = result.offset;
    $('historyDepositCount').textContent = result.total + ' depósito(s) · Página ' + (Math.floor(offset/20)+1);
    $('historyDepositPrevious').disabled = offset === 0;
    $('historyDepositNext').disabled = offset+20 >= result.total;
    const items = result.items.map(item => {
      const card = document.createElement('div');
      card.className = 'card';
      card.style.overflowWrap = 'anywhere';
      card.append(paragraph(new Date(item.created*1000).toLocaleString('pt-BR')+' · '+item.currency+' · '+depositNames[item.status]),
        paragraph((item.amount/100000000).toFixed(8)+' '+item.currency+' · Crédito: '+(item.credit/1000000).toLocaleString('pt-BR',{maximumFractionDigits:6})+' DCK'));
      if (item.txid) card.append(paragraph('TXID: '+item.txid));
      if (item.approved_at) card.append(paragraph('Creditado em '+new Date(item.approved_at*1000).toLocaleString('pt-BR')));
      return card;
    });
    $('walletDepositHistory').replaceChildren(...items);
    $('historyDepositMessage').textContent = result.total ? '' : 'Nenhum depósito solicitado nesta conta.';
  } catch(error) { $('historyDepositMessage').textContent = error.message; }
}
$('historyDepositRefresh').addEventListener('click',()=>loadDepositHistory(0));
$('historyDepositPrevious').addEventListener('click',()=>loadDepositHistory(Math.max(0,btcHistoryOffset-20)));
$('historyDepositNext').addEventListener('click',()=>loadDepositHistory(btcHistoryOffset+20));

async function loadBtcAdmin(offset=btcAdminOffset) {
  try {
    const result=await api('/api/admin/btc?offset='+offset);
    btcAdminOffset=result.offset;
    $('btcAdminPrevious').disabled=offset===0;
    $('btcAdminNext').disabled=offset+50>=result.total;
    const rows=result.deposits.map(invoice=>{
      const card=btcInvoiceCard(invoice);
      card.append(paragraph('Jogador: @'+invoice.name));
      const form=document.createElement('form');
      let data;
      if(invoice.status==='address') {
        const address=adminInput(form,'Novo endereço exclusivo da sua carteira Bitcoin','text');
        address.maxLength=90;
        data=()=>({action:'assign',address:address.value.trim()});
      } else if(invoice.status==='review') {
        const received=adminInput(form,'BTC recebido no endereço do pedido','text');
        const vout=adminInput(form,'Índice da saída (vout)','number',0);
        const confirmations=adminInput(form,'Confirmações verificadas (mínimo 6)','number',6);
        const verified=adminInput(form,'Conferi rede Bitcoin, TXID, endereço exclusivo, saída, valor e confirmações na carteira','checkbox');
        data=()=>({action:'approve',received:received.value.trim(),vout:Number(vout.value),confirmations:Number(confirmations.value),verified:verified.checked});
      } else return card;
      const button=document.createElement('button');
      button.textContent=invoice.status==='address'?'Atribuir endereço BTC':'Aprovar DCK do depósito BTC';
      form.append(button);
      form.addEventListener('submit',async event=>{
        event.preventDefault();
        if(!confirm('Confirmar operação para @'+invoice.name+'?'))return;
        button.disabled=true;
        try {
          const result=await api('/api/admin/btc',{id:invoice.id,...data()});
          $('btcAdminMessage').textContent=result.message;
          await loadBtcAdmin();
        } catch(error){$('btcAdminMessage').textContent=error.message;}
        finally{button.disabled=false;}
      });
      card.append(form);
      return card;
    });
    $('btcAdminList').replaceChildren(...rows);
  }catch(error){$('btcAdminList').replaceChildren();$('btcAdminMessage').textContent=error.message;}
}
$('btcAdminRefresh').addEventListener('click',()=>loadBtcAdmin(0));
$('btcAdminPrevious').addEventListener('click',()=>loadBtcAdmin(Math.max(0,btcAdminOffset-50)));
$('btcAdminNext').addEventListener('click',()=>loadBtcAdmin(btcAdminOffset+50));
