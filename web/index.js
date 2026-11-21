// AB 4.0 UI slots; no framework, CDN, or changes to the host application.
const changed = new EventTarget();
let cachedRules, cachedAt = 0;
function invalidateRules() { cachedRules = null; cachedAt = 0; changed.dispatchEvent(new Event('change')); }
function rules(host) {
  if (!cachedRules || Date.now() - cachedAt > 3000) {
    cachedAt = Date.now(); cachedRules = host.api.get('rules').catch(e => {cachedRules = null; throw e;});
  }
  return cachedRules;
}
const css = `
:host{display:block;color:var(--ab-color-text,#292d36);font:14px/1.5 system-ui}
*{box-sizing:border-box}button,input,textarea,select{font:inherit;color:inherit}
button,textarea,select,input[type=number]{border:1px solid #8885;border-radius:6px;background:transparent;padding:8px 12px}
button{cursor:pointer}button:disabled{opacity:.45;cursor:default}.primary{background:var(--ab-color-primary,#4f6bed);color:#fff;border-color:transparent}
.muted,small{color:var(--ab-color-text-secondary,#777)}p{margin:8px 0 16px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.choices{display:grid;gap:8px;margin:16px 0}.choice{display:flex;align-items:start;gap:10px;padding:12px;border:1px solid #8884;border-radius:8px;cursor:pointer}.choice:has(input:checked){border-color:var(--ab-color-primary,#4f6bed);background:#6478ee0b}.choice input{margin-top:5px;accent-color:var(--ab-color-primary,#4f6bed)}.choice strong{display:block}.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:5px}.tag{padding:1px 7px;border-radius:4px;background:#8881;font-size:12px}
label.field{display:grid;gap:6px;margin:12px 0}textarea{width:100%;min-height:72px}details{margin:16px 0}summary{cursor:pointer;color:var(--ab-color-text-secondary,#777)}.actions{margin-top:20px}.notice{padding:10px;background:#d99b1515;border-radius:6px}.error{color:#c14c38}#message{min-height:22px;margin-top:12px}.match{padding:8px 0;border-bottom:1px solid #8883;overflow-wrap:anywhere}#matches{max-height:260px;overflow:auto} [hidden]{display:none!important}
.start-mode{border:0;padding:0;margin:12px 0;min-width:0}.start-mode legend{padding:0;margin-bottom:6px}
.start-options{display:flex;gap:8px;flex-wrap:wrap}.start-options .choice{align-items:center;min-height:44px;padding:8px 12px}.start-options input{margin:0}
`;
const settingsCard = `
:host{font-family:var(--ab-font-family,system-ui)}
.settings-panel{margin:0;border:1px solid var(--ab-color-border,#e2e8f0);border-radius:var(--ab-radius-md,8px);overflow:hidden;min-width:0;max-width:100%;transition:border-color var(--ab-transition-normal,200ms)}
.settings-panel>summary{display:flex;align-items:center;justify-content:space-between;gap:8px;height:34px;padding:0 14px;list-style:none;color:var(--ab-color-text-secondary,#64748b);font-size:15px;font-weight:600;border-bottom:1px solid transparent;cursor:pointer}
.settings-panel>summary::-webkit-details-marker{display:none}.settings-panel>summary:hover{color:var(--ab-color-text,#1e293b)}.settings-panel>summary:focus-visible{outline:2px solid var(--ab-color-primary);outline-offset:-2px}.settings-panel[open]>summary{border-bottom-color:var(--ab-color-border,#e2e8f0)}
.settings-panel>summary svg{width:14px;height:14px;transition:transform var(--ab-transition-normal,200ms)}.settings-panel[open]>summary svg{transform:rotate(180deg)}
.settings-body{background:var(--ab-color-surface,#fff);padding:12px 14px;font-size:14px;color:var(--ab-color-text,#1e293b);overflow-x:hidden}.settings-body p:last-child{margin-bottom:0}
@media(pointer:coarse){.settings-panel>summary{height:44px}}`;
const settingsFrame = (title, body) => `<style>${css}${settingsCard}</style><details class="settings-panel" open><summary><span>${title}</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg></summary><div class="settings-body">${body}</div></details>`;
class Status extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML = `<style>${css}:host{text-align:center;padding:6px 4px}span{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:600;line-height:1.4;border:1px solid transparent}span:before{content:'';width:5px;height:5px;border-radius:50%;background:currentColor;flex:none}.pending{color:#a66a12;background:#d99b1518;border-color:#d99b1533}.approved{color:#248464;background:#2da77e16;border-color:#2da77e30}@media(prefers-color-scheme:dark){.pending{color:#e5b657;background:#d99b1520}.approved{color:#72cfae;background:#2da77e20}}</style><span></span>`;
    this.update = async () => {
      try {
        const data = await rules(this.host);
        if (!this.isConnected) return;
        const rule = data.rules.find(r => r.id === Number(this.context?.bangumiId));
        const el = this.shadowRoot.querySelector('span');
        el.textContent = rule?.state === 'approved' ? '追番中' : '待选版本';
        el.className = rule?.state === 'approved' ? 'approved' : 'pending';
        el.title = '打开番剧 → 下载版本';
      } catch {this.shadowRoot.querySelector('span').textContent = '审核状态暂不可用';}
    };
    changed.addEventListener('change', this.update); this.update();
  }
  disconnectedCallback() {changed.removeEventListener('change', this.update);}
}
class Detail extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML = `<style>${css}</style>
      <p class="muted">单集、合集和单片可分别选择。每个所选版本独立去重。</p>
      <div id="notice" class="notice" hidden></div><div id="choices" class="choices"></div>
      <fieldset id="mode" class="start-mode" disabled>
        <legend>开始方式</legend>
        <div class="start-options">
          <label class="choice"><input type="radio" name="mode" value="backlog" checked>补下已发现的缺集</label>
          <label class="choice"><input type="radio" name="mode" value="future">只追后续新发布</label>
        </div>
      </fieldset>
      <details id="advanced"><summary>高级设置</summary>
        <label class="field">从第几集开始<input id="episode" type="number" min="0" max="100000" step="0.5" value="1"><small>合集按第一集判断，单片没有集数限制。</small></label>
        <label><input id="custom" type="checkbox"> 自定义版本条件</label>
        <label id="include-field" class="field" hidden>必须包含（每行一项，全部匹配）<textarea id="include"></textarea></label>
        <label class="field">排除（每行一项，任一匹配即跳过）<textarea id="exclude"></textarea></label>
        <details><summary>查看匹配明细</summary><div id="matches"></div></details>
      </details>
      <div class="row actions"><button id="approve" class="primary" disabled>开始追番</button><button id="hold" hidden>暂停追番</button><button id="refresh">刷新版本</button></div>
      <div id="message" role="status">正在读取版本…</div>`;
    this.$ = id => this.shadowRoot.getElementById(id);
    this.rid = Number(this.context?.bangumiId);
    this.$('mode').onchange = () => this.schedule();
    for (const id of ['episode','include','exclude']) this.$(id).oninput = () => this.schedule();
    this.$('custom').onchange = () => {this.$('include-field').hidden = !this.$('custom').checked; this.schedule();};
    this.$('refresh').onclick = () => this.load();
    this.$('approve').onclick = () => this.mutate('approve', {preview_token:this.token});
    this.$('hold').onclick = () => this.mutate('hold', {});
    this.load();
  }
  disconnectedCallback() {clearTimeout(this.timer); this.serial = (this.serial || 0) + 1;}
  invalidate() {this.serial = (this.serial || 0) + 1; this.token = null; this.$('approve').disabled = true;}
  error(e) {this.$('message').textContent = e.message || String(e); this.$('message').className = 'error';}
  async load() {
    this.invalidate(); const serial = this.serial;
    clearTimeout(this.timer);
    this.data = null;
    this.$('mode').disabled = true;
    this.$('refresh').disabled = true;
    this.$('message').className = '';
    this.$('message').textContent = '正在读取版本…';
    try {
      const data = await this.host.api.get(`rules/${this.rid}/versions`);
      if (!this.isConnected || serial !== this.serial) return;
      this.data = data; this.selected = new Set(); this.unfiltered = false;
      this.$('mode').disabled = Boolean(data.needs_sync);
      this.$('choices').hidden = Boolean(data.needs_sync);
      this.$('advanced').hidden = Boolean(data.needs_sync);
      const p = data.rule.policy || {};
      const mode = p.mode === 'future' ? 'future' : 'backlog';
      for (const input of this.$('mode').querySelectorAll('input')) input.checked = input.value === mode;
      this.$('episode').value = p.from_episode ?? 1;
      this.$('include').value = (p.include || []).join('\n'); this.$('exclude').value = (p.exclude || []).join('\n');
      this.$('hold').hidden = data.rule.state !== 'approved';
      this.$('approve').textContent = data.rule.state === 'approved' ? '保存版本' : '开始追番';
      this.$('notice').hidden = data.health.ready;
      this.$('notice').textContent = '请先在 AB 设置 → 订阅审核中启用保护，再开启聚合 RSS。';
      this.$('choices').replaceChildren();
      const any = document.createElement('label'); any.className = 'choice';
      const anyRadio = document.createElement('input'); anyRadio.type = 'checkbox'; anyRadio.name = 'unfiltered';
      const anyText = document.createElement('div'); anyText.innerHTML = '<strong>单集不过滤版本，沿用 AB 规则</strong><small>允许所有符合 AB 过滤条件的单集版本。合集和单片可单独勾选。</small>';
      if (p.unfiltered) {anyRadio.checked = true; this.unfiltered = true;}
      anyRadio.onchange = () => {this.unfiltered = anyRadio.checked; this.selected.clear(); this.$('choices').querySelectorAll('input[name=version]').forEach(i => {i.checked = false;}); this.$('include').value = ''; this.$('exclude').value = ''; this.$('custom').checked = false; this.$('include-field').hidden = true; this.schedule();};
      any.append(anyRadio, anyText); this.$('choices').append(any);
      for (const v of data.versions) {
        const label = document.createElement('label'); label.className = 'choice';
        const radio = document.createElement('input'); radio.type = 'checkbox'; radio.name = 'version'; radio.value = v.id;
        const body = document.createElement('div'); const name = document.createElement('strong'); name.textContent = v.group; body.append(name);
        const tags = document.createElement('div'); tags.className = 'tags';
        const kind = {batch:'合集', feature:'单片', single:'单集'}[v.kind] || '单集';
        for (const text of [kind,v.resolution || '清晰度未标注',v.source,v.codec,v.subtitle].filter(Boolean)) {const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = text; tags.append(tag);}
        body.append(tags); const count = document.createElement('small');
        count.textContent = v.kind === 'batch'
          ? `已发现 ${v.count} 个合集：${v.ranges.map(([start, end]) => `${start}–${end} 集`).join('、')}`
          : v.kind === 'feature' ? '整部作品，无集号' : `已发现 ${v.count} 集：${v.episodes.join('、')}`;
        body.append(count); body.title = v.example;
        const same = p.version_ids?.length ? p.version_ids.includes(v.id) : p.version_id ? p.version_id === v.id : v.kind === 'single' && p.include?.length === v.include.length && p.include.every(t => v.include.some(x => x.toLowerCase() === t.toLowerCase()));
        if (same) {radio.checked = true; this.selected.add(v.id);}
        radio.onchange = () => {if (radio.checked) this.selected.add(v.id); else this.selected.delete(v.id); this.unfiltered = false; anyRadio.checked = false; this.$('include').value = v.include.join('\n'); this.$('custom').checked = false; this.$('include-field').hidden = true; this.schedule();};
        label.append(radio, body); this.$('choices').append(label);
      }
      this.$('custom').checked = Boolean(p.include?.length && !this.selected.size);
      this.$('include-field').hidden = !this.$('custom').checked;
      this.$('message').className = '';
      this.$('message').textContent = data.needs_sync
        ? '番剧规则已变化，请等待后台同步后刷新版本。'
        : data.versions.length ? '请选择要追的版本。' : '尚未发现可选版本。后台收集候选后可刷新；也可在高级设置中自定义条件。';
      if (this.selected.size || this.unfiltered || this.$('custom').checked) this.schedule();
    } catch (e) {if (serial === this.serial) this.error(e);}
    finally {if (this.isConnected) this.$('refresh').disabled = false;}
  }
  schedule() {
    if (!this.data || this.data.needs_sync) return;
    this.invalidate(); clearTimeout(this.timer);
    this.$('exclude').disabled = this.unfiltered && !this.$('custom').checked;
    this.$('message').className = ''; this.$('message').textContent = '正在检查匹配集数…';
    this.timer = setTimeout(() => this.preview(), 250);
  }
  async preview() {
    const serial = this.serial;
    const terms = id => this.$(id).value.split('\n').map(s => s.trim()).filter(Boolean);
    const body = {exclude:terms('exclude'), mode:this.$('mode').querySelector('input:checked').value, from_episode:Number(this.$('episode').value)};
    if (this.$('custom').checked) body.include = terms('include');
    else if (this.unfiltered) {body.unfiltered = true; body.exclude = [];}
    else if (this.selected.size) body.version_ids = [...this.selected];
    else {this.$('message').textContent = '请至少选择一个版本，或选择不过滤版本。'; return;}
    try {
      const result = await this.host.api.post(`rules/${this.rid}/preview`, body);
      if (!this.isConnected || serial !== this.serial) return;
      this.token = result.preview_token;
      this.$('approve').disabled = !this.data.health.ready || this.busy;
      const counts = result.download_counts;
      const breakdown = [[counts?.single, '单集'], [counts?.batch, '合集'], [counts?.feature, '单片']]
        .filter(([count]) => count).map(([count, label]) => `${count} 个${label}`).join('、');
      this.$('message').textContent = body.mode === 'future'
        ? '仅下载确认后匹配的新发布。已有候选不会补下。'
        : `将下载 ${result.will_download} 个资源${breakdown ? `（${breakdown}）` : ''}。已下载的同版本资源会跳过。`;
      this.$('matches').replaceChildren();
      for (const item of result.items.slice(0,500)) {const row = document.createElement('div'); row.className = 'match'; row.textContent = `${item.name} · ${item.reason || '将下载'}`; this.$('matches').append(row);}
    } catch (e) {if (serial === this.serial) this.error(e);}
  }
  async mutate(action, body) {
    if (this.busy) return;
    this.busy = true; this.invalidate(); this.$('hold').disabled = true;
    try {
      await this.host.api.post(`rules/${this.rid}/${action}`, body);
      invalidateRules(); this.host.toast(action === 'hold' ? '已暂停这部番的新下载' : '已保存下载版本', 'info');
      await this.load();
    } catch (e) {this.error(e);}
    finally {this.busy = false; this.$('hold').disabled = false;}
  }
}
class Settings extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML = settingsFrame('订阅审核', `<p id="status">正在读取…</p><p id="help" class="muted">启用后，新发现的番剧会先等待选择版本。下载器连接、目录和已有下载均保留。已有番剧也需确认一次版本。</p><button id="activate" class="primary" disabled>启用并刷新</button><p id="refresh-note" class="muted">请先保存其他设置；启用后会刷新 AB 页面。</p><hr style="border:0;border-top:1px solid var(--ab-color-border,#e2e8f0);margin:16px 0"><p id="update-status" role="status">正在读取更新状态…</p><div class="row"><button id="update-check">检查更新</button><button id="update-install" class="primary" hidden>更新插件</button><a id="release" target="_blank" rel="noopener noreferrer" hidden>版本说明</a></div><small>每 6 小时自动检查 GitHub 稳定版；点击更新后先备份、校验并重新加载。更新成功会刷新页面，请先保存其他设置。</small>`);
    this.$ = id => this.shadowRoot.getElementById(id);
    this.renderUpdate = data => {
      if (!this.isConnected) return;
      this.updateData = data;
      this.$('update-status').textContent = `当前 ${data.current} · ` + (data.error || (data.available ? `有新版 ${data.version}` : data.checked_at ? '已是最新稳定版' : '等待自动检查'));
      this.$('update-install').hidden = !data.available;
      this.$('update-install').textContent = `更新到 ${data.version || ''}`;
      this.$('release').hidden = !data.url;
      if (data.url) this.$('release').href = data.url;
    };
    this.$('update-check').onclick = async () => {
      this.$('update-check').disabled = true;
      try {this.renderUpdate(await this.host.api.post('update/check', {}));}
      catch (e) {this.$('update-status').textContent = e.message || String(e);}
      finally {this.$('update-check').disabled = false;}
    };
    this.$('update-install').onclick = async () => {
      this.$('update-install').disabled = true; this.$('update-check').disabled = true;
      this.$('update-status').textContent = '正在下载、备份并安装，请稍候…';
      try {
        await this.host.api.post('update/install', {version:this.updateData.version});
        window.location.reload();
      } catch (e) {
        this.$('update-status').textContent = `${e.message || e}。如连接中断，请刷新页面确认当前版本后再操作。`;
        this.$('update-install').disabled = false; this.$('update-check').disabled = false;
      }
    };
    this.host.api.get('update').then(this.renderUpdate).catch(e => {this.$('update-status').textContent = e.message || String(e);});
    this.$('activate').onclick = async () => {
      this.$('activate').disabled = true;
      try {await this.host.api.post('activate', {}); invalidateRules(); window.location.reload();}
      catch (e) {this.$('status').textContent = e.message || String(e); this.$('activate').disabled = false;}
    };
    rules(this.host).then(data => {
      if (!this.isConnected) return;
      this.$('status').textContent = data.health.ready ? '已启用 · 在番剧详情的「下载版本」中选择版本即可追番。' : '启用订阅审核后，即可安全开启 Mikan 聚合 RSS。';
      this.$('activate').disabled = false;
      this.$('activate').hidden = data.health.ready;
      this.$('refresh-note').hidden = data.health.ready;
    }).catch(e => {this.$('status').textContent = e.message || String(e);});
  }
}
customElements.define('ab-plugin-subscription-review-status', Status);
customElements.define('ab-plugin-subscription-review-detail', Detail);
customElements.define('ab-plugin-subscription-review-settings', Settings);

