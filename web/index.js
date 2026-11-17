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
`;
class Status extends HTMLElement {
  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML = `<style>${css}:host{text-align:center;font-size:12px;padding:5px}.pending{color:#aa7a22}.approved{color:#278456}</style><span></span>`;
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
      <p class="muted">选择一次下载版本，后续新集自动追番。</p>
      <div id="notice" class="notice" hidden></div><div id="choices" class="choices"></div>
      <label class="field">开始方式<select id="mode"><option value="future">只追后续新发布</option><option value="backlog">补下已有集数</option></select></label>
      <details><summary>高级设置</summary>
        <label class="field">从第几集开始<input id="episode" type="number" min="0" max="100000" step="0.5" value="1"></label>
        <label><input id="custom" type="checkbox"> 自定义版本条件</label>
        <label id="include-field" class="field" hidden>必须包含（每行一项，全部匹配）<textarea id="include"></textarea></label>
        <label class="field">排除（每行一项，任一匹配即跳过）<textarea id="exclude"></textarea></label>
        <details><summary>查看匹配明细</summary><div id="matches"></div></details>
      </details>
      <div class="row actions"><button id="approve" class="primary" disabled>开始追番</button><button id="hold" hidden>暂停追番</button><button id="refresh">刷新版本</button></div>
      <div id="message" role="status">正在读取版本…</div>`;
    this.$ = id => this.shadowRoot.getElementById(id);
    this.rid = Number(this.context?.bangumiId);
    for (const id of ['mode','episode','include','exclude']) this.$(id).oninput = () => this.schedule();
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
    try {
      const data = await this.host.api.get(`rules/${this.rid}/versions`);
      if (!this.isConnected || serial !== this.serial) return;
      this.data = data; this.selected = null;
      const p = data.rule.policy || {};
      this.$('mode').value = p.mode || 'future'; this.$('episode').value = p.from_episode ?? 1;
      this.$('include').value = (p.include || []).join('\n'); this.$('exclude').value = (p.exclude || []).join('\n');
      this.$('hold').hidden = data.rule.state !== 'approved';
      this.$('approve').textContent = data.rule.state === 'approved' ? '保存版本' : '开始追番';
      this.$('notice').hidden = data.health.ready;
      this.$('notice').textContent = '请先在 AB 设置 → 订阅审核中启用保护，再开启聚合 RSS。';
      this.$('choices').replaceChildren();
      for (const v of data.versions) {
        const label = document.createElement('label'); label.className = 'choice';
        const radio = document.createElement('input'); radio.type = 'radio'; radio.name = 'version'; radio.value = v.id;
        const body = document.createElement('div'); const name = document.createElement('strong'); name.textContent = v.group; body.append(name);
        const tags = document.createElement('div'); tags.className = 'tags';
        for (const text of [v.resolution,v.source,v.codec,v.subtitle].filter(Boolean)) {const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = text; tags.append(tag);}
        body.append(tags); const count = document.createElement('small'); count.textContent = `已发现 ${v.count} 集`; body.append(count); body.title = v.example;
        const same = p.version_id ? p.version_id === v.id : p.include?.length === v.include.length && p.include.every(t => v.include.some(x => x.toLowerCase() === t.toLowerCase()));
        if (same) {radio.checked = true; this.selected = v.id;}
        radio.onchange = () => {this.selected = v.id; this.$('include').value = v.include.join('\n'); this.$('custom').checked = false; this.$('include-field').hidden = true; this.schedule();};
        label.append(radio, body); this.$('choices').append(label);
      }
      this.$('custom').checked = Boolean(p.include?.length && !this.selected);
      this.$('include-field').hidden = !this.$('custom').checked;
      this.$('message').className = '';
      this.$('message').textContent = data.versions.length ? '请选择要追的版本。' : '尚未发现可选版本。RSS 更新后可刷新；也可在高级设置中自定义条件。';
      if (this.selected || this.$('custom').checked) this.schedule();
    } catch (e) {if (serial === this.serial) this.error(e);}
  }
  schedule() {
    this.invalidate(); clearTimeout(this.timer);
    this.$('message').className = ''; this.$('message').textContent = '正在检查匹配集数…';
    this.timer = setTimeout(() => this.preview(), 250);
  }
  async preview() {
    const serial = this.serial;
    const terms = id => this.$(id).value.split('\n').map(s => s.trim()).filter(Boolean);
    const body = {exclude:terms('exclude'), mode:this.$('mode').value, from_episode:Number(this.$('episode').value)};
    if (this.$('custom').checked) body.include = terms('include');
    else if (this.selected) body.version_id = this.selected;
    else {this.$('message').textContent = '请先选择一个版本。'; return;}
    try {
      const result = await this.host.api.post(`rules/${this.rid}/preview`, body);
      if (!this.isConnected || serial !== this.serial) return;
      this.token = result.preview_token;
      this.$('approve').disabled = !this.data.health.ready || this.busy;
      this.$('message').textContent = body.mode === 'future' ? '仅下载确认后新发布的匹配集数。已有候选不会补下。' : `当前将补下 ${result.will_download} 集，已有下载会跳过。`;
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
    this.shadowRoot.innerHTML = `<style>${css}</style><p id="status">正在读取…</p><p id="help" class="muted">启用后，新发现的番剧会先等待选择版本。下载器连接、目录和已有下载均保留。已有番剧也需确认一次版本。</p><button id="activate" class="primary" disabled>启用并刷新</button><p id="refresh-note" class="muted">请先保存其他设置；启用后会刷新 AB 页面。</p>`;
    this.$ = id => this.shadowRoot.getElementById(id);
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
