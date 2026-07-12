let products = {};
    let runtimeConfig = {};
    let devicePolicy = {};
    let policyHistory = [];
    let latestEvents = [];
    let autoRefresh = true;
    let latestRecords = [];
    const statusNames = {
      accepted: '已识别',
      low_confidence: '低置信度',
      needs_confirm: '待确认',
      manually_confirmed: '已确认',
      unknown: '未知',
      invalid_record: '记录异常'
    };

    function money(value) {
      const number = Number(value || 0);
      return number.toFixed(2).replace(/\\.00$/, '').replace(/0$/, '');
    }

    function mediaUrl(path) {
      return path ? `/media?path=${encodeURIComponent(path)}` : '';
    }

    function statusClass(status) {
      return String(status || 'unknown').replace(/[^a-z_]/g, '');
    }

    function statusText(status) {
      return statusNames[status] || status || 'unknown';
    }

    function policyActionText(action) {
      return ({
        keep: '保持原结果',
        accept: '直接通过',
        needs_confirm: '进入待确认',
        reject: '暂停结算'
      })[action] || action || '无';
    }

    function pricingModeText(mode) {
      return ({
        standard: '按原单价计价',
        discount_10_over_1000g: '大重量九折优惠'
      })[mode] || mode || '无';
    }

    function voiceCommandText(command) {
      return ({
        status: '播报状态',
        weight: '播报重量',
        latest: '最近交易',
        price: '播报价格',
        pending: '是否待确认',
        help: '帮助'
      })[command] || command || '未知命令';
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[char]));
    }

    function shortId(value, head = 8, tail = 4) {
      const text = String(value || '');
      if (text.length <= head + tail + 3) return text;
      return `${text.slice(0, head)}...${text.slice(-tail)}`;
    }

    function formatTime(value) {
      return String(value || '').replace('T', ' ');
    }

    function compactEventMessage(event) {
      let message = String(event.message || '');
      const transactionId = String(event.transaction_id || '');
      if (transactionId) {
        message = message.replaceAll(transactionId, shortId(transactionId));
      }
      return message;
    }

    async function getJson(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(await response.text());
      return await response.json();
    }

    function getFilters() {
      return {
        start: document.getElementById('filterStart').value,
        end: document.getElementById('filterEnd').value,
        product: document.getElementById('filterProduct').value,
        status: document.getElementById('filterStatus').value,
        limit: document.getElementById('filterLimit').value || '100'
      };
    }

    function buildQuery(includeLimit = true) {
      const filters = getFilters();
      const params = new URLSearchParams();
      for (const [key, value] of Object.entries(filters)) {
        if (!includeLimit && key === 'limit') continue;
        if (value) params.set(key, value);
      }
      return params.toString();
    }

    function syncTransactionPanelHeight() {
      const panel = document.querySelector('.transaction-panel');
      const side = document.querySelector('#dashboardPage .side-body');
      if (!panel || !side || !document.getElementById('dashboardPage')?.classList.contains('active')) return;
      panel.style.removeProperty('--transaction-panel-height');
      requestAnimationFrame(() => {
        const sideHeight = Math.ceil(side.getBoundingClientRect().height);
        if (sideHeight > 0) {
          panel.style.setProperty('--transaction-panel-height', `${sideHeight}px`);
        }
      });
    }

    function renderSummary(summary) {
      document.getElementById('mCount').textContent = summary.total_transactions;
      document.getElementById('mSales').textContent = money(summary.total_sales);
      document.getElementById('mWeight').textContent = `${Math.round(summary.total_weight_g || 0)}g`;
      const pending = (summary.status_counts.needs_confirm || 0) + (summary.status_counts.low_confidence || 0) + (summary.status_counts.unknown || 0);
      document.getElementById('mPending').textContent = pending;
    }

    function renderAnalytics(analytics) {
      const trend = analytics.last_7_days || [];
      const maxSales = Math.max(1, ...trend.map(item => Number(item.sales || 0)));
      document.getElementById('salesTrend').innerHTML = trend.map(item => {
        const height = Math.max(4, Math.round(Number(item.sales || 0) / maxSales * 100));
        return `
          <div class="trend-item">
            <span class="trend-value">${money(item.sales)}</span>
            <div class="trend-bar" style="height:${height}px" title="${money(item.sales)}元"></div>
            <span>${String(item.date || '').slice(5)}</span>
          </div>
        `;
      }).join('');

      const ranking = (analytics.product_ranking || []).slice(0, 5);
      const maxProductSales = Math.max(1, ...ranking.map(item => Number(item.sales || 0)));
      document.getElementById('productRanking').innerHTML = ranking.length ? ranking.map(item => {
        const width = Math.round(Number(item.sales || 0) / maxProductSales * 100);
        return `
          <div class="rank-row">
            <strong>${escapeHtml(item.product)}</strong>
            <div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div>
            <span>${money(item.sales)}</span>
          </div>
        `;
      }).join('') : '<span class="muted">暂无数据</span>';

      const quality = analytics.recognition_quality || {};
      document.getElementById('recognitionQuality').innerHTML = `
        <div class="quality-box"><label>识别准确率</label><strong>${Number(quality.recognized_rate || 0).toFixed(1)}%</strong></div>
        <div class="quality-box"><label>低置信度比例</label><strong>${Number(quality.low_quality_rate || 0).toFixed(1)}%</strong></div>
        <div class="quality-box"><label>已识别数</label><strong>${Number(quality.recognized_count || 0)}</strong></div>
        <div class="quality-box"><label>异常/待确认</label><strong>${Number(quality.low_quality_count || 0)}</strong></div>
      `;
    }

    function renderRecords(records) {
      const tbody = document.getElementById('recordRows');
      tbody.innerHTML = '';
      if (!Array.isArray(records) || !records.length) {
        tbody.innerHTML = '<tr><td class="empty-row" colspan="8">暂无交易记录</td></tr>';
        return;
      }
      for (const record of records) {
        const tr = document.createElement('tr');
        const transactionId = record.transaction_id || '';
        tr.innerHTML = `
          <td>${escapeHtml(record.timestamp || '')}</td>
          <td><span class="status ${statusClass(record.status)}">${statusText(record.status)}</span></td>
          <td>${escapeHtml(record.product_name || record.product_id || '')}</td>
          <td>${Number(record.confidence || 0).toFixed(3)}</td>
          <td>${Math.round(record.weight_g || 0)}g</td>
          <td>${money(record.total_price)}</td>
          <td>${escapeHtml(record.voice_text || '')}</td>
          <td><button class="link-btn" data-id="${escapeHtml(transactionId)}">详情</button></td>
        `;
        tbody.appendChild(tr);
      }
      for (const button of tbody.querySelectorAll('button[data-id]')) {
        button.addEventListener('click', () => openDetail(button.dataset.id));
      }
    }

    function renderSyncStatus(status) {
      const box = document.getElementById('syncStatus');
      const dotClass = status.connected ? 'online' : (status.enabled ? 'offline' : '');
      const state = !status.enabled ? 'MQTT同步未启用' : (status.connected ? 'MQTT已连接' : 'MQTT未连接');
      const broker = status.enabled ? `${status.host}:${status.port}` : '';
      const last = status.last_message_at ? `最近同步 ${status.last_message_at}` : '暂无同步消息';
      const error = status.last_error ? `错误：${status.last_error}` : '';
      box.innerHTML = `
        <span class="dot ${dotClass}"></span>
        <span>${state}</span>
        ${broker ? `<span>${broker}</span>` : ''}
        ${status.topic ? `<span>${status.topic}</span>` : ''}
        <span>${last}</span>
        ${error ? `<span>${error}</span>` : ''}
      `;
    }

    function renderDeviceStatus(status) {
      const online = document.getElementById('deviceOnline');
      const dotClass = status.online ? 'online' : 'offline';
      online.innerHTML = `<span class="dot ${dotClass}"></span><span>${status.online ? '在线' : '离线'}</span>`;
      const latestStatus = statusText(status.latest_status || 'unknown');
      const source = status.recent_heartbeat ? '设备心跳' : (status.recent_report ? '最近交易记录' : (status.mqtt_connected ? '仅后台MQTT连接' : '暂无活跃数据'));
      document.getElementById('deviceStatus').innerHTML = `
        <div><span>服务状态</span><strong>${escapeHtml(status.service_state || '暂无')}</strong></div>
        <div><span>当前重量</span><strong>${status.current_weight_g == null ? '暂无' : Number(status.current_weight_g).toFixed(1) + 'g'}</strong></div>
        <div><span>设备ID</span><strong>${escapeHtml(status.device_id || '未知')}</strong></div>
        <div><span>最近一次上报</span><strong>${escapeHtml(status.last_report_at || '暂无')}</strong></div>
        <div><span>MQTT连接</span><strong>${status.mqtt_connected ? '已连接' : '未连接'}</strong></div>
        <div><span>状态来源</span><strong>${escapeHtml(source)}</strong></div>
        <div><span>本地记录数</span><strong>${Number(status.record_count || 0)}</strong></div>
        <div><span>今日交易数</span><strong>${Number(status.today_transactions || 0)}</strong></div>
        <div><span>今日销售额</span><strong>${money(status.today_sales)} 元</strong></div>
        <div><span>最近识别状态</span><strong>${escapeHtml(latestStatus)}</strong></div>
        <div><span>最近商品</span><strong>${escapeHtml(status.latest_product || '暂无')}</strong></div>
        <div><span>最近心跳时间</span><strong>${escapeHtml(status.last_heartbeat_at || '暂无')}</strong></div>
      `;
    }

    function getSection(name) {
      runtimeConfig[name] = runtimeConfig[name] || {};
      return runtimeConfig[name];
    }

    function runtimeField(id) {
      return document.querySelector(`#runtimePage #${id}`);
    }

    function renderRuntimeConfig(config) {
      const payload = config?.config ? config : {config};
      runtimeConfig = payload.config || {};
      const recognition = getSection('recognition');
      const camera = getSection('camera');
      const tts = getSection('tts');
      const mqtt = getSection('mqtt');
      runtimeField('cfgDeviceId').value = runtimeConfig.device_id || '';
      runtimeField('cfgAcceptConfidence').value = Number(recognition.accept_confidence ?? 0.75);
      runtimeField('cfgConfirmGap').value = Number(recognition.confirm_gap ?? 0.15);
      runtimeField('cfgTopk').value = Number(recognition.topk ?? 3);
      runtimeField('cfgCameraWidth').value = Number(camera.width ?? 1920);
      runtimeField('cfgCameraHeight').value = Number(camera.height ?? 1080);
      runtimeField('cfgTtsBackend').value = tts.backend || 'syn6288';
      runtimeField('cfgTtsPort').value = tts.port || '/dev/ttyS10';
      runtimeField('cfgTtsVolume').value = Number(tts.volume ?? 3);
      runtimeField('cfgTtsSpeed').value = Number(tts.speed ?? 5);
      runtimeField('cfgMqttEnabled').checked = mqtt.enabled !== false;
      runtimeField('cfgMqttOptional').checked = mqtt.optional !== false;
      renderRuntimeStatus(payload.status || {}, payload.cache || {});
    }

    function renderRuntimeStatus(status, cache) {
      const box = document.getElementById('runtimeStatus');
      if (!box) return;
      const sourceNames = {
        published: '本次后台刚下发',
        mqtt_retained: 'MQTT retained远程配置',
        '': '默认参数'
      };
      box.innerHTML = `
        <div>配置来源<strong>${escapeHtml(sourceNames[cache.source || ''] || cache.source || '默认参数')}</strong></div>
        <div>下发Topic<strong>${escapeHtml(status.topic || '暂无')}</strong></div>
        <div>最近下发<strong>${escapeHtml(status.last_published_at || cache.updated_at || '暂无')}</strong></div>
        <div>下发状态<strong>${status.last_error ? `失败：${escapeHtml(status.last_error)}` : (status.topic ? '成功/可读取' : '等待首次下发')}</strong></div>
        <div>当前音量<strong>${Number(runtimeConfig.tts?.volume ?? 3)}</strong></div>
        <div>当前置信度阈值<strong>${Number(runtimeConfig.recognition?.accept_confidence ?? 0.75)}</strong></div>
      `;
    }

    function collectRuntimeConfig() {
      const previous = runtimeConfig || {};
      const cleaned = {...previous};
      delete cleaned.web_upload;
      return {
        ...cleaned,
        device_id: runtimeField('cfgDeviceId').value.trim() || 'lubancat3_demo_001',
        recognition: {
          ...(previous.recognition || {}),
          backend: 'rknn-det',
          accept_confidence: Number(runtimeField('cfgAcceptConfidence').value || 0.75),
          confirm_gap: Number(runtimeField('cfgConfirmGap').value || 0.15),
          topk: Number(runtimeField('cfgTopk').value || 3)
        },
        camera: {
          ...(previous.camera || {}),
          width: Number(runtimeField('cfgCameraWidth').value || 1920),
          height: Number(runtimeField('cfgCameraHeight').value || 1080)
        },
        weight: {
          ...(previous.weight || {})
        },
        tts: {
          ...(previous.tts || {}),
          backend: runtimeField('cfgTtsBackend').value,
          port: runtimeField('cfgTtsPort').value.trim() || '/dev/ttyS10',
          volume: Number(runtimeField('cfgTtsVolume').value || 3),
          speed: Number(runtimeField('cfgTtsSpeed').value || 5)
        },
        mqtt: {
          ...(previous.mqtt || {}),
          enabled: runtimeField('cfgMqttEnabled').checked,
          optional: runtimeField('cfgMqttOptional').checked
        }
      };
    }

    function policyField(id) {
      return document.querySelector(`#policyPage #${id}`);
    }

    function renderDevicePolicy(payload) {
      const policy = payload?.policy || payload || {};
      devicePolicy = policy;
      policyField('policyVersion').value = policy.policy_version || 'policy-v1.0.0';
      policyField('policyEnabled').value = policy.enabled === false ? 'false' : 'true';
      policyField('policyDescription').value = policy.description || '';
      policyField('lowConfidenceAction').value = policy.low_confidence_action || 'needs_confirm';
      policyField('unknownProductAction').value = policy.unknown_product_action || 'needs_confirm';
      policyField('pricingMode').value = policy.pricing_mode || 'standard';
      policyField('voiceTemplate').value = policy.voice_template || '';
      policyField('confirmVoiceTemplate').value = policy.confirm_voice_template || '';
      policyField('rejectVoiceTemplate').value = policy.reject_voice_template || '';
      renderPolicyStatus(payload.status || {}, payload.cache || {}, payload.latest_event || {});
    }

    function renderPolicyHistory(items) {
      policyHistory = Array.isArray(items) ? items : [];
      const tbody = document.getElementById('policyHistoryRows');
      if (!tbody) return;
      tbody.innerHTML = policyHistory.length ? policyHistory.map((item) => `
        <tr>
          <td class="mono-cell event-full">${escapeHtml(item.policy_version || '')}</td>
          <td class="mono-cell event-full">${escapeHtml(formatTime(item.modified_at || item.archived_at || item.updated_at || ''))}</td>
          <td>${escapeHtml(policyActionText(item.low_confidence_action))}</td>
          <td>${escapeHtml(pricingModeText(item.pricing_mode))}</td>
          <td title="${escapeHtml(item.voice_template || '')}"><div class="template-cell">${escapeHtml(item.voice_template || '')}</div></td>
          <td><button class="link-btn" data-policy-history-index="${Number(item.history_index)}">回滚</button></td>
        </tr>
      `).join('') : '<tr><td class="empty-row" colspan="6">暂无策略变更记录</td></tr>';

      for (const button of tbody.querySelectorAll('button[data-policy-history-index]')) {
        button.addEventListener('click', () => rollbackDevicePolicy(Number(button.dataset.policyHistoryIndex)));
      }
    }

    function renderPolicyStatus(status, cache, latestEvent) {
      const box = document.getElementById('policyStatus');
      if (!box) return;
      const sourceNames = {
        published: '本次后台刚下发',
        mqtt_retained: 'MQTT retained远程策略',
        '': '默认策略'
      };
      box.innerHTML = `
        <div>策略来源<strong>${escapeHtml(sourceNames[cache.source || ''] || cache.source || '默认策略')}</strong></div>
        <div>下发Topic<strong>${escapeHtml(status.topic || '暂无')}</strong></div>
        <div>最近下发<strong>${escapeHtml(status.last_published_at || cache.updated_at || '暂无')}</strong></div>
        <div>下发状态<strong>${status.last_error ? `失败：${escapeHtml(status.last_error)}` : (status.topic ? '成功/可读取' : '等待首次下发')}</strong></div>
        <div>当前版本<strong>${escapeHtml(devicePolicy.policy_version || 'policy-v1.0.0')}</strong></div>
        <div>可回滚<strong>${status.rollback_available ? '是' : '否'}</strong></div>
        <div>最近应用版本<strong>${escapeHtml(latestEvent.policy_version || '暂无')}</strong></div>
        <div>最近应用时间<strong>${escapeHtml(latestEvent.timestamp || '暂无')}</strong></div>
        <div>应用状态<strong>${escapeHtml(latestEvent.status || '暂无')}</strong></div>
      `;
    }

    function renderEventSummary(summary) {
      const latestPolicy = summary.latest_policy_applied || {};
      const latest = summary.latest || {};
      document.getElementById('eCount').textContent = Number(summary.total_events || 0);
      document.getElementById('ePolicyCount').textContent = Number(summary.policy_apply_count || 0);
      document.getElementById('eLatestPolicy').textContent = latestPolicy.policy_version || '暂无';
      document.getElementById('eLatestAt').textContent = latest.timestamp || '暂无';

      const ranking = Object.entries(summary.policy_version_counts || {})
        .sort((left, right) => Number(right[1]) - Number(left[1]))
        .slice(0, 8);
      const maxCount = Math.max(1, ...ranking.map(([, count]) => Number(count || 0)));
      document.getElementById('policyVersionRanking').innerHTML = ranking.length ? ranking.map(([version, count]) => {
        const width = Math.round(Number(count || 0) / maxCount * 100);
        return `
          <div class="rank-row">
            <strong>${escapeHtml(version)}</strong>
            <div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div>
            <span>${Number(count || 0)}</span>
          </div>
        `;
      }).join('') : '<span class="muted">暂无策略应用事件</span>';
    }

    function renderDeviceEvents(events) {
      latestEvents = events || [];
      const tbody = document.getElementById('eventRows');
      if (!tbody) return;
      tbody.innerHTML = latestEvents.length ? latestEvents.map(event => {
        const transactionId = event.transaction_id || '';
        const message = compactEventMessage(event);
        return `
        <tr>
          <td class="mono-cell event-full" title="${escapeHtml(event.timestamp || '')}">${escapeHtml(formatTime(event.timestamp))}</td>
          <td class="mono-cell event-full" title="${escapeHtml(event.event_type || '')}">${escapeHtml(event.event_type || '')}</td>
          <td class="mono-cell event-full" title="${escapeHtml(event.device_id || '')}">${escapeHtml(event.device_id || '')}</td>
          <td class="mono-cell event-full" title="${escapeHtml(event.policy_version || '')}">${escapeHtml(event.policy_version || '')}</td>
          <td class="mono-cell" title="${escapeHtml(transactionId)}">${escapeHtml(shortId(transactionId))}</td>
          <td>${escapeHtml(statusText(event.record_status || event.status || ''))}</td>
          <td class="mono-cell" title="${escapeHtml(event.pricing_mode || '')}">${escapeHtml(event.pricing_mode || '')}</td>
          <td title="${escapeHtml(event.message || '')}"><div class="event-message">${escapeHtml(message)}</div></td>
        </tr>
      `;
      }).join('') : '<tr><td colspan="8">暂无设备事件</td></tr>';
    }

    function collectDevicePolicy() {
      return {
        ...(devicePolicy || {}),
        policy_version: policyField('policyVersion').value.trim() || 'policy-v1.0.0',
        enabled: policyField('policyEnabled').value === 'true',
        description: policyField('policyDescription').value.trim(),
        low_confidence_action: policyField('lowConfidenceAction').value,
        unknown_product_action: policyField('unknownProductAction').value,
        pricing_mode: policyField('pricingMode').value,
        voice_template: policyField('voiceTemplate').value.trim(),
        confirm_voice_template: policyField('confirmVoiceTemplate').value.trim(),
        reject_voice_template: policyField('rejectVoiceTemplate').value.trim()
      };
    }

    function renderProducts() {
      const list = document.getElementById('productList');
      list.innerHTML = '';
      const selectedProduct = document.getElementById('filterProduct').value;
      const productFilter = document.getElementById('filterProduct');
      productFilter.innerHTML = '<option value="">全部商品</option>';
      const header = document.createElement('div');
      header.className = 'product-row header';
      header.innerHTML = '<div>商品ID</div><div>商品名称</div><div>单价</div><div>单位</div><div>启用</div><div>备注</div><div>操作</div>';
      list.appendChild(header);
      for (const [id, item] of Object.entries(products)) {
        const row = document.createElement('div');
        row.className = 'product-row';
        row.dataset.originalId = id;
        const history = Array.isArray(item.price_history) ? item.price_history : [];
        const latestHistory = history.length ? history[history.length - 1] : null;
        row.innerHTML = `
          <label class="product-field">
            <span>商品ID</span>
            <input value="${escapeHtml(id)}" data-field="id">
          </label>
          <label class="product-field">
            <span>商品名称</span>
            <input value="${escapeHtml(item.name || id)}" data-field="name">
          </label>
          <label class="product-field">
            <span>单价</span>
            <input type="number" step="0.1" min="0" value="${Number(item.unit_price || 0)}" data-field="unit_price">
          </label>
          <label class="product-field">
            <span>单位</span>
            <select data-field="unit">
              ${['斤', 'kg'].map(unit => `<option value="${unit}" ${unit === (item.unit || '斤') ? 'selected' : ''}>${unit}</option>`).join('')}
            </select>
          </label>
          <div class="product-actions-row">
            <label class="product-enable">
              <input type="checkbox" data-field="enabled" ${item.enabled === false ? '' : 'checked'}>
              <span>启用</span>
            </label>
            <button class="danger" type="button" data-action="delete-product">删除</button>
          </div>
          <label class="product-field product-field-full">
            <span>备注</span>
            <input value="${escapeHtml(item.remark || '')}" data-field="remark" placeholder="${item.price_history?.length ? `调价${item.price_history.length}次` : '备注'}">
          </label>
          <div class="product-history">${latestHistory ? `最近调价：${escapeHtml(latestHistory.timestamp || '')}，${money(latestHistory.old_price)} -> ${money(latestHistory.new_price)}` : '暂无调价记录'}</div>
        `;
        list.appendChild(row);
        row.querySelector('[data-action="delete-product"]').addEventListener('click', () => {
          row.remove();
        });
        if (item.enabled !== false) {
          const option = document.createElement('option');
          option.value = id;
          option.textContent = `${item.name || id} (${id})`;
          productFilter.appendChild(option);
        }
      }
      productFilter.value = selectedProduct;
    }

    function renderDetail(record) {
      const predictions = Array.isArray(record.top_predictions) ? record.top_predictions : [];
      const detections = Array.isArray(record.detections) ? record.detections : [];
      const policy = record.policy || {};
      const original = policy.original || {};
      const pricing = record.pricing || {};
      const hasPolicy = Object.keys(policy).length > 0;
      const hasPricing = Object.keys(pricing).length > 0;
      const sourceImage = record.source_image || '';
      const previewImage = record.detection_preview_image || '';
      const productOptions = Object.entries(products).filter(([, item]) => item.enabled !== false).map(([id, item]) => {
        const selected = id === record.product_id ? 'selected' : '';
        return `<option value="${escapeHtml(id)}" data-price="${Number(item.unit_price || 0)}" ${selected}>${escapeHtml(item.name || id)} (${escapeHtml(id)})</option>`;
      }).join('');
      const correctionCount = Array.isArray(record.corrections) ? record.corrections.length : 0;
      const latestCorrection = correctionCount ? record.corrections[correctionCount - 1] : null;
      return `
        <div class="detail-grid">
          <div><span>交易ID</span><strong>${escapeHtml(record.transaction_id || '')}</strong></div>
          <div><span>时间</span><strong>${escapeHtml(record.timestamp || '')}</strong></div>
          <div><span>设备</span><strong>${escapeHtml(record.device_id || '')}</strong></div>
          <div><span>状态</span><strong>${statusText(record.status)}</strong></div>
          <div><span>商品</span><strong>${escapeHtml(record.product_name || record.product_id || '')}</strong></div>
          <div><span>置信度</span><strong>${Number(record.confidence || 0).toFixed(3)}</strong></div>
          <div><span>置信度差值</span><strong>${record.confidence_gap === undefined || record.confidence_gap === null ? '无' : Number(record.confidence_gap || 0).toFixed(3)}</strong></div>
          <div><span>重量</span><strong>${Math.round(record.weight_g || 0)}g</strong></div>
          <div><span>总价</span><strong>${record.total_price === undefined || record.total_price === null ? '待确认' : `${money(record.total_price)} 元`}</strong></div>
          <div><span>单价</span><strong>${money(record.unit_price)} 元/${escapeHtml(record.unit || '')}</strong></div>
          <div><span>修正次数</span><strong>${correctionCount}</strong></div>
        </div>
        ${sourceImage || previewImage ? `
          <div class="detail-media-grid">
            ${previewImage ? `
              <div class="detail-media">
                <span>检测图</span>
                <img src="${mediaUrl(previewImage)}" alt="检测图">
              </div>
            ` : ''}
            ${sourceImage ? `
              <div class="detail-media">
                <span>原图</span>
                <img src="${mediaUrl(sourceImage)}" alt="原图">
              </div>
            ` : ''}
          </div>
        ` : ''}
        <div class="detail-section">
          <h4>策略影响</h4>
          ${hasPolicy ? `
            <div class="detail-grid">
              <div><span>策略版本</span><strong>${escapeHtml(policy.policy_version || '')}</strong></div>
              <div><span>策略动作</span><strong>${escapeHtml(policyActionText(policy.selected_action))}</strong></div>
              <div><span>计价模式</span><strong>${escapeHtml(pricingModeText(policy.pricing_mode || pricing.mode))}</strong></div>
              <div><span>应用时间</span><strong>${escapeHtml(policy.applied_at || '')}</strong></div>
              <div><span>原始状态</span><strong>${escapeHtml(statusText(original.status || ''))}</strong></div>
              <div><span>策略后状态</span><strong>${escapeHtml(statusText(record.status || ''))}</strong></div>
              <div><span>原始总价</span><strong>${original.total_price === undefined || original.total_price === null ? '无' : `${money(original.total_price)} 元`}</strong></div>
              <div><span>策略后总价</span><strong>${record.total_price === undefined || record.total_price === null ? '无' : `${money(record.total_price)} 元`}</strong></div>
            </div>
            <div>
              <strong>原始语音文本</strong>
              <pre>${escapeHtml(original.voice_text || '无')}</pre>
            </div>
          ` : '<span class="muted">暂无策略记录，可能是策略功能接入前生成的交易。</span>'}
        </div>
        <div class="detail-section">
          <h4>计价明细</h4>
          ${hasPricing ? `
            <div class="detail-grid">
              <div><span>计价模式</span><strong>${escapeHtml(pricingModeText(pricing.mode))}</strong></div>
              <div><span>原价</span><strong>${pricing.base_price === undefined || pricing.base_price === null ? '无' : `${money(pricing.base_price)} 元`}</strong></div>
              <div><span>优惠</span><strong>${money(pricing.discount || 0)} 元</strong></div>
              <div><span>优惠原因</span><strong>${escapeHtml(pricing.reason || '无')}</strong></div>
            </div>
          ` : '<span class="muted">暂无计价明细，可能是早期交易记录。</span>'}
        </div>
        <form class="confirm-form" id="confirmForm" data-id="${escapeHtml(record.transaction_id || '')}">
          <h4>人工确认 / 修正</h4>
          <div class="confirm-grid">
            <div class="field">
              <label for="confirmProduct">商品类别</label>
              <select id="confirmProduct">${productOptions}</select>
            </div>
            <div class="field">
              <label for="confirmPrice">单价</label>
              <input type="number" step="0.1" min="0" id="confirmPrice" value="${Number(record.unit_price || 0)}">
            </div>
            <div class="field">
              <label for="confirmNote">备注</label>
              <input type="text" id="confirmNote" placeholder="例如：后台人工确认">
            </div>
            <button class="primary" type="submit">确认修正</button>
          </div>
          <span class="message" id="confirmMsg">${latestCorrection ? `最近修正：${escapeHtml(latestCorrection.timestamp || '')}` : ''}</span>
        </form>
        <div>
          <strong>语音文本</strong>
          <pre>${escapeHtml(record.voice_text || '')}</pre>
        </div>
        <div>
          <strong>Top预测</strong>
          <pre>${escapeHtml(predictions.map(item => `${item.product_id}: ${Number(item.confidence || 0).toFixed(3)}`).join('\\n') || '无')}</pre>
        </div>
        <div>
          <strong>检测框</strong>
          <pre>${escapeHtml(detections.map(item => {
            const box = Array.isArray(item.bbox_xyxy) ? item.bbox_xyxy.join(', ') : '无坐标';
            return `${item.product_id}: ${Number(item.confidence || 0).toFixed(3)} [${box}]`;
          }).join('\\n') || '无框，等待人工/语音补盲')}</pre>
        </div>
        <div>
          <strong>原始记录</strong>
          <pre>${escapeHtml(JSON.stringify(record, null, 2))}</pre>
        </div>
      `;
    }

    async function openDetail(transactionId) {
      if (!transactionId) return;
      const record = latestRecords.find(item => item.transaction_id === transactionId)
        || await getJson(`/api/transaction?id=${encodeURIComponent(transactionId)}`);
      document.getElementById('detailBody').innerHTML = renderDetail(record);
      bindConfirmForm();
      document.getElementById('detailModal').classList.add('open');
    }

    function bindConfirmForm() {
      const form = document.getElementById('confirmForm');
      if (!form) return;
      const productSelect = document.getElementById('confirmProduct');
      const priceInput = document.getElementById('confirmPrice');
      productSelect.addEventListener('change', () => {
        const option = productSelect.selectedOptions[0];
        if (option?.dataset.price) priceInput.value = option.dataset.price;
      });
      form.addEventListener('submit', async event => {
        event.preventDefault();
        const message = document.getElementById('confirmMsg');
        message.textContent = '正在保存...';
        try {
          const updated = await getJson('/api/transaction/confirm', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              transaction_id: form.dataset.id,
              product_id: productSelect.value,
              unit_price: Number(priceInput.value || 0),
              note: document.getElementById('confirmNote').value,
              operator: 'web_dashboard'
            })
          });
          message.textContent = '已确认并重新计价';
          latestRecords = latestRecords.map(item => item.transaction_id === updated.transaction_id ? updated : item);
          document.getElementById('detailBody').innerHTML = renderDetail(updated);
          bindConfirmForm();
          await refresh();
        } catch (error) {
          message.textContent = `保存失败：${error.message}`;
        }
      });
    }

    function closeDetail() {
      document.getElementById('detailModal').classList.remove('open');
    }

    async function clearTransactions() {
      if (!confirm('确定要清空交易流水吗？该操作只清空 records/transactions.jsonl，不会删除商品、策略和运行参数。')) {
        return;
      }
      const result = await getJson('/api/transactions/clear', {method: 'POST'});
      alert(`已清空交易流水，共 ${Number(result.count || 0)} 条。`);
      closeDetail();
      await refresh();
    }

    async function clearDeviceEvents() {
      if (!confirm('确定要清空设备事件日志吗？该操作只清空 records/device_events.jsonl。')) {
        return;
      }
      const result = await getJson('/api/device-events/clear', {method: 'POST'});
      alert(`已清空设备事件，共 ${Number(result.count || 0)} 条。`);
      await refresh();
    }

    async function refresh() {
      const query = buildQuery();
      const eventParams = new URLSearchParams();
      const eventType = document.getElementById('eventTypeFilter')?.value || '';
      const eventLimit = document.getElementById('eventLimit')?.value || '100';
      if (eventType) eventParams.set('event_type', eventType);
      eventParams.set('limit', eventLimit);
      const [summary, records, productData, syncStatus, deviceStatus, analytics, runtimeData, policyData, policyHistoryData, eventSummary, eventRecords] = await Promise.all([
        getJson(`/api/summary?${query}`),
        getJson(`/api/transactions?${query}`),
        getJson('/api/products'),
        getJson('/api/mqtt-status'),
        getJson('/api/device-status'),
        getJson(`/api/analytics?${query}`),
        getJson('/api/runtime-config'),
        getJson('/api/device-policy'),
        getJson('/api/device-policy/history'),
        getJson('/api/device-events/summary'),
        getJson(`/api/device-events?${eventParams.toString()}`)
      ]);
      products = productData;
      latestRecords = records;
      renderSummary(summary);
      renderRecords(records);
      renderProducts();
      renderSyncStatus(syncStatus);
      renderDeviceStatus(deviceStatus);
      renderAnalytics(analytics);
      renderEventSummary(eventSummary);
      renderDeviceEvents(eventRecords);
      renderPolicyHistory(policyHistoryData);
      if (!document.activeElement?.closest('.runtime-card')) {
        renderRuntimeConfig(runtimeData);
      }
      if (!document.activeElement?.closest('#policyPage')) {
        renderDevicePolicy(policyData);
      }
      syncTransactionPanelHeight();
      document.getElementById('lastUpdated').textContent = `最后刷新 ${new Date().toLocaleTimeString()}`;
    }

    async function saveRuntimeConfig() {
      const message = document.getElementById('runtimeMsg');
      message.textContent = '正在保存...';
      try {
        const response = await getJson('/api/runtime-config', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(collectRuntimeConfig())
        });
        renderRuntimeConfig(response);
        if (response.mqtt?.error) {
          message.textContent = `MQTT下发失败：${response.mqtt.error}`;
        } else if (response.mqtt?.topic) {
          message.textContent = `已保存并下发：${response.mqtt.topic}`;
        } else {
          message.textContent = '已生成参数，等待下发状态';
        }
      } catch (error) {
        message.textContent = `保存失败：${error.message}`;
      }
      setTimeout(() => {
        if (message.textContent.startsWith('已保存')) message.textContent = '';
      }, 2400);
    }

    async function reloadRuntimeConfig() {
      const message = document.getElementById('runtimeMsg');
      message.textContent = '正在读取远程参数...';
      try {
        const data = await getJson('/api/runtime-config');
        renderRuntimeConfig(data);
        message.textContent = '已读取远程参数';
      } catch (error) {
        message.textContent = `读取失败：${error.message}`;
      }
    }

    async function saveDevicePolicy() {
      const message = document.getElementById('policyMsg');
      message.textContent = '正在保存...';
      try {
        const response = await getJson('/api/device-policy', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(collectDevicePolicy())
        });
        renderDevicePolicy(response);
        await refresh();
        if (response.mqtt?.error) {
          message.textContent = `MQTT下发失败：${response.mqtt.error}`;
        } else if (response.mqtt?.topic) {
          message.textContent = `已保存并下发：${response.mqtt.topic}`;
        } else {
          message.textContent = '已生成策略，等待下发状态';
        }
      } catch (error) {
        message.textContent = `保存失败：${error.message}`;
      }
    }

    async function reloadDevicePolicy() {
      const message = document.getElementById('policyMsg');
      message.textContent = '正在读取远程策略...';
      try {
        const data = await getJson('/api/device-policy');
        renderDevicePolicy(data);
        message.textContent = '已读取远程策略';
      } catch (error) {
        message.textContent = `读取失败：${error.message}`;
      }
    }

    async function rollbackDevicePolicy(historyIndex = null) {
      const message = document.getElementById('policyMsg');
      message.textContent = '正在回滚...';
      try {
        const options = historyIndex === null ? {method: 'POST'} : {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({history_index: historyIndex})
        };
        const data = await getJson('/api/device-policy/rollback', options);
        renderDevicePolicy(data);
        await refresh();
        message.textContent = `已回滚并下发：${data.policy?.policy_version || ''}`;
      } catch (error) {
        message.textContent = `回滚失败：${error.message}`;
      }
    }

    async function sendVoiceCommand(command) {
      const message = document.getElementById('voiceCommandMsg');
      const last = document.getElementById('voiceCommandLast');
      const topic = document.getElementById('voiceCommandTopic');
      const commandLabel = voiceCommandText(command);
      message.textContent = '正在下发语音补盲命令...';
      try {
        const response = await getJson('/api/voice-command', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({command, source: 'web_mobile'})
        });
        topic.textContent = response.topic || '暂无';
        last.textContent = commandLabel;
        if (response.mqtt?.error) {
          message.textContent = `下发失败：${response.mqtt.error}`;
        } else {
          message.textContent = `已下发：${commandLabel}`;
        }
      } catch (error) {
        message.textContent = `下发失败：${error.message}`;
      }
    }

    function describeVoiceIntent(response) {
      const intent = response.intent || {};
      if (intent.intent === 'confirm_product') {
        return `修正商品为：${intent.product_name || intent.product_id}`;
      }
      if (intent.intent === 'voice_command') {
        return `播报命令：${voiceCommandText(intent.command || response.command)}`;
      }
      return '暂无解析结果';
    }

    function describeVoiceIntentResult(response) {
      if (response.updated) {
        const record = response.updated;
        return `已修正交易 ${shortId(record.transaction_id)}，${record.product_name || record.product_id}，总价 ${record.total_price ?? '-'} 元`;
      }
      if (response.command) {
        return `已下发：${voiceCommandText(response.command)}`;
      }
      return '已执行';
    }

    async function executeVoiceIntent(text) {
      const message = document.getElementById('voiceCommandMsg');
      const topic = document.getElementById('voiceCommandTopic');
      const last = document.getElementById('voiceCommandLast');
      const textBox = document.getElementById('voiceIntentText');
      const parsed = document.getElementById('voiceIntentParsed');
      const result = document.getElementById('voiceIntentResult');
      const value = String(text || '').trim();
      if (!value) {
        message.textContent = '请先输入或说出一条语音补盲指令';
        return;
      }
      textBox.textContent = value;
      parsed.textContent = '正在解析...';
      result.textContent = '等待执行...';
      message.textContent = '正在执行语音补盲指令...';
      try {
        const response = await getJson('/api/voice-intent', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text: value, source: 'web_mobile'})
        });
        topic.textContent = response.topic || '暂无';
        parsed.textContent = describeVoiceIntent(response);
        result.textContent = describeVoiceIntentResult(response);
        if (response.command) last.textContent = voiceCommandText(response.command);
        if (response.speech_text) last.textContent = response.speech_text;
        if (response.mqtt?.error) {
          message.textContent = `已本地执行，MQTT下发失败：${response.mqtt.error}`;
        } else {
          message.textContent = '语音补盲执行完成';
        }
        await refresh();
      } catch (error) {
        parsed.textContent = '解析失败';
        result.textContent = error.message;
        message.textContent = `执行失败：${error.message}`;
      }
    }

    function startSpeechInput() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      const button = document.getElementById('startSpeechBtn');
      const input = document.getElementById('voiceIntentInput');
      const message = document.getElementById('voiceCommandMsg');
      if (!SpeechRecognition) {
        message.textContent = '当前浏览器不支持语音识别，请使用文本输入';
        return;
      }
      const recognition = new SpeechRecognition();
      recognition.lang = 'zh-CN';
      recognition.interimResults = false;
      recognition.maxAlternatives = 1;
      button.disabled = true;
      button.textContent = '聆听中...';
      message.textContent = '请说出补盲指令，例如：这是苹果';
      recognition.onresult = event => {
        const text = event.results?.[0]?.[0]?.transcript || '';
        input.value = text;
        executeVoiceIntent(text);
      };
      recognition.onerror = event => {
        message.textContent = `语音识别失败：${event.error || '未知错误'}，可改用文本输入`;
      };
      recognition.onend = () => {
        button.disabled = false;
        button.textContent = '语音输入';
      };
      recognition.start();
    }

    async function saveProducts() {
      const next = {};
      for (const row of document.querySelectorAll('#productList .product-row:not(.header)')) {
        const id = row.querySelector('[data-field="id"]').value.trim();
        if (!id) continue;
        next[id] = {
          name: row.querySelector('[data-field="name"]').value.trim() || id,
          unit_price: Number(row.querySelector('[data-field="unit_price"]').value || 0),
          unit: row.querySelector('[data-field="unit"]').value,
          enabled: row.querySelector('[data-field="enabled"]').checked,
          remark: row.querySelector('[data-field="remark"]').value.trim(),
          voice_name: row.querySelector('[data-field="name"]').value.trim() || id,
          price_history: products[row.dataset.originalId]?.price_history || [],
          created_at: products[row.dataset.originalId]?.created_at || ''
        };
      }
      products = await getJson('/api/products', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(next)
      });
      renderProducts();
      syncTransactionPanelHeight();
      document.getElementById('productMsg').textContent = '已保存商品档案';
      setTimeout(() => document.getElementById('productMsg').textContent = '', 1800);
    }

    function addProductRow() {
      const id = `new_product_${Date.now()}`;
      products[id] = {
        name: '新商品',
        unit: '斤',
        unit_price: 0,
        voice_name: '新商品',
        enabled: true,
        remark: '新品注册',
        price_history: []
      };
      renderProducts();
      syncTransactionPanelHeight();
    }

    document.getElementById('refreshBtn').addEventListener('click', refresh);
    document.getElementById('applyFiltersBtn').addEventListener('click', refresh);
    document.getElementById('applyEventFiltersBtn').addEventListener('click', refresh);
    document.getElementById('clearTransactionsBtn').addEventListener('click', clearTransactions);
    document.getElementById('clearEventsBtn').addEventListener('click', clearDeviceEvents);
    document.getElementById('resetFiltersBtn').addEventListener('click', () => {
      document.getElementById('filterStart').value = '';
      document.getElementById('filterEnd').value = '';
      document.getElementById('filterProduct').value = '';
      document.getElementById('filterStatus').value = '';
      document.getElementById('filterLimit').value = '100';
      refresh();
    });
    document.getElementById('exportCsvBtn').addEventListener('click', () => {
      const query = buildQuery(false);
      window.location.href = `/api/transactions.csv${query ? `?${query}` : ''}`;
    });
    document.getElementById('autoRefreshBtn').addEventListener('click', () => {
      autoRefresh = !autoRefresh;
      document.getElementById('autoRefreshBtn').textContent = `自动刷新：${autoRefresh ? '开' : '关'}`;
    });
    for (const button of document.querySelectorAll('.tab-btn')) {
      button.addEventListener('click', () => {
        for (const item of document.querySelectorAll('.tab-btn')) item.classList.remove('active');
        for (const page of document.querySelectorAll('.page')) page.classList.remove('active');
        button.classList.add('active');
        document.getElementById(button.dataset.page).classList.add('active');
        syncTransactionPanelHeight();
      });
    }
    window.addEventListener('resize', syncTransactionPanelHeight);
    document.getElementById('saveProductsBtn').addEventListener('click', saveProducts);
    document.getElementById('addProductBtn').addEventListener('click', addProductRow);
    document.querySelector('#runtimePage #saveRuntimeBtn').addEventListener('click', saveRuntimeConfig);
    document.getElementById('reloadRuntimeBtn').addEventListener('click', reloadRuntimeConfig);
    document.getElementById('savePolicyBtn').addEventListener('click', saveDevicePolicy);
    document.getElementById('reloadPolicyBtn').addEventListener('click', reloadDevicePolicy);
    document.getElementById('rollbackPolicyBtn').addEventListener('click', rollbackDevicePolicy);
    for (const button of document.querySelectorAll('[data-voice-command]')) {
      button.addEventListener('click', () => sendVoiceCommand(button.dataset.voiceCommand));
    }
    document.getElementById('startSpeechBtn').addEventListener('click', startSpeechInput);
    document.getElementById('runVoiceIntentBtn').addEventListener('click', () => {
      executeVoiceIntent(document.getElementById('voiceIntentInput').value);
    });
    document.getElementById('voiceIntentInput').addEventListener('keydown', event => {
      if (event.key === 'Enter') executeVoiceIntent(event.target.value);
    });
    document.getElementById('closeDetailBtn').addEventListener('click', closeDetail);
    document.getElementById('detailModal').addEventListener('click', event => {
      if (event.target.id === 'detailModal') closeDetail();
    });
    setInterval(() => {
      if (autoRefresh) refresh().catch(error => console.warn(error));
    }, 2500);
    refresh().catch(error => {
      document.getElementById('recordRows').innerHTML = `<tr><td class="empty-row" colspan="8">${escapeHtml(error.message)}</td></tr>`;
    });
