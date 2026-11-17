// Native Web Component: no CDN, build step, framework, or injected HTML titles.
class ReviewPanel extends HTMLElement {
  connectedCallback() {
    this.attachShadow({mode: 'open'});
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;color:var(--ab-color-text,#222);font:14px/1.5 system-ui}
        section{background:var(--ab-color-surface,#fff);padding:16px;border:1px solid #8885;border-radius:10px}
        h3{margin:0 0 10px}button,select,input,textarea{font:inherit;color:inherit;background:transparent;border:1px solid #8888;border-radius:6px;padding:7px;box-sizing:border-box}
        button{cursor:pointer}button:disabled{opacity:.45;cursor:default}button.primary{background:var(--ab-color-primary,#386bc4);color:white;border:0}
        .bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:grid;gap:5px}textarea{width:100%;min-height:130px}
        .notice{padding:9px;border-radius:6px;background:#d99b1520;white-space:pre-wrap}.muted{opacity:.7}.list{max-height:380px;overflow:auto}article{border-top:1px solid #8884;padding:9px 0;overflow-wrap:anywhere}article button{margin-top:5px}.ok{color:#198149}.error{color:#c14c38}
        @media(max-width:600px){.grid{grid-template-columns:1fr}.bar select{width:100%}}
      </style>
      <section>
        <h3>订阅审核</h3>
        <div class="notice" id="health">正在读取审核状态…</div>
        <div class="bar"><select id="rules" aria-label="选择番剧"></select><button id="refresh">刷新</button><button id="sync">同步已有下载记录</button></div>
        <div id="editor" hidden>
          <p class="muted">每部番确认一次版本条件。关键词按字面匹配，忽略大小写；必须包含全部指定词，排除任意指定词。可从下方发布选取条件。</p>
          <div class="grid"><label>必须包含（每行一项）<textarea id="include" placeholder="LoliHouse\n1080p\nHEVC\n内封"></textarea></label><label>排除（每行一项）<textarea id="exclude"></textarea></label></div>
          <div class="bar"><label>开始方式<select id="mode"><option value="future">只追预览之后的新发布</option><option value="backlog">也处理当前候选（按已有记录去重）</option></select></label><label>从第几集开始<input id="episode" type="number" min="0" max="100000" value="1" step="0.5"></label></div>
          <div class="bar"><button id="preview">预览匹配结果</button><button class="primary" id="approve" disabled>批准并追番</button><button id="hold">暂停这部番的新下载</button></div>
          <div role="status" id="message"></div>
          <h4 id="list-title">候选发布</h4><div class="list" id="candidates"></div>
        </div>
      </section>`;
    this.$ = id => this.shadowRoot.getElementById(id);
    this.$('rules').onchange = () => this.loadRule();
    this.$('refresh').onclick = () => this.refresh();
    this.$('sync').onclick = () => this.run(async () => {await this.host.api.post('sync', {}); await this.refresh();});
    this.$('preview').onclick = () => this.run(() => this.preview());
    this.$('approve').onclick = () => this.run(() => this.approve());
    this.$('hold').onclick = () => this.run(async () => {await this.host.api.post(`rules/${this.rid}/hold`, {}); await this.refresh();});
    for (const id of ['include','exclude','mode','episode']) this.$(id).oninput = () => this.invalidate();
    this.refresh();
  }
  async run(action) {
    if (this.busy) return;
    this.busy = true;
    try {await action();} catch (e) {this.$('message').textContent = e.message || String(e); this.host.toast(e.message || '操作失败', 'error');}
    finally {this.busy = false;}
  }
  invalidate() {this.editSerial = (this.editSerial || 0) + 1; this.token = null; this.$('approve').disabled = true;}
  async refresh() {
    try {
      const data = await this.host.api.get('rules');
      if (!this.isConnected) return;
      this.data = data;
      this.$('health').textContent = data.health.ready
        ? '下载器保护已生效。未批准的番剧不会投递；后台每 30 秒检查已批准候选。'
        : `保护尚未就绪：将下载器实例 ${data.health.unguarded_instances.join('、') || '（未配置）'} 的类型改为 ${data.health.provider}，保持原实例 ID、路径和凭据。先停用 RSS，再完成切换。`;
      const old = this.context?.bangumiId || this.rid;
      this.$('rules').replaceChildren();
      for (const r of data.rules) {
        const opt = document.createElement('option'); opt.value = r.id;
        opt.textContent = `${r.state === 'approved' ? '已批准' : '待审核'} · ${r.official_title} · S${r.season} · ${r.count} 条`;
        this.$('rules').append(opt);
      }
      if (data.rules.some(r => r.id === Number(old))) this.$('rules').value = String(old);
      this.$('rules').disabled = Boolean(this.context?.bangumiId);
      this.$('editor').hidden = !data.rules.length;
      if (data.rules.length) await this.loadRule();
    } catch (e) {this.$('health').textContent = `无法读取审核状态：${e.message || e}`;}
  }
  async loadRule() {
    this.invalidate(); this.rid = Number(this.$('rules').value);
    const rid = this.rid;
    const rule = this.data.rules.find(r => r.id === rid);
    const p = rule.policy || {};
    this.$('include').value = (p.include || []).join('\n');
    this.$('exclude').value = (p.exclude || []).join('\n');
    this.$('mode').value = p.mode || 'future'; this.$('episode').value = p.from_episode ?? 1;
    this.$('message').textContent = rule.state === 'approved' ? '现有条件已批准；修改后需重新预览并批准。' : '待审核：可以先开启聚合 RSS 收集候选，再在这里选择版本。';
    const items = await this.host.api.get(`rules/${rid}/candidates`);
    if (this.rid !== rid || !this.isConnected) return;
    this.items = items; this.render(items, false);
  }
  render(items, preview) {
    this.$('list-title').textContent = preview ? '匹配预览' : `候选发布（${items.length}）`;
    this.$('candidates').replaceChildren();
    for (const item of items.slice(0,500)) {
      const row = document.createElement('article');
      const title = document.createElement('div'); title.textContent = item.name; row.append(title);
      const reason = document.createElement('div'); reason.className = item.reason ? 'muted' : 'ok'; reason.textContent = item.reason || '符合条件，将进入下载队列'; row.append(reason);
      if (!preview) {
        const pick = document.createElement('button'); pick.textContent = '使用此发布的版本条件';
        pick.onclick = () => {
          const codec = item.name.match(/\b(HEVC|AVC|x265|x264|H\.265|H\.264)\b/i)?.[0];
          this.$('include').value = [...new Set([item.group,item.resolution,item.source,item.subtitle,codec].filter(Boolean))].join('\n');
          this.invalidate(); this.$('include').focus();
        };
        row.append(pick);
      }
      this.$('candidates').append(row);
    }
    if (items.length > 500) {const note=document.createElement('p');note.textContent='仅展示前 500 条；预览和审核会检查全部候选。';this.$('candidates').append(note);}
  }
  async preview() {
    const serial = this.editSerial, rid = this.rid;
    const terms = id => this.$(id).value.split('\n').map(s => s.trim()).filter(Boolean);
    const result = await this.host.api.post(`rules/${this.rid}/preview`, {include:terms('include'), exclude:terms('exclude'), mode:this.$('mode').value, from_episode:Number(this.$('episode').value)});
    if (serial !== this.editSerial || rid !== this.rid || !this.isConnected) return;
    this.token = result.preview_token; this.render(result.items, true);
    this.$('message').textContent = `当前符合条件：${result.will_download} 条。已有集数会跳过，后续新集沿用这些条件。`;
    this.$('approve').disabled = !this.data.health.ready;
  }
  async approve() {
    const result = await this.host.api.post(`rules/${this.rid}/approve`, {preview_token:this.token});
    this.invalidate(); await this.refresh(); this.$('message').textContent = result.message;
  }
}
customElements.define('ab-plugin-subscription-review', ReviewPanel);
customElements.define('ab-plugin-subscription-review-detail', class extends ReviewPanel {});