class History extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML = `<style>${css}</style>
      <p>清除按集数保存的下载历史，让已下载过的集数可以重新选版本补下。支持清理已删除规则留下的记录。</p>
      <p class="notice">清除会同时处理 AB 历史和插件去重记录，并暂停同番相关规则等待重新审核。媒体文件和 qB 任务会保留。</p>
      <div class="row"><button id="all">全选 / 取消全选</button><button id="refresh">刷新历史</button></div>
      <div id="episodes" class="choices"></div>
      <div class="row"><button id="preview">预览清除所选历史</button><button id="clear" class="primary" hidden>确认清除并备份</button></div>
      <p id="message" role="status"></p>`;
    this.$ = id => this.shadowRoot.getElementById(id);
    this.rid = Number(this.context?.bangumiId);
    this.$('refresh').onclick = () => this.load();
    this.$('all').onclick = () => {const inputs = [...this.$('episodes').querySelectorAll('input')]; const checked = !inputs.every(i => i.checked); inputs.forEach(i => {i.checked = checked;}); this.invalidate();};
    this.$('preview').onclick = () => this.preview();
    this.$('clear').onclick = () => this.clear();
    this.load();
  }
  invalidate() {this.serial = (this.serial || 0) + 1; this.token = null; this.$('clear').hidden = true; this.$('message').textContent = '';}
  async load() {
    this.invalidate(); this.$('preview').disabled = true;
    try {
      const data = await this.host.api.get(`rules/${this.rid}/history`);
      if (!this.isConnected) return;
      this.$('episodes').replaceChildren();
      for (const ep of data.episodes) {
        const label = document.createElement('label'); label.className = 'choice';
        const input = document.createElement('input'); input.type = 'checkbox'; input.value = JSON.stringify(ep.selection); input.onchange = () => this.invalidate();
        const content = document.createElement('div'); const title = document.createElement('strong'); title.textContent = ep.label;
        const details = document.createElement('small'); details.textContent = ep.names.join('\n') || '仅剩插件去重记录'; details.style.whiteSpace = 'pre-wrap';
        content.append(title, details); label.append(input, content); this.$('episodes').append(label);
      }
      this.$('preview').disabled = !data.episodes.length;
      this.$('message').textContent = data.episodes.length ? `共 ${data.episodes.length} 条下载历史。包括同番其他字幕组和已删除规则的记录。` : '没有已下载历史。';
    } catch (e) {this.$('message').textContent = e.message || String(e);}
  }
  async preview() {
    const episodes = [...this.$('episodes').querySelectorAll('input:checked')].map(i => JSON.parse(i.value));
    if (!episodes.length) {this.$('message').textContent = '请先勾选记录。'; return;}
    this.$('preview').disabled = true; const serial = this.serial;
    try {
      const data = await this.host.api.post(`rules/${this.rid}/history/preview`, {episodes});
      if (!this.isConnected || serial !== this.serial) return;
      this.token = data.preview_token; this.$('clear').hidden = false;
      this.$('message').textContent = `将清除「${data.title}」${data.episodes.map(e => e.label).join('、')}的历史，并将 ${data.related_rules.length} 条相关规则转为待审核。确认后备份并清除。`;
    } catch (e) {this.$('message').textContent = e.message || String(e);}
    finally {this.$('preview').disabled = false;}
  }
  async clear() {
    if (!this.token || this.busy) return;
    this.busy = true; this.$('clear').disabled = true; this.$('preview').disabled = true;
    try {
      const result = await this.host.api.post(`rules/${this.rid}/history/clear`, {preview_token:this.token});
      invalidateRules(); await this.load();
      this.$('message').textContent = `已清除 ${result.cleared_episodes.length} 条历史。请重新选择下载版本。备份：${result.backup}`;
      this.host.toast('已清除历史，相关番剧等待重新选版本', 'info');
    } catch (e) {this.invalidate(); this.$('message').textContent = e.message || String(e);}
    finally {this.busy = false; this.$('clear').disabled = false; this.$('preview').disabled = false;}
  }
}
class Maintenance extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML = settingsFrame('下载历史维护', `<p>查找现有番剧及已删除规则留下的下载历史。</p>
      <label class="field">番剧<input id="search" placeholder="输入番名筛选"><select id="rule"><option value="">选择番剧…</option></select></label>
      <div id="history"></div><details><summary>检查未匹配种子</summary><button id="refresh">刷新</button><div id="orphans"></div><button id="repair">重新关联可识别记录</button><p id="message" role="status"></p></details>`);
    this.$ = id => this.shadowRoot.getElementById(id);
    this.$('search').oninput = () => this.options();
    this.$('rule').onchange = () => {
      this.$('history').replaceChildren(); const rid = Number(this.$('rule').value); if (!rid) return;
      const el = document.createElement('ab-plugin-subscription-review-history'); el.host = this.host; el.context = {bangumiId:rid}; this.$('history').append(el);
    };
    this.$('refresh').onclick = () => this.orphans();
    this.$('repair').onclick = async () => {
      const ids = (this.orphanRows || []).filter(r => r.rule_id).map(r => r.id);
      if (!ids.length) return;
      this.$('repair').disabled = true;
      try {const result = await this.host.api.post('maintenance/orphans/repair', {ids}); invalidateRules(); await this.orphans(); this.$('message').textContent = `已关联 ${result.repaired.length} 条，合并 ${result.merged.length} 条重复记录。相关番剧先等待选版本。`;}
      catch(e) {this.$('message').textContent = e.message || String(e);}
      finally {this.$('repair').disabled = false;}
    };
    this.host.api.get('maintenance/rules').then(data => {this.rows = data; this.options();}).catch(e => {this.$('message').textContent = e.message;});
    this.orphans();
  }
  options() {
    this.$('rule').replaceChildren(new Option('选择番剧…', ''));
    const query = this.$('search').value.toLowerCase();
    for (const r of this.rows || []) if (r.title.toLowerCase().includes(query)) {
      this.$('rule').append(new Option(`${r.title} · 第 ${r.season} 季 · ${r.group || '未标注组'} · #${r.id}${r.missing || r.deleted ? '（已删除规则）' : ''}`, r.id));
    }
  }
  async orphans() {
    try {
      this.orphanRows = await this.host.api.get('maintenance/orphans'); this.$('orphans').replaceChildren();
      for (const row of this.orphanRows) {const el = document.createElement('div'); el.className = 'match'; el.textContent = `#${row.id} ${row.name} · ${row.reason}${row.rule_title ? ' → ' + row.rule_title : ''}`; this.$('orphans').append(el);}
      this.$('message').textContent = this.orphanRows.length ? `共 ${this.orphanRows.length} 条未匹配记录。重新关联会暂停涉及番剧，等待确认版本。` : '没有未匹配种子。';
      this.$('repair').disabled = !this.orphanRows.some(r => r.rule_id);
    } catch(e) {this.$('message').textContent = e.message || String(e);}
  }
}
customElements.define('ab-plugin-subscription-review-history', History);
customElements.define('ab-plugin-subscription-review-maintenance', Maintenance);
